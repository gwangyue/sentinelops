"""审批与执行（03 §4.4）。执行器只接收 (plan_id, approval_id, idempotency_key)，其余自己从 Postgres 读。"""

from datetime import datetime
from typing import Any, Literal

from sentinel.contracts._base import Contract


class ApprovalRequest(Contract):
    id: str  # apv_...
    plan_id: str
    plan_hash: str
    requested_at: datetime
    expires_at: datetime  # 默认 30 分钟后过期


class ApprovalDecision(Contract):
    approval_id: str
    decision: Literal["approved", "rejected"]
    approver: str
    reason: str | None
    decided_at: datetime


class ExecutionResult(Contract):
    plan_id: str
    approval_id: str
    idempotency_key: str  # hash(investigation_id, plan_hash)
    status: Literal["succeeded", "failed", "precheck_failed", "duplicate"]
    output: dict[str, Any]
    verify_passed: bool | None
    rolled_back: bool = False
