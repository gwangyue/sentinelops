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
| 位置 | `investigation.py:329` `approve`（只有 `interrupt()`）；`remediate.py:189` `record_decision`；CLI `approvals approve` |
| 人话 | 图停下来等人。人在命令行里批准或拒绝，决定写进 Postgres，同时记审计 `approval.decided`，然后恢复同一次调查（同一个 thread_id 从 checkpoint 找回状态） |
| 关键概念 | **interrupt**（LangGraph 暂停图的方式：抛一个特殊异常，恢复时节点从第一行重新执行。所以节点里不能有副作用，也不能被宽泛的 except 吞掉——有 lint 规则检查） |
| 为什么这样设计 | 02 §3.4：resume 只带"已恢复"，审批结果在下一个节点里从存储读，模型伪造不了审批 |
| 可能出错的地方 | 同一个审批决定两次：存储的唯一约束挡住（`DuplicateError`） |

### ③④ execute 和执行器：唯一有写权限的地方

| 项 | 内容 |
|---|---|
| 位置 | `investigation.py:334` `execute`；`core/graph/run.py` `queue_executor`；`exec/core.py:46` `process`；`exec/__main__.py`；`exec/live.py` |
| 人话 | 调查引擎只把三个 ID 写进队列；独立的执行器进程自己去读方案和审批，依次检查：系统有没有冻结、哈希三处是否一致、审批是否通过且没过期、这个幂等键是否已经执行过、前置检查现在是否还满足——都过了才执行 |
| 关键概念 | **幂等键**（hash(调查 ID, plan_hash)：恢复、重试时同一个方案不会执行两次）；**TOCTOU**（"检查时满足、执行时已不满足"：所以执行前要再查一次） |
| 为什么这样设计 | ADR-005：写凭证只在执行器进程里；`exec` 不能 import `core`（import-linter 检查） |
| 可能出错的地方 | 执行器没启动：调查等 300 秒后升级。回放评测里用 `eval/replay_exec.py` 在进程内调用同一个 `process`，只把"执行"换成"切到录制的 post 阶段" |
| 面试可能被问 | 问：为什么回放的执行器不另写一个简单版本？答：校验逻辑是安全的核心，评测必须测到真实的那一份 |

### ⑤ verify：验证失败就回滚

| 项 | 内容 |
|---|---|
| 位置 | `investigation.py:353` `verify`；`remediate.py:164` `run_verify` |
| 人话 | 按动作声明的验证条件轮询（例如集群变绿）；超时没达成，就请执行器按前置检查快照把设置恢复原值，然后升级给人 |
| 可能出错的地方 | 回放的状态不会变，按时间轮询只会空转到超时——回放时只检查一次（测试时踩过） |

### ⑥⑦ confirm 和 memorize：修好了不代表诊断对

| 项 | 内容 |
|---|---|
| 位置 | `investigation.py:371` `confirm`、`:375` `memorize_node`；`core/graph/confirm.py:45` `memorize`、`:90` `record_confirmation` |
| 人话 | 修好之后再停一次，等人确认根因。确认（或修正）写进审计，memorize 节点从审计里读这个决定，才写记忆；人修正过的记忆标记 `corrected=true`。不确认就不写 |
| 为什么这样设计 | 05 §3.1：模型的结论直接写进记忆，一次误诊就会变成"经验"，错误会自我强化 |
| 可能出错的地方 | 模型把假设编号写成 `h1`：确认命令现在不区分大小写（kind 上实测发现） |

### ⑧⑨⑩ 治理三件套

| 站 | 位置 | 人话 |
|---|---|---|
| 紧急停止 | `governance/kill_switch.py:51` `freeze` | 冻结状态存在 Postgres，执行器每次都读；冻结时作废待审批、不出方案，但诊断照常 |
| 审计校验 | `governance/audit.py:66` `verify_chain`、`:78` `verify_anchors` | 逐条重算哈希；改动或删除中间的记录会在下一条报断点；整条链被重算要靠库外的锚定文件才能发现 |
| 上线门槛 | `eval/gate.py:40` `gate` | 修复模式下不安全提议为 0、结果都能通过 schema、录制未覆盖 ≤ 5% |

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
