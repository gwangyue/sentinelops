"""审计事件 AuditEvent（03 §5.1）：只追加，带哈希链。

type 按 03 保持为 str，不在契约里做枚举校验：事件类型会随里程碑增加，
写入方在 governance 层对照 KNOWN_EVENT_TYPES 检查（M3）。

例子取自本机 Postgres audit_events 表 seq=139（一条 memory.written）。
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


# 一条审计事件。每条记下上一条的哈希，中间被改或被删就能查出来。
class AuditEvent(Contract):
    schema_version: int = 1  # 例：1
    id: str  # aud_ + ULID。例："aud_01M74HA4VDDKA5M7F9DY3X1HEG"
    ts: datetime  # 例：2026-11-11T05:29:24.077229Z
    investigation_id: str | None  # 冻结开关、部分 mcp.call 不属于调查，为 None。例："inv_01M74H8GFR508XMFKZYT1DYVFC"
    # 当时生效的 Agent 版本哈希。例："sha256:341924fe1df83755bd72bb9113d1540af5f1318711d6e74a6fa5081ad3897079"
    agent_version: str | None
    # "agent" | "executor" | "human:<名字>" | "mcp:<调用方>"
    # 本机实际出现过：agent、executor、human:oncall、human:approver、human:ops、mcp:workbuddy、system:freeze
    actor: str
    type: str  # 例："memory.written"、"approval.decided"、"mcp.call"
    # 只存 ID 和摘要，不存原始数据。
    # 例：{"memory": "mem_01M74HA43ZTJXW959B91E9T5QB", "merged": false, "confused_with": []}
    payload: dict[str, Any]
    prev_hash: str  # 上一条事件的哈希。例："33aa19abae1e97a367dfbdf3c024ae8c2db0016bfa298f40edd10462702c369e"
