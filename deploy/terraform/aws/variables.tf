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
  # 01 §5.2：3 台能放下 es-target 3 节点和其他所有组件；跑通之后可以试 m6i.large（S6）
  type    = string
  default = "m6i.xlarge"
}
