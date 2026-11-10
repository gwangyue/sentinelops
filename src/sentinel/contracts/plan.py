"""修复方案 RemediationPlan（03 §4.3）。

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
