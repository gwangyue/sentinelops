# 版本写死，只用发布满两周的版本（spike-results.md 的规则）：aws 6.72.0（2026-11-04）、tls 4.4.2（10-21）、time 0.14.2（09-11）。
# tls / time 是 EKS 模块的依赖，也一起写死。
terraform {
  required_version = ">= 1.10" # S3 后端的 use_lockfile 从 1.10 开始支持，不再需要 DynamoDB 锁表

  required_providers {
    aws  = { source = "hashicorp/aws", version = "6.72.0" }
    tls  = { source = "hashicorp/tls", version = "4.4.2" }
    time = { source = "hashicorp/time", version = "0.14.2" }
  }

  # state 放 S3（不放本机）：state 丢了，destroy 就找不到要删的资源。
  # bucket 名带账号 ID，由 aws-up.sh 用 -backend-config 传入（后端配置里不能用变量）
  backend "s3" {
    key          = "sentinelops/eks.tfstate"
    region       = "ap-southeast-2"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region
  # 所有资源都带这个标签：destroy 之后按标签检查有没有残留（make aws-down 的最后一步）
  default_tags {
    tags = { project = "sentinelops", managed-by = "terraform" }
  }
}
