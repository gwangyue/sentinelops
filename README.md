# sentinelops

一个运维 Agent：**确定性检测 → LLM 诊断 → 人审批 → 受控执行 → 验证 → 沉淀**。围绕 Elasticsearch 构建，用"专家包"扩展到 MySQL。

核心立场：**LLM 只负责"想"（诊断、提议），所有"做"的事和安全相关的决定都在它够不着的地方。** 巡检时集群健康就不调用 LLM；诊断的每条论断都要引用工具输出原文，由代码校验；修复方案的哈希、审批、执行权限全部由确定性代码和独立进程负责。

```
 检测器（确定性，不调 LLM）──有问题──▶ 诊断 Agent（LangGraph + Deep Agents，只读工具）
                                          │ 假设 + 证据（观测 ID + 原文片段，代码校验）
                                          ▼
                                     plan：LLM 只输出 3 个字段的提议 ──▶ 白名单 + schema 校验
                                          │ 方案哈希、预演、影响范围（代码生成）
                                          ▼
                                     人审批（interrupt）──▶ 独立执行器（唯一持有写凭证，禁止外网）
                                          │                     重算哈希、查审批和幂等、重做前置检查
                                          ▼
                                     验证（失败自动回滚）──▶ 人确认根因 ──▶ 写入事故记忆
 旁路：审计哈希链 · 紧急停止 · 回放评测 · 治理周报 · MCP 只读服务
```

## 做到了什么（附证据）

| 方面 | 结果 | 证据 |
|---|---|---|
| 受控修复 | 修复模式评测 72 次运行，**不安全提议 0 次**；可修复场景 48/48 修好 | [M3 报告](docs/reports/M3-report.md) §5 |
| 证据约束 | 诊断里的每条论断都要引用观测原文，引用不存在或对不上就判为无证据 | [06 §3.1](docs/design/06-eval.md) |
| 可扩展 | 加入 MySQL 专家包（5 个工具、3 个检测器、1 个动作）时，core 和执行器**零改动**（相对通用化提交 `m4-base`） | [M4 报告](docs/reports/M4-report.md) §1 |
| 评测 | 回放模式评测（录一次、反复回放）；按场景聚类的 bootstrap 区间；数据集带版本标签；收敛曲线；纯规则基线对照 | [06 篇](docs/design/06-eval.md)、[M2 报告](docs/reports/M2-report.md) |
| 上云 | `make aws-up / aws-patrol / aws-down`：一条命令建 EKS、在云上跑一次调查、拆干净并检查残留。一次完整演示约 $1 | [M4 报告](docs/reports/M4-report.md) §2 |
| 治理 | 审计哈希链 + 锚定；紧急停止；周报的每个数字都能用 SQL 从审计表重算 | [07 篇](docs/design/07-governance.md) |

**没做成的也写在这里**：记忆实验（M-E1）没有测出显著的诊断增益，所以记忆层（L5）默认关闭；场景都来自 3 节点的测试集群，偏简单，评测证明的是"安全地动手"，还不能说明"诊断得更准"。详见各里程碑报告的"风险"一节。

## 快速开始

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
make check        # lint + 类型检查 + 依赖方向检查 + 251 个测试；不联网、不调用真实 LLM
```

再往下需要真实的集群和 Gemini API key（`GOOGLE_API_KEY`）：

| 形态 | 命令 | 用途 |
|---|---|---|
| 本地全链路 | `make kind-up`，然后 `sentinel patrol es-target` | kind + ECK + Chaos Mesh，本机约需 10 GiB 内存；`sentinel bench inject <场景>` 注入故障 |
| 回放 | `make compose-up`，然后 `sentinel eval run --pack es --split dev --dataset-tag <标签>` | 评测用。**回放录制不在仓库里**（约 8MB，仓库只放索引 `fixtures/index.jsonl`），要先在 kind 上用 `sentinel bench record` 录制 |
| AWS EKS | `make aws-up && make aws-patrol && make aws-down` | 悉尼区域，约 $0.6/小时，**用完务必 `make aws-down`**；需要 terraform、AWS 凭证 |

`sentinel --help` 列出全部命令（巡检、调查、审批、评测、审计、周报、MCP 等）。

## 安全边界

- **LLM 生成不了安全相关字段**：方案哈希、审批 ID、方案 ID、幂等键都由代码生成；LLM 能碰到的修复内容只有"动作、参数、依据哪个假设"3 个字段。
- **动作只能从目录里选**：目录里没有删除类动作；ES 执行器账号删除索引、读写文档等操作实测返回 403。
- **写凭证只在执行器里**：Agent 的进程和 Secret 里只有只读账号；执行器是独立进程，只信任 Postgres 里的审批记录，云上禁止访问外网。
- **这些规则由工具强制**：依赖方向（import-linter）、契约修改（hook）、写凭证位置、模型型号不能用别名等都有检查，违反时 `make check` 失败。

## 数据流向

工具输出的摘要会发送到 **Gemini API**（诊断）和 **LangSmith**（trace、数据集、评测结果），它们在美国或欧盟区域。作为作品项目，被管系统是测试集群，没有真实的敏感数据；用在企业环境里需要加脱敏钩子（[07 §6](docs/design/07-governance.md)）。

LangSmith 的 trace 需要显式打开：`LANGSMITH_TRACING=true`、`LANGSMITH_PROJECT=sentinelops`。

## 文档导航

| 想了解 | 读这里 |
|---|---|
| 项目目标和不做什么 | [00 总览](docs/design/00-overview.md) |
| 架构、部署形态、凭证矩阵 | [01 架构](docs/design/01-architecture.md) |
| 端到端演示（10 个步骤） | [golden-demo](docs/design/golden-demo.md) |
| 契约（数据结构） | [03 契约](docs/design/03-contracts.md) |
| 上下文分层、记忆、评测、治理 | [04](docs/design/04-context.md)、[05](docs/design/05-memory.md)、[06](docs/design/06-eval.md)、[07](docs/design/07-governance.md) |
| 架构决策 | [ADR](docs/design/adr/) |
| 每个里程碑做了什么、怎么验收 | `docs/reports/M1`–`M4` 的报告和代码导览 |

## 技术栈

LangGraph（外层流程）+ Deep Agents（诊断）· Gemini · FastAPI · Postgres · Elasticsearch（被管系统 + 记忆库）· LangSmith · kind / ECK / Chaos Mesh · Helm · Terraform · AWS EKS
