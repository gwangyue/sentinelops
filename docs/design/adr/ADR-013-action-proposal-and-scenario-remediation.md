# ADR-013 新增 ActionProposal；ScenarioSpec 增加可选的标准修复

- 状态：已接受（2026-11-11，人确认：M2 决策点之后的 D10–D11 计划）
- 相关：03 篇 §4.3、§6.1，02 篇 §3.4，ADR-005，CLAUDE.md"不要让 LLM 生成 plan_hash"

## 背景

1. plan 节点要让 LLM 选一个修复动作。03 篇只有 `RemediationPlan`，它同时带着 `precheck_snapshot`、`dry_run_result`、`plan_hash` 这些必须由确定性代码填写的安全字段。如果直接用它做 LLM 的结构化输出，模型就会"顺手"填这些字段，哪怕之后再覆盖掉，也违反了"安全相关字段不由 LLM 生成"的规则。CLAUDE.md 又要求数据结构一律使用 `contracts/` 里的模型，所以 LLM 的输出结构也必须进契约。
2. M3 D11 的回放执行器需要"执行动作之后"的录制（phase=post）。故障台录制时要真实执行一次修复，就需要知道每个场景用哪个动作、哪些参数来修。`GroundTruth.acceptable_actions` 只有动作 ID，没有参数。

## 决策

1. 新增 `ActionProposal`（`contracts/plan.py`），这是 LLM 唯一能输出的修复相关结构：

   ```python
   class ActionProposal(Contract):
       action_id: str            # 必须在动作目录里（plan 节点校验）
       params: dict[str, Any]    # 必须通过动作的 params_schema（plan 节点校验）
       rationale_hypothesis: str # 依据的是哪个假设（"H1"）
   ```

   `RemediationPlan` 不变：plan 节点校验 `ActionProposal` 之后，由代码执行前置检查和预演，算出 `plan_hash`，再组装成 `RemediationPlan`。

2. `ScenarioSpec` 新增 `remediation: ActionProposal | None = None`。录制 post 阶段时，故障台按它走一遍完整的方案、审批、执行流程。字段带默认值，旧的场景文件不用改也能读；没有可用动作的场景（例如节点宕机）保持为空。

## 放弃的方案

| 方案 | 放弃原因 |
|---|---|
| LLM 直接输出 `RemediationPlan`，然后覆盖安全字段 | 安全字段仍然经过了 LLM，审计时说不清"这个值是谁写的" |
| 在 plan 节点里定义一个内部 schema | 违反"数据结构一律用 contracts/"的规则；评测和审计也要读这个结构 |
| 故障台从 `acceptable_actions` 推断参数 | 参数依赖场景（例如水位要提高到多少），推断就是在代码里藏一份没有声明的场景知识 |

## 后果

- 好处：LLM 能接触到的修复相关字段只有 3 个，都在 plan 节点经过白名单和 schema 校验；"不安全提议"的定义因此很清楚，就是这 3 个字段通不过校验。
- 代价：契约多一个模型；`samples/v1/` 的基线数据要补一个样例。
