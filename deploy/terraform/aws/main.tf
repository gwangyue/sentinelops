# AWS 上的 SentinelOps 基础设施（01 §5.2 的拓扑，ADR-007）。
# 负责：VPC（2 个可用区、单个 NAT）、ECR 的 VPC 端点、EKS 集群和节点组、插件（含 EBS CSI 的 Pod Identity）、ECR 仓库。
# 不负责：集群里的东西（Helm chart 和 ECK operator，由 deploy/aws/aws-up.sh 装）；state 桶（aws-up.sh 建，一直保留）。
# 只在演示时 apply，用完就 destroy：控制面、NAT、接口端点不足 1 小时按 1 小时计费（spike-results S6）。

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)
  cidr = "10.0.0.0/16"
}

# ---------- 网络 ----------
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.7.3" # 2026-09-18

  name = var.name
  cidr = local.cidr
  azs  = local.azs

  # 节点只放私有子网；公有子网只放 NAT（01 §5.2：没有 ALB / Ingress，不暴露公网入口）
  private_subnets = [for i in range(2) : cidrsubnet(local.cidr, 4, i)]
  public_subnets  = [for i in range(2) : cidrsubnet(local.cidr, 8, 100 + i)]

  enable_nat_gateway   = true
  single_nat_gateway   = true # 演示环境，一个 NAT 就够；每个可用区一个会让 NAT 费用翻倍
  enable_dns_hostnames = true # 接口端点的私有 DNS 需要
}

# 拉 ECR 镜像走 VPC 端点，不经过 NAT（NAT 按 GB 收处理费）；S3 是网关端点，免费（ECR 的镜像层存在 S3 里）
module "vpc_endpoints" {
  source  = "terraform-aws-modules/vpc/aws//modules/vpc-endpoints"
  version = "6.7.3"

  vpc_id                     = module.vpc.vpc_id
  create_security_group      = true
  security_group_name_prefix = "${var.name}-vpce-"
  security_group_rules = {
    ingress_https = { cidr_blocks = [local.cidr] }
  }

  endpoints = {
    s3 = {
      service         = "s3"
      service_type    = "Gateway"
      route_table_ids = module.vpc.private_route_table_ids
    }
    ecr_api = {
      service             = "ecr.api"
      private_dns_enabled = true
      subnet_ids          = module.vpc.private_subnets
    }
    ecr_dkr = {
      service             = "ecr.dkr"
      private_dns_enabled = true
      subnet_ids          = module.vpc.private_subnets
    }
  }
}

# ---------- EBS CSI 的权限：Pod Identity ----------
# 不用 IRSA：组织的 SCP 拒绝 iam:CreateOpenIDConnectProvider（spike-results S6 补充）。Pod Identity 不需要 OIDC provider，
# 也是 AWS 现在推荐的方式。没有这个权限，PVC 会一直 Pending（"could not create volume in EC2: UnauthorizedOperation"）
resource "aws_iam_role" "ebs_csi" {
  name = "${var.name}-ebs-csi"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEBSCSIDriverPolicyV2"
}

# ---------- EKS ----------
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "21.29.0" # 2026-10-07

  name               = var.name
  kubernetes_version = var.kubernetes_version

  # API 端点对公网开放，但只放行操作员的 IP：本机要用 kubectl / helm；没有 VPN，私有端点连不上
  endpoint_public_access       = true
  endpoint_public_access_cidrs = [var.operator_cidr]

  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = true # 跑 terraform 的 IAM 用户就是操作员

  # 下面三项关掉模块的默认值：
  enable_irsa = false # 会建 OIDC provider，被 SCP 拒绝
  # 模块默认会建一个 KMS key 加密 Secret；destroy 后 key 要"等待删除"7–30 天，演示环境不值得。
  # TODO(edge): 不加密 etcd 里的 Secret（EKS 默认的 AWS 托管密钥加密仍然有）；正式环境打开
  create_kms_key    = false
  encryption_config = null
  # 控制面日志的 CloudWatch 日志组 destroy 时容易留下来继续计费；排障时再打开
  create_cloudwatch_log_group = false
  enabled_log_types           = []

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  addons = {
    # before_compute：网络和 Pod Identity 代理要在节点加入之前就绪，否则节点 NotReady / Pod 拿不到权限
    vpc-cni = {
      before_compute = true
      # 打开网络策略：chart 里的 NetworkPolicy（01 §5.1）在 EKS 上默认不执行
      configuration_values = jsonencode({ enableNetworkPolicy = "true" })
    }
    eks-pod-identity-agent = { before_compute = true }
    kube-proxy             = {}
    coredns                = {}
    # 1.30 起新集群的 gp2 不再是默认存储类，而且必须装这个插件 PVC 才能分到卷（S6）
    aws-ebs-csi-driver = {
      pod_identity_association = [{
        role_arn        = aws_iam_role.ebs_csi.arn
        service_account = "ebs-csi-controller-sa"
      }]
    }
  }

  eks_managed_node_groups = {
    default = {
      ami_type       = "AL2023_x86_64_STANDARD" # 镜像按 linux/amd64 构建（deploy/docker/Dockerfile）
      instance_types = [var.node_instance_type]
      min_size       = 3
      max_size       = 3
      desired_size   = 3
    }
  }
}

# ---------- 镜像仓库 ----------
resource "aws_ecr_repository" "sentinelops" {
  name                 = "sentinelops"
  image_tag_mutability = "IMMUTABLE" # tag 是 git 提交，同一个 tag 不能换内容
  force_delete         = true        # 仓库里有镜像也能 destroy；镜像随时可以从源码重建
}

resource "aws_ecr_lifecycle_policy" "sentinelops" {
  repository = aws_ecr_repository.sentinelops.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "只保留最近 5 个镜像"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}
