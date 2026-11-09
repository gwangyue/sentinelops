# ADR-007 本地用 kind + ECK + Chaos Mesh，云上用 AWS EKS，两边同构

- 状态：已接受（2026-11-09）
- 相关：01 篇 §5

## 背景

需要能在本地做集成测试，也能部署到云上。故障注入需要控制被管系统的节点。作者选定 AWS 作为云平台，同时希望补上 K8s 和 Terraform 方面的经验。

## 决策

- 本地：kind 集群，ES 用 ECK（Elastic 官方的 K8s operator）部署，故障注入用 Chaos Mesh 加脚本。
- 云上：AWS EKS，东京区域（ap-northeast-1），用 Terraform 创建，和本地用同一套 Helm chart，只换 values 文件。
- 只在演示时创建云上集群，用完就销毁。

## 放弃的方案

| 方案 | 放弃原因 |
|---|---|
| docker compose 运行全部组件 | 不方便做节点级故障注入；也和云上的部署方式不同构 |
| AWS ECS | 比 EKS 简单，但 ECK 和 Chaos Mesh 都依赖 K8s |
| Elastic Cloud | 无法注入节点级故障 |
| GCP | Gemini 原生支持和 Cloud Run 更省事；但东京的招聘市场更熟悉 AWS（作者的选择） |

## 后果

- 好处：本地和云同构；故障注入能力完整；顺带积累 K8s 和 Terraform 经验。
- 代价：本地内存占用较高（约 10–12 GiB【估算】）；EKS 的固定成本只适合按需开启。
