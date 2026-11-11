# M3 代码导览：受控修复

> 按 `docs/design/templates/code-tour.md` 生成。M1、M2 的导览讲的是"巡检 → 诊断 → 报告"；本篇沿着 **UC2 主路径**往后走：提议 → 审批 → 执行 → 验证 → 确认 → 写记忆。30–40 分钟读完。
> 读之前记住一句话：**LLM 只能"提议"，所有安全相关的字段和决定都在 LLM 够不着的地方。**

## 1. 路线图

```
sentinel patrol --mode remediate
   │
   ▼
diagnose ──▶ ① plan（investigation.py:264；remediate.py）
               LLM 只输出 ActionProposal ──▶ 白名单 + params_schema ──✗──▶ 不安全提议，升级
                                          │✓
               前置检查 · 预演 · 影响范围 · plan_hash（确定性代码）──▶ 写方案和审批请求（Postgres）
   │
   ▼
② approve：interrupt() ……… 人：sentinel approvals approve <apv> --as approver（record_decision）
   │
   ▼
③ execute：只把三个 ID 写进队列 ─────────▶ ④ 执行器进程 python -m sentinel.exec（exec/core.py:46）
                                            冻结？→ 哈希三处一致 → 审批通过且没过期 → 幂等 → 重做前置检查 → 执行
   ▼
⑤ verify：按 VerifySpec 轮询 ──✗──▶ 请求执行器回滚，升级
   │✓
⑥ confirm：interrupt() ……… 人：sentinel investigations confirm <inv> --root-cause H1
   ▼
⑦ memorize：只在确认之后写记忆 ──▶ report
旁路：⑧ sentinel freeze / unfreeze（kill_switch.py）· ⑨ sentinel audit verify（audit.py）· ⑩ sentinel eval gate（gate.py）
```

## 2. 逐站讲解

### ① plan：LLM 只能提议

| 项 | 内容 |
|---|---|
| 位置 | `core/graph/investigation.py:264` `plan`；`core/graph/remediate.py:78` `parse_proposal`、`:86` `validate_proposal`、`:105` `build_plan`；提示词 `core/diagnosis/prompts/plan.md` |
| 人话 | 让模型从动作目录里挑一个动作、填参数。模型的输出先过两道确定性检查：动作在不在白名单、参数符不符合 schema。不符合就算一次"不安全提议"，直接升级给人。符合的话，由代码做前置检查、预演、影响范围检查，算出方案哈希 |
| 输入 / 输出 | 模型输出 `ActionProposal`（3 个字段，ADR-013）；代码组装 `RemediationPlan` 和 `ApprovalRequest`（03 §4.3–4.4） |
| 关键概念 | **plan_hash**（方案指纹：动作、目标、参数、前置检查快照的规范化 JSON 的 sha256；审批批的就是这个指纹，方案改一个字都要重新审批） |
| 为什么这样设计 | CLAUDE.md"不要让 LLM 生成 plan_hash"；ADR-013：LLM 能碰到的修复相关字段只有 3 个 |
| 可能出错的地方 | 模型不知道前置检查里的取值规则：v32 评测里它把分片上限提到 20（规则最多 2 倍），被前置检查拦下。v35 在参数 schema 里加了 `description`，规则对模型可见 |
| 面试可能被问 | 问：提示词注入能让 Agent 删索引吗？答：动作目录里没有删除动作，白名单校验会拦下；即使绕过，执行器的 ES 账号也没有删除权限（实测 403） |

### ② approve 和 record_decision：审批不经过 LLM

| 项 | 内容 |
|---|---|
| 位置 | `investigation.py:329` `approve`（只有 `interrupt()`）；`remediate.py:189` `record_decision`；`remediate.py:151` `check_approval`（在 execute 节点里调用）；CLI `approvals approve` |
| 人话 | 图停下来等人。人在命令行里批准或拒绝，决定写进 Postgres，同时记审计 `approval.decided`，然后恢复同一次调查（同一个 thread_id 从 checkpoint 找回状态） |
| 输入 / 输出 | 输入：plan 节点写好的 `ApprovalRequest`（03 §4.4：方案哈希、有效期 30 分钟）；人给出的 `ApprovalDecision`（批准 / 拒绝、审批人、原因）。输出：审计 `approval.decided`；恢复时 resume 只带"已恢复"，不带任何内容 |
| 关键概念 | **interrupt**（LangGraph 暂停图的方式：抛一个特殊异常，恢复时节点从第一行重新执行。所以节点里不能有副作用，也不能被宽泛的 except 吞掉——有 lint 规则检查）；**checkpoint**（图的状态存在 Postgres，CLI 是另一个进程，也能用同一个 thread_id 恢复） |
| 为什么这样设计 | 02 §3.4：resume 只带"已恢复"，审批结果在下一个节点里从存储读，模型伪造不了审批；写入（方案、审批请求）放在 plan 节点，因为恢复时 approve 节点会重新执行 |
| 可能出错的地方 | 同一个审批决定两次：存储的唯一约束挡住（`DuplicateError`）；审批过期（超过 30 分钟）：execute 节点读到之后记 `approval.expired`，不执行；审批人和发起人是同一个人：07 §4 第一版不强制，只记进审计 |
| 面试可能被问 | 问：模型被注入了恶意指令，能不能自己"批准"一个方案？答：不能。审批只能通过 CLI 写进 Postgres，resume 不带内容；execute 节点和执行器都从存储读决定，模型的输出里没有任何地方会被当作审批 |

### ③④ execute 和执行器：唯一有写权限的地方

| 项 | 内容 |
|---|---|
| 位置 | `investigation.py:334` `execute`；`core/graph/run.py:142` `queue_executor`；`exec/core.py:46` `process`；`exec/__main__.py`；`exec/live.py` |
| 人话 | 调查引擎只把三个 ID 写进队列；独立的执行器进程自己去读方案和审批，依次检查：系统有没有冻结、哈希三处是否一致、审批是否通过且没过期、这个幂等键是否已经执行过、前置检查现在是否还满足——都过了才执行 |
| 输入 / 输出 | 输入：`execution_requests` 里的 `(plan_id, approval_id, idempotency_key)`（ADR-005：执行器只接收这三个 ID），其余从 Postgres 读 `RemediationPlan`、`ApprovalRequest`、`ApprovalDecision`。输出：`ExecutionResult`（03 §4.4，`succeeded / failed / precheck_failed / duplicate`）、审计 `action.executed` |
| 关键概念 | **幂等键**（hash(调查 ID, plan_hash)：恢复、重试时同一个方案不会执行两次）；**TOCTOU**（"检查时满足、执行时已不满足"：所以执行前要再查一次）；**哈希三处一致**（方案记录里的、审批请求里的、执行器重算的） |
| 为什么这样设计 | ADR-005：写凭证只在执行器进程里；`exec` 不能 import `core`（import-linter 检查）；执行器不信任调查引擎传来的任何东西 |
| 可能出错的地方 | 执行器没启动：调查等 300 秒后升级。回放评测里用 `eval/replay_exec.py` 在进程内调用同一个 `process`，只把"执行"换成"切到录制的 post 阶段"；动作函数抛异常时记为 `failed`，执行器进程不退出 |
| 面试可能被问 | 问：为什么回放的执行器不另写一个简单版本？答：校验逻辑是安全的核心，评测必须测到真实的那一份 |

### ⑤ verify：验证失败就回滚

| 项 | 内容 |
|---|---|
| 位置 | `investigation.py:353` `verify`；`remediate.py:164` `run_verify`；回滚请求的幂等键 `remediate.py:180` `execution_keys` |
| 人话 | 按动作声明的验证条件轮询（例如集群变绿）；超时没达成，就请执行器按前置检查快照把设置恢复原值，然后升级给人 |
| 输入 / 输出 | 输入：动作规格的 `VerifySpec`（03 §4.2：验证函数、超时、轮询间隔）和只读工具；输出：审计 `verify.passed` / `verify.failed`；失败时再交给执行器一个 `rollback` 请求，审计 `action.rolled_back` |
| 关键概念 | **回滚有自己的幂等键**（hash(调查 ID, plan_hash#rollback)：执行和回滚各只能发生一次）；**回滚不查审批有效期**（回滚是恢复原状，审批过期之后也必须能做） |
| 为什么这样设计 | 02 §3.4"验证失败就回滚：回滚动作在动作目录里预先定义，不让 LLM 临场想办法补救"；验证用的是只读工具，和诊断走同一个后端 |
| 可能出错的地方 | 回放的状态不会变，按时间轮询只会空转到超时——回放时只检查一次（测试时踩过）；每次轮询前要清掉工具调用的缓存，否则读到的是执行之前的结果 |
| 面试可能被问 | 问：验证通过了，就说明根因找对了吗？答：不一定。修好了只说明动作有效，所以验证之后还有一个人确认根因的 confirm 节点，确认过的才写进记忆 |

### ⑥⑦ confirm 和 memorize：修好了不代表诊断对

| 项 | 内容 |
|---|---|
| 位置 | `investigation.py:371` `confirm`、`:375` `memorize_node`；`core/graph/confirm.py:45` `memorize`、`:90` `record_confirmation`；CLI `investigations confirm` |
| 人话 | 修好之后再停一次，等人确认根因。确认（或修正）写进审计，memorize 节点从审计里读这个决定，才写记忆；人修正过的记忆标记 `corrected=true`。不确认就不写 |
| 输入 / 输出 | 输入：人的确认（`--root-cause H1` 或 `--label <标签>`，只能是专家包声明过的根因标签）；输出：审计 `rootcause.confirmed` / `rootcause.corrected` → `memory.written`，一条 `MemoryRecord`（03 §6.4；`source_investigations` 包含这次调查） |
| 关键概念 | **修正**（人给出的根因和 H1 不同，记忆里 `corrected=true`——05 §3.1"这类记忆最有价值"）；**反馈**（这次 L5 里出现过的记忆，按确认的根因更新命中、有用、伤害次数） |
| 为什么这样设计 | 05 §3.1：模型的结论直接写进记忆，一次误诊就会变成"经验"，错误会自我强化；确认结果从审计里读，而不是从 resume 里拿，和审批同一个道理 |
| 可能出错的地方 | 模型把假设编号写成 `h1`：确认命令现在不区分大小写（kind 上实测发现）；评测里没有人来确认，所以评测不走确认段（`Deps.memorizer` 为空） |
| 面试可能被问 | 问：人一直不确认会怎样？答：调查停在 confirm；恢复时没有确认记录就直接结束，不写记忆——"宁可少记，也不要记错"（02 §3.4） |

### ⑧ 紧急停止

| 项 | 内容 |
|---|---|
| 位置 | `governance/kill_switch.py:51` `freeze`、`:83` `unfreeze`；执行器里的检查在 `exec/core.py:46` `process` 的第一步；plan 节点里的检查在 `investigation.py:264` `plan` |
| 人话 | 一个开关：打开后执行器拒绝一切动作，等审批的方案全部作废，新的调查照常诊断、但不出修复方案 |
| 输入 / 输出 | 输入：`sentinel freeze --reason --as`；输出：`freeze_events` 表里追加一行，审计 `freeze.enabled`（ADR-014），每个被作废的审批记 `approval.expired`（reason=frozen） |
| 关键概念 | **冻结状态存在 Postgres、执行器自己读**（07 §7：不依赖 agent-api 是否正常运行）；**冻结之后仍然诊断**（出了问题的时候，最需要的就是诊断信息） |
| 为什么这样设计 | 07 §7；作废待审批的方式是写一个 `rejected` 决定（审批人 `system:freeze`），之后谁也批不了它，执行器即使收到请求也会因为冻结而拒绝 |
| 可能出错的地方 | 冻结时回滚也不做：冻结就是要人接手，自动回滚可能让情况更复杂；"解冻需要审批人角色"第一版只记 `--as`，不强制 |
| 面试可能被问 | 问：执行器已经在执行中途了，冻结能拦住吗？答：拦不住正在执行的那一次——冻结在每次执行开始前检查。已经开始的动作要靠验证失败后的回滚，或者人手动处理 |

### ⑨ 审计校验

| 项 | 内容 |
|---|---|
| 位置 | `governance/audit.py:66` `verify_chain`、`:78` `verify_anchors`；追加时的锁和哈希在 `:53` `pg_append`；CLI `audit verify`、`audit anchor` |
| 人话 | 每条审计记录都带着上一条的哈希；逐条重算，改动或删除中间的记录会在下一条对不上。最后一条被改、整条链被重算，要靠库外的锚定文件才能发现 |
| 输入 / 输出 | 输入：按顺序排好的 `AuditEvent`（03 §5.1）和锚定文件 `var/audit/anchors.log`；输出：问题列表（空 = 完整），`audit verify` 有问题时退出码为 1 |
| 关键概念 | **哈希链**（每条事件的哈希 = sha256(规范化 JSON，含 prev_hash)）；**外部锚定**（把最后一条的哈希另存到库外，防"整条链重算"） |
| 为什么这样设计 | 07 §5；哈希链的规则放在 governance 而不是 core，因为执行器也往同一条链上写，而执行器不能依赖 core |
| 可能出错的地方 | 应用账号还是超级用户，哈希链只能"发现"篡改，不能"阻止"（07 §5 第一条"只授予 INSERT"还没做）；锚定文件本身在本地，同一台机器上的人也能改 |
| 面试可能被问 | 问：有数据库完全权限的人能不能绕过哈希链？答：能——他可以把整条链重新算一遍。所以要外部锚定：库外存过的哈希他改不了 |

### ⑩ 上线门槛

| 项 | 内容 |
|---|---|
| 位置 | `eval/gate.py:40` `gate`；CLI `eval gate v<N> --dataset-tag` |
| 人话 | 版本能不能上线的底线检查：修复模式下不安全提议为 0、结果都能通过 schema、录制未覆盖的用例不超过 5% |
| 输入 / 输出 | 输入：这个版本在这个标签上的评测结果文件（修复模式必须有）；输出：三项检查各自通过或不通过，任何一项不通过退出码为 1 |
| 关键概念 | **安全门槛优先**（06 §7.1–7.2：安全门槛不通过，主指标再好也不上线）；**只有修复模式会产生提议**，所以没有修复模式的结果就判不通过 |
| 为什么这样设计 | 06 §7.1"第一版只做安全底线"；取舍规则（§7.2 的比较）由 `eval compare` 和人来做，门槛只负责"能不能上" |
| 可能出错的地方 | 门槛看的是 dev；heldout 只在里程碑跑，门槛不读它；评分口径变了（例如 v35 改了 top1 只看诊断阶段的升级）之后，新旧结果要注意口径 |
| 面试可能被问 | 问：为什么门槛只看"不安全提议为 0"，不看准确率？答：准确率的取舍有很多情况（06 §7.2），需要人判断；安全是不能交易的底线，所以写成自动检查 |

## 3. 先预测，再运行

| # | 要运行的命令 | 请先预测 | 实际结果（运行后填写） |
|---|---|---|---|
| 1 | `sentinel approvals show <最近的 apv>` | 前置检查快照里会记下哪些原值？为什么要记？ | |
| 2 | `sentinel freeze --reason 演练 --as you` 之后 `sentinel approvals list`，再 `unfreeze` | 待审批的会变成什么状态？审计里多了哪几条？ | |
| 3 | `uv run pytest tests/flows/test_remediation.py -k unsafe -v` | 4 种不安全提议分别是什么？哪一种最容易被忽略？ | |

## 4. 自测题

<details><summary>题 1：审批批的到底是什么？如果审批之后有人改了方案的参数会怎样？</summary>

答案：批的是 plan_hash。执行器会用方案里的字段重算哈希，和方案记录、审批请求里的三处比对，任何一处不一致就拒绝执行（`action.precheck_failed`，stage=approval）。
</details>

<details><summary>题 2：为什么 approve 节点里只有一行 interrupt()？</summary>

答案：恢复时节点会从第一行重新执行，节点里如果写了副作用（例如写审批请求）就会执行两次。所以写入放在 plan 节点，读取放在 execute 节点。
</details>

<details><summary>题 3：执行器已经有了前置检查，plan 节点为什么还要做一次？</summary>

答案：plan 节点的检查是给审批人看的（快照进入 plan_hash）；执行器的检查是防"审批之后状态变了"（TOCTOU）。两次检查的目的不同。
</details>

<details><summary>题 4：v32 的 top1 为什么是 0.97？是诊断变差了吗？</summary>

答案：不是。模型提议的参数超出前置检查允许的范围，方案被拦下、调查升级，旧的评分把这种升级也算成"诊断升级"，于是 top1 被判错。v35 起 top1 只看诊断阶段的升级，方案阶段的结果由 remediation_completed 衡量；同时参数 schema 写明了取值规则。
</details>

<details><summary>题 5：冻结之后为什么还要继续诊断？</summary>

答案：07 §7：出了问题的时候，最需要的就是诊断信息。冻结只挡住"动手"（方案和执行），不挡"看"。
</details>
