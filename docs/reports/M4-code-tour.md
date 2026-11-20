# M4 代码导览：扩展与上云

> 按 `docs/design/templates/code-tour.md` 生成。M1–M3 的导览沿着"一次调查"走；M4 的改动不在调查主路径上，而在两条"外围"路线：**一个新领域怎么接进来**（①–④），以及**同一次调查怎么搬到云上跑**（⑤–⑨）。30–40 分钟读完。
> 读之前记住一句话：**扩展靠"专家包 + 连接注册表"，core 不认识任何具体领域；上云靠"同一个镜像 + 不同的 values"，代码不知道自己跑在哪里。**

## 1. 路线图

```
路线 A：加一个领域（MySQL）                         路线 B：搬到云上（EKS 悉尼）
                                                   make aws-up
packs/mysql/pack.yaml                                 │
   │  ① discover_packs() 扫描目录                     ▼
   ▼                                               ⑤ Terraform：VPC · EKS · Pod Identity · ECR
core/packs_registry.py ──▶ 工具 · 检测器 · 动作       │
   │                                                  ▼
   │  ② connection.kind = mysql                    ⑥ 镜像：带 .git，容器里算出同一个 code_sha
   ▼                                                  │
sdk/connect.py ──▶ ReadOnlySql / WriteSql              ▼
   │                                               ⑦ Helm chart：密码、两份 Secret、网络策略
   ▼                                                  │
③ 检测器 → 诊断 → 动作（mysql_actions.py）             ▼
   │                                               ⑧ aws-patrol：在 agent-api 的 Pod 里跑 sentinel patrol
   ▼                                                  │
④ 对外：report weekly（SQL 重算）· mcp serve（只读）   ▼
                                                   ⑨ aws-down：先删 PVC，再 destroy，直接查残留
```

## 2. 逐站讲解

### ① 专家包发现：core 不认识"ES"

| 项 | 内容 |
|---|---|
| 位置 | `core/packs_registry.py:65` `discover_packs`、`:49` `load_pack`；`core/graph/run.py` `load_packs` |
| 人话 | core 不再写死"有哪些领域"，而是扫描 `sentinel/packs/*/pack.yaml`：有几个目录就有几个专家包。加 MySQL 就是加一个目录 |
| 输入 / 输出 | `PackManifest`（03 §3.1）→ `LoadedPack`（工具、检测器、动作规格、场景） |
| 关键概念 | **零改动验收**（加新领域时 core 和执行器的代码一行不改，用来证明扩展点设计对了）；**`m4-base`**（m3 里有 4 处写死 ES，先做一次只改这 4 处的提交，零改动从它开始算） |
| 为什么这样设计 | 00 §5 和 01 §4："可扩展到 MySQL"是项目目标之一；ADR-001 的分层 |
| 可能出错的地方 | 写死的地方不止 core：D13 发现 CLI 的 `approvals show`、`investigations confirm` 也写死了 es，MySQL 演示时报错（已修，CLI 不在零改动范围内） |
| 面试可能被问 | 问：怎么证明你的架构真的可扩展？答：先加一个领域，要求 core/exec 的 diff 为空；第一次做不到，找出 4 处耦合，单独提交改掉，再从那里开始算——"做不到"本身就是最有价值的发现 |

### ② 连接注册表：一个 kind，一对连接

| 项 | 内容 |
|---|---|
| 位置 | `sdk/connect.py:30` `connector`、`:36` `open_read`、`:40` `open_write`；执行器取写连接 `exec/live.py:31` `write_connection` |
| 人话 | 专家包在清单里写 `connection.kind: mysql`，注册表据此给出只读连接（`ReadOnlySql`，会话设成只读事务）或写连接（`WriteSql`）。core 只拿只读的，执行器只拿写的 |
| 输入 / 输出 | `connection.kind` + 凭证名 → 连接对象；凭证来自环境变量 `SENTINEL_CRED_<名字>_{URL,USER,PASSWORD}` |
| 关键概念 | **凭证名**（清单里只写名字，不写密码；03 §3.1"凭证本身不写在清单里"） |
| 为什么这样设计 | ADR-005：写凭证只在执行器进程里解析；08 §6.4 有测试检查"写凭证只在 exec/" |
| 可能出错的地方 | MySQL 的 `SYSTEM_VARIABLES_ADMIN` 不能限定到单个变量，执行器账号也能改 `read_only`（D13 实测）；动作目录里没有这个动作，靠"执行器只按规格执行"约束（01 §6） |
| 面试可能被问 | 问：只读账号为什么还要再设只读事务？答：两层防线——账号权限防"能不能"，只读事务防"万一账号给多了"；任何一层单独失效都不会写入 |

### ③ MySQL 的检测器和动作：照着 ES 的样子

| 项 | 内容 |
|---|---|
| 位置 | `packs/mysql/detectors/connections.py:10` `detect`；`packs/mysql/actions/mysql_actions.py:20` 前置检查、`:32` 预演、`:40` 执行、`:45` 回滚、`:50` 验证；规格 `mysql.raise_max_connections_temporarily.yaml` |
| 人话 | 检测器算连接使用率，超过阈值产出问题事件；唯一的动作是临时调高 `max_connections`，前置检查限定不超过原值 2 倍，回滚恢复原值 |
| 输入 / 输出 | `DetectContext` → `Finding`；`ActionContext` / `ExecContext` → `CheckResult`、执行结果、`VerifyResult`（03 §4） |
| 关键概念 | **动作的五件套**（前置检查、预演、执行、回滚、验证：M3 为 ES 定下的规格，MySQL 原样复用） |
| 为什么这样设计 | 锁等待、主库只读两个场景故意没有动作（杀会话、关只读都是高风险操作，交给人）：修复模式评测里模型每次都选"不动作" |
| 可能出错的地方 | 故障台在 Pod 里起几十个 mysql 客户端会把 MySQL 撑到 OOMKill（D13 实测两次），改成本机一个进程持有连接 |
| 面试可能被问 | 问：为什么动作目录这么小？答：目录小就是安全边界——模型只能在目录里挑；没有的动作，提示词注入也调不到 |

### ④ 对外的两个出口：周报和 MCP

| 项 | 内容 |
|---|---|
| 位置 | `governance/report.py:141` `audit_numbers`、`:79` `pg_runner`、`:88` `sqlite_runner`；CLI `cli/main.py:580` `report weekly`；`mcp/server.py:33` `build_server`、`:64` `exported_tools` |
| 人话 | 周报的每个数都是一条对审计表的 SQL，同一份 SQL 在 Postgres 和 sqlite 上都能跑；MCP 只导出清单里标了 `mcp_export: true` 的只读工具，调用方只能访问白名单里的目标，返回摘要，每次调用写审计 |
| 输入 / 输出 | `AuditEvent`（03 §5.1）→ `AuditNumbers`；MCP 请求 → 工具摘要 + `mcp.call` 审计 |
| 关键概念 | **可重算**（周报不存任何"统计结果"，只存原始审计，数字随时能重新算出来，golden-demo 第 10 步检查的就是这个） |
| 为什么这样设计 | 07 §8 周报；02 §8 MCP 只读；00 §5 不做清单里没有"对外开放写操作" |
| 可能出错的地方 | 成本不全：plan 节点的模型调用不在审计里，要补得改 core（零改动要求），周报里注明了 |
| 面试可能被问 | 问：外部 Agent 通过 MCP 能触发修复吗？答：不能。导出的只有只读工具，动作不导出；目标按调用方限制；每次调用都有审计 |

### ⑤ Terraform：关掉的几个默认值比打开的更重要

| 项 | 内容 |
|---|---|
| 位置 | `deploy/terraform/aws/main.tf:85` `module "eks"`（`:100` `enable_irsa = false`、`:117` `enableNetworkPolicy`、`:124` `pod_identity_association`）；`variables.tf:28` 节点类型 |
| 人话 | 用官方模块建 VPC（2 个可用区、1 个 NAT）、EKS 1.37、3 台节点、EBS CSI 插件。关掉模块默认会建的 OIDC provider（账号不允许）、KMS key（删除后要等 7–30 天）、日志组（destroy 时容易留下继续收费） |
| 输入 / 输出 | 变量（区域、操作员 IP、节点类型）→ 约 60 个 AWS 资源；state 放 S3 |
| 关键概念 | **SCP**（组织给成员账号加的限制，优先于 IAM，所以加权限绕不过去）；**Pod Identity**（让 Pod 拿 AWS 权限的方式，不需要 OIDC provider）；**tainted**（"创建了一半"的资源，下次 apply 先删再建） |
| 为什么这样设计 | 01 §5.2 的拓扑；ADR-007 修订：区域改悉尼、用 Pod Identity；S6：只在演示时开 |
| 可能出错的地方 | 账号是免费套餐，m6i.xlarge 不允许启动，节点组卡在 CREATING 却连 ASG 都没有——dry-run 和权限模拟都查不出，最后在 CloudTrail 里找到 `not eligible for Free Tier` |
| 面试可能被问 | 问：apply 卡住了怎么处理？答：先查节点组、实例、ASG 活动，再查 CloudTrail 里被拒的调用；停 apply 用 SIGINT（不是 kill -9），让 Terraform 把资源记成 tainted、释放锁，下次 apply 自动替换 |

### ⑥ 镜像：容器里算出同一个版本哈希

| 项 | 内容 |
|---|---|
| 位置 | `deploy/docker/Dockerfile:24`（先装依赖）、`:28`（拷代码和 .git）、`:31`（构建时断言工作区干净、打印 code_sha）；`deploy/aws/aws-up.sh:45` `image` |
| 人话 | agent-api、执行器、CLI 共用一个镜像，靠启动命令区分。镜像里带了 `.git`（约 5MB），所以容器里"代码对应哪个提交、有没有未提交的改动"的检查照常工作 |
| 输入 / 输出 | 工作区 → 镜像（tag = 提交号）→ ECR |
| 关键概念 | **code_sha**（`src/ config/ uv.lock pyproject.toml` 在 HEAD 里的对象哈希，ADR-012；版本靠它对应到一份确定的代码） |
| 为什么这样设计 | 不改 core 的前提下让版本检查在容器里成立；Mac 是 arm64、节点是 x86，所以构建 `linux/amd64` |
| 可能出错的地方 | `/app` 由 root 创建，非 root 用户写不了 `.venv`（第一次构建失败）；云上 Postgres 是新的，版本表为空，要在云上重新 `version freeze`（哈希和本地相同，编号从 v1 开始） |
| 面试可能被问 | 问：怎么保证云上跑的就是评测过的那版代码？答：版本哈希由代码内容决定；镜像构建时断言工作区干净，容器里算出的哈希和本机逐字相同（`sha256:3b9f3dd0…`），对不上就拒绝开始调查 |

### ⑦ Helm chart：凭证分两份，执行器出不了网

| 项 | 内容 |
|---|---|
| 位置 | `deploy/helm/sentinel/templates/_helpers.tpl:7` `sentinel.pw`；`app.yaml:10` `sentinel-agent-env`、`:62` `sentinel-exec-env`、`:87` `wait-schema`；`netpol.yaml:40` `exec-isolation` |
| 人话 | 密码第一次安装时随机生成，升级时读回原值。Agent 的 Secret 里只有只读账号，执行器的 Secret 在另一个命名空间、只有写账号。执行器的网络策略只放行被管系统、Postgres 和 DNS |
| 输入 / 输出 | `values.yaml`（kind 默认）/ `values-aws.yaml`（gp3 存储类）→ 4 个命名空间的全部组件 |
| 关键概念 | **NetworkPolicy**（K8s 的防火墙规则）；**initContainer**（先于主容器运行的容器，这里等 Agent 建好表再让执行器启动） |
| 为什么这样设计 | ADR-005 凭证隔离；01 §5.1、07 §3 T6 执行器禁止外网；es-memory 只关 HTTP 层 TLS、认证照常（账号密码放在连接串里，httpx 会转成 Basic 认证，不用改 core） |
| 可能出错的地方 | 独立审阅发现执行器原来没有网络策略（M3 起"以后补"）；kind 上实测还发现新 Pod 启动头几秒策略没生效。执行器先于 Agent 启动会因为没表而崩溃（D16 实测，加了 initContainer） |
| 面试可能被问 | 问：Agent 被提示词注入了，能拿到写密码吗？答：拿不到。写密码在另一个命名空间的 Secret 里，Agent 的 Pod 没挂它；Agent 能做的只是提议，执行要经过人审批和执行器的校验 |

### ⑧ aws-patrol：在云上跑一次调查

| 项 | 内容 |
|---|---|
| 位置 | `deploy/aws/aws-patrol.sh`；故障脚本 `deploy/chaos/es/allocation_disabled.sh`（`CTX` 可以覆盖） |
| 人话 | 在 agent-api 的 Pod 里依次执行：冻结版本、建知识索引、健康巡检（不调用 LLM）、注入故障、等检测器触发、跑一次调查、撤销故障 |
| 输入 / 输出 | `sentinel patrol es-target`（集群内地址、只读账号）→ 调查记录，状态 `diagnosed` |
| 关键概念 | **只验证能跑通**（你的决定：上云这两天不评测效果，所以只要求调查走完，不判断根因对错） |
| 为什么这样设计 | 选 `allocation_disabled`：只改集群设置再删一个 Pod，不需要 Chaos Mesh，也不需要在节点上填盘（`fill_disk.sh` 依赖 kind 节点） |
| 可能出错的地方 | 调查开始时 data-2 还没回来，H1 是"节点少一个"，注入的根因排在 H2；LangSmith 之前一直没有 CLI 巡检的 trace（缺 `LANGSMITH_TRACING=true`） |
| 面试可能被问 | 问：云上和本地的调查有什么不同？答：代码完全相同（同一个哈希）；不同的只有连接地址和凭证，都由环境变量注入 |

### ⑨ aws-down：拆干净比建起来更难

| 项 | 内容 |
|---|---|
| 位置 | `deploy/aws/aws-down.sh:11` `k8s_cleanup`、`:29` `leftovers` |
| 人话 | 先卸载 chart、删 PVC、等 EBS 卷删完，再 `terraform destroy`，最后按资源类型逐项查有没有残留 |
| 输入 / 输出 | → 0 个计费资源（state 桶保留） |
| 关键概念 | **动态卷不在 state 里**（PVC 触发 EBS CSI 创建的卷，Terraform 不知道它们存在；直接 destroy，卷会留下来继续收费） |
| 为什么这样设计 | S6：NAT、端点、控制面不足 1 小时按 1 小时算；忘了 destroy 一个月约 $430 |
| 可能出错的地方 | 标签 API 有延迟：destroy 刚结束还列出 12 个已删除的资源，所以残留判定改为以直接查询为准 |
| 面试可能被问 | 问：怎么确认云资源真的删干净了？答：三层——Terraform 的 destroy 结果、按类型直接查（集群、实例、NAT、EIP、端点、卷）、第二天看 Cost Explorer；标签 API 只作提示 |

## 3. 先预测，再运行

| # | 要运行的命令 | 请先预测 | 实际结果（运行后填写） |
|---|---|---|---|
| 1 | `git diff --stat m4-base..HEAD -- src/sentinel/core src/sentinel/exec` | 会有几个文件？是哪几类改动？ | |
| 2 | `helm template s deploy/helm/sentinel -f deploy/helm/sentinel/values-aws.yaml \| grep -A6 "name: sentinel-agent-env"` | Agent 的 Secret 里有哪几个键？有没有 `EXECUTOR` 字样？ | |
| 3 | `kubectl --context kind-sentinel -n sentinel-exec run t --rm -i --restart=Never --image=curlimages/curl:8.11.1 --command -- sh -c 'sleep 15; curl -s -o /dev/null --connect-timeout 5 https://1.1.1.1 && echo 通 \|\| echo 不通'` | 通还是不通？去掉 `sleep 15` 呢？ | |

## 4. 自测题

<details><summary>题 1：加一个 Redis 专家包，需要改哪些目录？</summary>

答案：只加 `sentinel/packs/redis/`（清单、工具、检测器、动作、场景）。如果 Redis 的连接类型是新的，还要在 `sdk/connect.py` 注册一个 kind；core 和 exec 不用改。

</details>

<details><summary>题 2：为什么镜像里要带 .git？不带会怎样？</summary>

答案：版本哈希和"工作区干净"的检查都调用 git（ADR-012）。不带 `.git`，容器里算不出 code_sha，`version freeze` 和巡检都会失败；要绕过就得改 core。

</details>

<details><summary>题 3：直接 `terraform destroy`，会留下什么在继续收费？</summary>

答案：PVC 动态创建的 EBS 卷。它们由集群里的 EBS CSI 驱动创建，不在 Terraform 的 state 里；集群删了，卷还在。所以 `aws-down` 先删 PVC、等卷删完再 destroy。

</details>

<details><summary>题 4：节点组卡在 CREATING 15 分钟，你会按什么顺序排查？</summary>

答案：节点组状态和 health issues → 有没有 EC2 实例 → ASG 的活动记录 → CloudTrail 里被拒绝的调用（这次是 `RunInstances: not eligible for Free Tier`）。

</details>

<details><summary>题 5：执行器的网络策略生效后，它能连 es-memory 吗？为什么这样设计？</summary>

答案：不能。执行器只需要被管系统（targets）、Postgres（读审批、写审计）和 DNS；记忆是 Agent 的事。持有写凭证的进程能连的地方越少，凭证泄露的出口就越少（07 §3 T6）。

</details>
