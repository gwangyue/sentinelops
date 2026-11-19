"""审批与执行（03 §4.4）。执行器只接收 (plan_id, approval_id, idempotency_key)，其余自己从 Postgres 读。

例子取自本机 Postgres 的 approval_requests、approval_decisions、executions 表（ES 磁盘水位场景的一次执行）。
"""

from datetime import datetime
from typing import Any, Literal

from sentinel.contracts._base import Contract


# 针对一份方案发出的审批请求。审批的是 plan_hash，方案内容一变就要重新审批。
class ApprovalRequest(Contract):
    id: str  # apv_ + ULID，由代码生成。例："apv_01M7411WGMAYKVTHF2TA0DN02D"
    plan_id: str  # 例："pln_01M7411WGMAYKVTHF2TA0DN02C"
    plan_hash: str  # 从方案复制过来。例："sha256:64838e87a7d3c97cd234667c78d137f071d245854f6be7389db459d7db10c7bf"
    requested_at: datetime  # 例：2026-11-11T00:45:16.180784Z
    expires_at: datetime  # 默认 30 分钟后过期。例：2026-11-11T01:15:16.180784Z


# 人对审批请求的决定。
class ApprovalDecision(Contract):
    approval_id: str  # 例："apv_01M744T7ED1KNDDFY8NE99VXRZ"
    decision: Literal["approved", "rejected"]  # 例："approved"
    approver: str  # human:<名字>。例："human:approver"
    reason: str | None  # 拒绝时一般会写；批准时可为 None。例：None
    decided_at: datetime  # 例：2026-11-11T01:51:10.280869Z


# 执行器执行一份已批准方案的结果（含验证、回滚情况）。
class ExecutionResult(Contract):
    plan_id: str  # 例："pln_01M744T7ED1KNDDFY8NE99VXRY"
    approval_id: str  # 例："apv_01M744T7ED1KNDDFY8NE99VXRZ"
    # hash(investigation_id, plan_hash)，同一个键只执行一次。
    # 例："sha256:ca91312bd5d6f03887d2f4851a4df8e9065e21cf3b82e0b7c018caad18d8d059"
    idempotency_key: str
    # duplicate = 这个幂等键已经执行过，直接返回上次的结果。例："succeeded"
    status: Literal["succeeded", "failed", "precheck_failed", "duplicate"]
    # 目标系统的原始返回。例（ES 改水位）：
    # {"acknowledged": true, "persistent": {"cluster": {"routing": {"allocation": {"disk": {"watermark":
    #  {"low": "94%", "high": "94%", ...}}}}}}, "transient": {}}
    output: dict[str, Any]
    verify_passed: bool | None  # 执行器不做验证（在调查图的 verify 节点做），它写的结果里为 None。例：None
    rolled_back: bool = False  # 例：False
