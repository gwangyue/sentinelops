"""审计哈希链（03 §5.1；07 §5）：调查引擎和执行器都往同一条链上写。

负责：一条事件的哈希怎么算（event_hash）、Postgres 上怎么在一个事务里追加（pg_append）。
不负责：哈希链的校验命令（audit verify，M3 D12）。
放在 governance 而不是 core/store：执行器不能依赖 core（ADR-005），但它的 action.executed 也要进同一条链。
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import psycopg
from ulid import ULID

from sentinel.contracts import AuditEvent

GENESIS_HASH = "0" * 64


def event_hash(e: AuditEvent) -> str:
    """一条审计事件的哈希 = sha256(事件的规范化 JSON，含 prev_hash)。下一条事件的 prev_hash 就是它。"""
    data = json.dumps(e.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


def new_event(
    prev: AuditEvent | None,
    *,
    investigation_id: str | None,
    agent_version: str | None,
    actor: str,
    type: str,
    payload: dict[str, Any],
) -> AuditEvent:
    return AuditEvent(
        id=f"aud_{ULID()}",
        ts=datetime.now(UTC),
        investigation_id=investigation_id,
        agent_version=agent_version,
        actor=actor,
        type=type,
        payload=payload,
        prev_hash=event_hash(prev) if prev else GENESIS_HASH,
    )


def row_to_event(r: dict[str, Any]) -> AuditEvent:
    return AuditEvent.model_validate({k: v for k, v in r.items() if k != "seq"})


def pg_append(c: psycopg.Connection[dict[str, Any]], **kw: Any) -> AuditEvent:
    """在调用方的事务里追加：先锁表、取最后一条、算 prev_hash、再插入，并发时链不会分叉。"""
    c.execute("LOCK TABLE audit_events IN EXCLUSIVE MODE")
    last = c.execute("SELECT * FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
    e = new_event(row_to_event(last) if last else None, **kw)
    c.execute(
        "INSERT INTO audit_events (id, ts, investigation_id, agent_version, actor, type, payload, prev_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (e.id, e.ts, e.investigation_id, e.agent_version, e.actor, e.type, json.dumps(e.payload), e.prev_hash),
    )
    return e
