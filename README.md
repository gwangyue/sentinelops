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
 旁路：审计哈希链 · 紧急停止 · 回放评测 · 治理周报 · MCP 只读服务 · 飞书 ChatOps（推送、卡片审批、固定指令）
```

## 做到了什么（附证据）

| 方面 | 结果 | 证据 |
|---|---|---|
| 受控修复 | 修复模式评测 72 次运行，**不安全提议 0 次**；可修复场景 48/48 修好 | [M3 报告](docs/reports/M3-report.md) §5 |
| 证据约束 | 诊断里的每条论断都要引用观测原文，引用不存在或对不上就判为无证据 | [06 §3.1](docs/design/06-eval.md) |
| 可扩展 | 加入 MySQL 专家包（5 个工具、3 个检测器、1 个动作）时，core 和执行器**零改动**（相对通用化提交 `m4-base`） | [M4 报告](docs/reports/M4-report.md) §1 |
| 评测 | 回放模式评测（录一次、反复回放）；按场景聚类的 bootstrap 区间；数据集带版本标签；收敛曲线；纯规则基线对照 | [06 篇](docs/design/06-eval.md)、[M2 报告](docs/reports/M2-report.md) |
| 上云 | `make aws-up / aws-patrol / aws-down`：一条命令建 EKS、在云上跑一次调查、拆干净并检查残留。一次完整演示约 $1 | [M4 报告](docs/reports/M4-report.md) §2 |
| 飞书 ChatOps | 群里收告警和审批卡片、点按钮批准、发固定指令；kind 和 EKS 上都从发现问题跑到验证通过。执行器不知道审批来自飞书，安全规则不放松 | [M5 报告](docs/reports/M5-report.md) §2、[09 篇](docs/design/09-chatops.md) |
| 治理 | 审计哈希链 + 锚定；紧急停止；周报的每个数字都能用 SQL 从审计表重算 | [07 篇](docs/design/07-governance.md) |

**没做成的也写在这里**：记忆实验（M-E1）没有测出显著的诊断增益，所以记忆层（L5）默认关闭；场景都来自 3 节点的测试集群，偏简单，评测证明的是"安全地动手"，还不能说明"诊断得更准"。详见各里程碑报告的"风险"一节。

## 快速开始（Quick Start）

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。下面按"不需要集群 → 本地 kind → 飞书 → AWS"逐步往下，每一步都可以停下。

### 0. 离线检查（不需要集群、不联网）

```bash
uv sync
make check        # lint + 类型检查 + 依赖方向检查 + 296 个测试；不联网、不调用真实 LLM
make demo-check   # golden-demo 第 1–11 步（回放 + 假 LLM + 假飞书）
```

### 1. 本地全链路：kind（巡检 → 诊断 → 审批 → 执行 → 验证 → 确认）

需要 Docker（约 10 GiB 内存）、kubectl、helm、kind，以及环境变量 `GOOGLE_API_KEY`（诊断）和 `LANGSMITH_API_KEY`（trace，可选）。

```bash
make kind-up                       # kind + ECK + Chaos Mesh + es-target（3 节点）+ mysql-target
make compose-up                    # Postgres（审批、审计、checkpoint）+ es-memory（记忆库）
make kind-remount                  # 仅当 Docker 重启后 es-target 报 master not discovered 时
```

**两个 shell，凭证分开**（写凭证只在执行器的 shell 里，ADR-005）：

```bash
# shell A：执行器（唯一持有写凭证的进程）
source deploy/kind/exec-env.sh && uv run python -m sentinel.exec
```

```bash
# shell B：Agent 和 CLI（只读凭证）；巡检 mysql-target 还要 source deploy/kind/mysql-env.sh
source deploy/kind/dev-env.sh
uv run sentinel version freeze --note "本地：说明这一版改了什么"   # 改过 src/ config/ 之后要先提交、再冻结，否则巡检会被拒绝
uv run sentinel patrol es-target                                  # 健康时不调用 LLM："健康，0 个问题"
```

注入故障，走一遍修复：

```bash
uv run sentinel bench inject es.disk_watermark.single             # 注入并等症状出现（约 1–2 分钟）
uv run sentinel patrol es-target --mode remediate                 # 诊断 + 提议修复，停在审批
uv run sentinel investigations show <inv>                         # 假设、置信度、证据（观测 ID + 原文）
uv run sentinel investigations context <inv>                      # 上下文报告：每层用了多少 token、删了什么
uv run sentinel approvals list
uv run sentinel approvals show <apv>                              # 动作、参数、预演、回滚、验证条件、方案哈希
uv run sentinel approvals approve <apv> --as <你的名字>           # 或 approvals reject；执行器重新校验后执行，然后验证
uv run sentinel investigations confirm <inv> --root-cause H1 --as <你的名字>   # 确认根因，写入记忆
uv run sentinel bench revert es.disk_watermark.single             # 撤销故障，等集群恢复 green
```

治理和审计：

```bash
uv run sentinel freeze --reason "..." --as <你的名字>             # 紧急停止：执行器拒绝所有动作，待审批全部作废
uv run sentinel unfreeze --reason "..." --as <审批人>
uv run sentinel audit verify --since 1d                           # 校验审计哈希链
uv run sentinel report weekly --show-sql                          # 治理周报，每个数字附 SQL
uv run sentinel mcp serve --as claude                             # 给外部 Agent 的 MCP 只读服务（stdio）
```

`uv run sentinel bench inject` 可用的场景见 `src/sentinel/packs/<pack>/scenarios/`；`uv run sentinel --help` 列出全部命令。

### 2. 回放评测（不需要集群）

```bash
make compose-up
uv run sentinel eval run --pack es --split dev --dataset-tag <标签> --reps 3   # 标签必填，不能是 latest
```

**回放录制不在仓库里**（约 8MB，仓库只放索引 `fixtures/index.jsonl`），要先在 kind 上录：`sentinel bench record <场景> --pack es`（这个 shell 需要同时 source `dev-env.sh` 和 `exec-env.sh`）。

### 3. 飞书 ChatOps（dev bot，配合第 1 步的 kind）

在飞书开放平台建一个自建应用（开机器人、长连接订阅 `im.message.receive_v1` 和卡片回调 `card.action.trigger`），把 bot 拉进测试群，然后写凭证文件（**放在仓库之外**，`chmod 600`）：

```
# ~/.config/sentinelops/feishu-dev.env（纯 KEY=VALUE，不要 export、不要引号）
FEISHU_APP_ID=cli_...
FEISHU_APP_SECRET=...
FEISHU_CHAT_ID=oc_...
```

审批人的 open_id 写进 `config/chatops.dev.yaml` 的 `approvers`（不知道自己的 open_id：先空着启动，在群里 @ 机器人，拒绝日志里会打出来）。

```bash
# shell B（已 source dev-env.sh）；执行器照常在 shell A
uv run sentinel chatops serve --env-file ~/.config/sentinelops/feishu-dev.env
```

在群里（应用没开"获取群组中所有消息"权限时，要从 @ 列表里选中机器人再发）：

| 指令 | 作用 |
|---|---|
| `/help` | 帮助卡片：模板、示例，每个目标一个「巡检」按钮、「待审批列表」按钮 |
| `/patrol es-target` | 巡检（remediate 模式）：有问题就推送"发现异常"和审批卡片，在卡片上点「批准」 |
| `/inv list` · `/inv show <inv>` | 最近的调查 / 调查摘要 |
| `/approvals list` | 每条待审批重新发一张审批卡片 |
| `/bench inject\|revert <场景>` | 注入 / 撤销故障（只在 dev 开放） |
| `/freeze <原因>` | 默认拒绝；`high_risk_enabled: true` 时才能用。**飞书永远不能解冻**，确认根因也只在 CLI |

### 4. AWS EKS（悉尼）

需要 terraform、AWS 凭证、`GOOGLE_API_KEY`、`LANGSMITH_API_KEY`。**开始计费约 $0.95/小时，用完务必 `make aws-down`。** 镜像在本地构建、推到 ECR，tag 是当前提交号，所以 `src/ config/` 必须先提交。

```bash
make aws-up        # VPC、EKS、ECR、镜像、Helm（agent-api · executor · chatops）、Secret；约 20–30 分钟
make aws-patrol    # 在云上冻结版本、注入 allocation_disabled、跑一次调查、撤销
make aws-down      # 先删 PVC 和 EBS 卷，再 terraform destroy，最后按类型查残留
```

飞书 prod bot（可选）：另建一个飞书应用和值班群，凭证写进 `~/.config/sentinelops/feishu-prod.env`（同上三个键），审批人写进 `config/chatops.prod.yaml`。`aws-up` 会检查这个文件，并用 `kubectl --from-env-file` 建 Secret `sentinel-feishu`（不经过 helm）。prod 没有 `/bench`；云上没有部署 mysql-target。

```bash
kubectl --context sentinel-aws -n sentinel-system logs -f deploy/chatops                    # 看飞书入口的日志
kubectl --context sentinel-aws -n sentinel-system exec deploy/agent-api -- sentinel approvals list   # 云上也能用 CLI 审批
```

## 安全边界

- **LLM 生成不了安全相关字段**：方案哈希、审批 ID、方案 ID、幂等键都由代码生成；LLM 能碰到的修复内容只有"动作、参数、依据哪个假设"3 个字段。
- **动作只能从目录里选**：目录里没有删除类动作；ES 执行器账号删除索引、读写文档等操作实测返回 403。
- **写凭证只在执行器里**：Agent 的进程和 Secret 里只有只读账号；执行器是独立进程，只信任 Postgres 里的审批记录，云上禁止访问外网。
- **这些规则由工具强制**：依赖方向（import-linter）、契约修改（hook）、写凭证位置、模型型号不能用别名等都有检查，违反时 `make check` 失败。

## 数据流向

工具输出的摘要会发送到 **Gemini API**（诊断）和 **LangSmith**（trace、数据集、评测结果），它们在美国或欧盟区域。用了飞书 ChatOps 时，审批卡片（动作、参数、前置检查）和异常摘要会发到**飞书**（国内飞书 open.feishu.cn），只放摘要和 ID，不贴原始观测（[09 §8](docs/design/09-chatops.md)）。作为作品项目，被管系统是测试集群，没有真实的敏感数据；用在企业环境里需要加脱敏钩子（[07 §6](docs/design/07-governance.md)）。

LangSmith 的 trace 需要显式打开：`LANGSMITH_TRACING=true`、`LANGSMITH_PROJECT=sentinelops`。

## 文档导航

| 想了解 | 读这里 |
|---|---|
| 项目目标和不做什么 | [00 总览](docs/design/00-overview.md) |
| 架构、部署形态、凭证矩阵 | [01 架构](docs/design/01-architecture.md) |
| 端到端演示（11 个步骤） | [golden-demo](docs/design/golden-demo.md) |
| 飞书 ChatOps | [09 篇](docs/design/09-chatops.md)、[ADR-015](docs/design/adr/) |
| 契约（数据结构） | [03 契约](docs/design/03-contracts.md) |
| 上下文分层、记忆、评测、治理 | [04](docs/design/04-context.md)、[05](docs/design/05-memory.md)、[06](docs/design/06-eval.md)、[07](docs/design/07-governance.md) |
| 架构决策 | [ADR](docs/design/adr/) |
| 每个里程碑做了什么、怎么验收 | `docs/reports/M1`–`M5` 的报告和代码导览 |

## 技术栈

LangGraph（外层流程）+ Deep Agents（诊断）· Gemini · FastAPI · Postgres · Elasticsearch（被管系统 + 记忆库）· LangSmith · 飞书开放平台（lark-oapi，长连接） · kind / ECK / Chaos Mesh · Helm · Terraform · AWS EKS
