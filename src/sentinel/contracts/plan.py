"""修复方案 RemediationPlan（03 §4.3）和 LLM 的修复提议 ActionProposal（ADR-013）。

LLM 只选 action_id 和 params；precheck_snapshot、dry_run_result、plan_hash 都由确定性代码填写（M3）。
契约里不提供 plan_hash 的计算，避免任何调用方"顺手"让模型生成它（CLAUDE.md 的安全规则）。

RemediationPlan 的例子取自本机 Postgres plans 表 pln_01M74H8TCQX1VNESYZ34M2EVPA（MySQL 连接数场景）。
"""

from typing import Any

from sentinel.contracts._base import Contract


# 一份待审批的修复方案：做什么、对谁做、做之前集群是什么样、预演结果如何。审批绑定的是 plan_hash。
class RemediationPlan(Contract):
    schema_version: int = 1  # 例：1
    id: str  # pln_ + ULID。例："pln_01M74H8TCQX1VNESYZ34M2EVPA"
    investigation_id: str  # 例："inv_01M74H8GFR508XMFKZYT1DYVFC"
    action_id: str  # 动作目录里的 ID。例："mysql.raise_max_connections_temporarily"、"es.raise_watermark_temporarily"
    target: str  # 例："mysql-target"
    params: dict[str, Any]  # 已通过动作的 params_schema。例：{"max_connections": 100}
    rationale_hypothesis: str  # 依据的假设 ID。例："H1"
    precheck_snapshot: dict[str, Any]  # 前置检查时读到的现状，回滚靠它。例：{"before": 60, "threads_connected": 56}
    dry_run_result: dict[str, Any]  # 预演结果，给审批人看影响。例：{"settings_changed": 1, "extra_connections": 40}
    # 回滚函数入口和说明；不可逆动作为 None。
    # 例："sentinel.packs.mysql.actions.mysql_actions:raise_max_connections_rollback（按前置检查快照恢复原值）"
    rollback_plan: str | None
    # hash(action_id, target, params, precheck_snapshot)，由确定性代码计算。
    # 例："sha256:614a98470d0f18431d8337d03956ce73927d9cff4853bfbd6a29a1c392e90b62"
    plan_hash: str


class ActionProposal(Contract):
    """LLM 唯一能输出的修复相关结构（ADR-013）。plan 节点校验白名单和 params_schema 之后，
    由确定性代码做前置检查、预演、算 plan_hash，组装成 RemediationPlan。"""

    # 例子取自场景 es.disk_watermark.with_distractor 的 remediation 字段
    action_id: str  # 必须在动作目录里。例："es.raise_watermark_temporarily"
    params: dict[str, Any]  # 必须通过动作的 params_schema。例：{"high_pct": 94}
    rationale_hypothesis: str  # 依据的是哪个假设（"H1"）
