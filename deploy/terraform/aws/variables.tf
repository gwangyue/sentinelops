variable "region" {
  # 不是东京：账号所在组织的 SCP 只放开了悉尼（ADR-007 修订、spike-results S6 补充）
  type    = string
  default = "ap-southeast-2"
}

variable "name" {
  type    = string
  default = "sentinel"
}

variable "kubernetes_version" {
  # 和本地 kind 一致（ADR-007 同构）；ECK 3.5.0 在 1.37 上实测可用（spike-results 的环境说明）
  type    = string
  default = "1.37"
}

variable "operator_cidr" {
  # 集群 API 只对操作员本机的公网 IP 开放（kubectl 要连）；aws-up.sh 自动取当前 IP
  type = string
}

variable "node_instance_type" {
  # 原计划 m6i.xlarge（01 §5.2）。D16 实测：账号是免费套餐（Free account plan），只能启动符合免费条件的实例类型，
  # m6i.xlarge 被拒（RunInstances: "not eligible for Free Tier"）。符合条件里最大的是 m7i-flex.large（2 vCPU / 8 GiB，x86），
  # 3 台 24 GiB，整套组件的内存 requests 约 9 GiB，放得下
  type    = string
  default = "m7i-flex.large"
}
