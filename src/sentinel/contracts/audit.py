"""审计事件 AuditEvent（03 §5.1）：只追加，带哈希链。

type 按 03 保持为 str，不在契约里做枚举校验：事件类型会随里程碑增加，
写入方在 governance 层对照 KNOWN_EVENT_TYPES 检查（M3）。
"""

from datetime import datetime
from typing import Any

from sentinel.contracts._base import Contract

# 03 §5.1 表格里的全部事件类型
KNOWN_EVENT_TYPES = frozenset(
    {
        "investigation.started",
        "investigation.closed",
        "investigation.escalated",
        "finding.detected",
        "finding.deduplicated",
        "patrol.healthy",  # 巡检没有发现问题（ADR-011；golden-demo 第 1 步）
        "diagnosis.completed",
        "plan.proposed",
        "plan.rejected_unsafe",
        "approval.requested",
        "approval.decided",
        "approval.expired",
        "action.executed",
        "action.precheck_failed",
        "action.rolled_back",
        "verify.passed",
        "verify.failed",
        "rootcause.confirmed",
        "rootcause.corrected",
        "memory.written",
        "memory.invalidated",
        "mcp.call",
        "budget.exceeded",
    }
)


class AuditEvent(Contract):
    schema_version: int = 1
    id: str
    ts: datetime
    investigation_id: str | None
    agent_version: str | None
    actor: str  # "agent" | "executor" | "human:<名字>" | "mcp:<调用方>"
    type: str
    payload: dict[str, Any]  # 只存 ID 和摘要，不存原始数据
    prev_hash: str  # 上一条事件的哈希
