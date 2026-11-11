"""修复方案 RemediationPlan（03 §4.3）和 LLM 的修复提议 ActionProposal（ADR-013）。

LLM 只选 action_id 和 params；precheck_snapshot、dry_run_result、plan_hash 都由确定性代码填写（M3）。
契约里不提供 plan_hash 的计算，避免任何调用方"顺手"让模型生成它（CLAUDE.md 的安全规则）。
"""

from typing import Any

from sentinel.contracts._base import Contract


class RemediationPlan(Contract):
    schema_version: int = 1
    id: str  # pln_...
    investigation_id: str
    action_id: str
    target: str
    params: dict[str, Any]
    rationale_hypothesis: str  # "H1"
    precheck_snapshot: dict[str, Any]
    dry_run_result: dict[str, Any]
    rollback_plan: str | None
    plan_hash: str  # hash(action_id, target, params, precheck_snapshot)


class ActionProposal(Contract):
    """LLM 唯一能输出的修复相关结构（ADR-013）。plan 节点校验白名单和 params_schema 之后，
    由确定性代码做前置检查、预演、算 plan_hash，组装成 RemediationPlan。"""

    action_id: str  # 必须在动作目录里
    params: dict[str, Any]  # 必须通过动作的 params_schema
    rationale_hypothesis: str  # 依据的是哪个假设（"H1"）
