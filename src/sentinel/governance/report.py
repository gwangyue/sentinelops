"""治理周报里来自审计的部分（07 §8；02 §7；golden-demo 第 10 步）。

负责：周报第 1、2、3、5 项和"需要你决定的事"里来自审计的条目。**每个数字都是一条对 audit_events 的 SQL**
      （QUERIES），golden-demo 要求"能用 SQL 从审计表里重新算出来"——这里的 SQL 就是定义本身。
      同一份 SQL 在 Postgres（线上）和 sqlite（测试、demo-check：把事件装进内存表）上都能跑：
      只用了 COUNT / SUM / JOIN 和 JSON 的 ->> 运算符，两边语义一致。
不负责：第 4 项评测状态（结果文件在 eval，由 CLI 组装）；成本的单价（pricing.yaml，由 CLI 换算）。
已知缺口：plan 节点的那一次模型调用没有记 token（要记得改 core，M4 要求 core 零改动），成本只含诊断。
"""

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sentinel.contracts import AuditEvent

# {start} / {end} 是窗口的两端（左闭右开），由 run 换成对应数据库的参数占位符
WINDOW = "ts >= {start} AND ts < {end}"


def _count(event_type: str, extra: str = "") -> str:
    return f"SELECT COUNT(*) FROM audit_events WHERE type = '{event_type}' {extra} AND {WINDOW}"


QUERIES: dict[str, str] = {
    # 1. 调查与修复
    "investigations": _count("investigation.started"),
    "executed": _count("action.executed", "AND payload->>'status' = 'succeeded'"),
    "verify_passed": _count("verify.passed"),
    "verify_failed": _count("verify.failed"),
    "rolled_back": _count("action.rolled_back"),
    # 2. 安全
    "unsafe": _count("plan.rejected_unsafe"),
    "approvals_rejected": (
        "SELECT payload->>'approval_id', COALESCE(payload->>'reason', '') FROM audit_events "
        f"WHERE type = 'approval.decided' AND payload->>'decision' = 'rejected' AND {WINDOW}"
    ),
    "freezes": _count("freeze.enabled"),
    # 3. 线上质量：确认（含修正）占"修好了、等人确认"的调查的比例
    "resolved": _count("investigation.closed", "AND payload->>'outcome' = 'resolved'"),
    "confirmed": _count("rootcause.confirmed"),
    "corrected": _count("rootcause.corrected"),
    # 5. 成本：诊断的 token（单价在 pricing.yaml，CLI 换算）
    "diagnosis_tokens": (
        "SELECT COUNT(*), COALESCE(SUM(CAST(payload->>'tokens_in' AS INTEGER)), 0), "
        "COALESCE(SUM(CAST(payload->>'tokens_out' AS INTEGER)), 0) FROM audit_events "
        f"WHERE type = 'diagnosis.completed' AND {WINDOW}"
    ),
    # 需要你决定的事：执行成功、没有回滚的"临时"修复（M3 报告 §9：先在周报里列出，由人处理）。
    # 不限开始时间：上周执行、到现在还没撤销的也要列出来
    "temporary_fixes": (
        "SELECT e.investigation_id, e.payload->>'plan_id', p.payload->>'action', e.ts FROM audit_events e "
        "JOIN audit_events p ON p.type = 'plan.proposed' AND p.payload->>'plan_id' = e.payload->>'plan_id' "
        "WHERE e.type = 'action.executed' AND e.payload->>'status' = 'succeeded' "
        "AND p.payload->>'action' LIKE '%temporar%' "
        "AND NOT EXISTS (SELECT 1 FROM audit_events r WHERE r.type = 'action.rolled_back' "
        "AND r.payload->>'plan_id' = e.payload->>'plan_id') AND e.ts < {end} ORDER BY e.ts"
    ),
    "memory_invalidated": (
        "SELECT payload->>'memory', COALESCE(payload->>'reason', '') FROM audit_events "
        f"WHERE type = 'memory.invalidated' AND {WINDOW}"
    ),
    # 每个动作的审批：批准 / 拒绝次数（从同一次调查的 plan.proposed 找到动作）
    "approvals_by_action": (
        "SELECT p.payload->>'action', d.payload->>'decision', COUNT(*) FROM audit_events d "
        "JOIN audit_events p ON p.type = 'plan.proposed' AND p.investigation_id = d.investigation_id "
        "AND p.payload->>'plan_id' IS NOT NULL "
        "WHERE d.type = 'approval.decided' AND d.ts >= {start} AND d.ts < {end} "
        "GROUP BY p.payload->>'action', d.payload->>'decision'"
    ),
}

Run = Callable[[str, dict[str, Any]], list[tuple[Any, ...]]]


def pg_runner(conn: Any) -> Run:
    def run(sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
        # psycopg 把 % 当作参数占位符：SQL 里本来的 %（LIKE 的通配符）先转义成 %%，再放进占位符
        q = sql.replace("%", "%%").format(start="%(start)s", end="%(end)s")
        return [tuple(r) for r in conn.execute(q, params).fetchall()]

    return run


def sqlite_runner(events: Sequence[AuditEvent]) -> Run:
    """把审计事件装进内存里的 sqlite 表（结构同 sql/001_init.sql），跑同一份 SQL。测试和 demo-check 用。"""
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE audit_events (seq INTEGER PRIMARY KEY, id TEXT, ts TEXT, investigation_id TEXT, "
        "agent_version TEXT, actor TEXT, type TEXT, payload TEXT, prev_hash TEXT)"
    )
    db.executemany(
        "INSERT INTO audit_events (id, ts, investigation_id, agent_version, actor, type, payload, prev_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                e.id,
                e.ts.isoformat(),
                e.investigation_id,
                e.agent_version,
                e.actor,
                e.type,
                json.dumps(e.payload),
                e.prev_hash,
            )
            for e in events
        ],
    )

    def run(sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
        p = {k: v.isoformat() if isinstance(v, datetime) else v for k, v in params.items()}
        return list(db.execute(sql.format(start=":start", end=":end"), p).fetchall())

    return run


@dataclass
class AuditNumbers:
    investigations: int
    executed: int
    verify_passed: int
    verify_failed: int
    rolled_back: int
    unsafe: int
    approvals_rejected: list[tuple[str, str]]
    freezes: int
    resolved: int
    confirmed: int
    corrected: int
    diagnoses: int
    tokens_in: int
    tokens_out: int
    temporary_fixes: list[tuple[str, str, str, str]] = field(default_factory=list)
    memory_invalidated: list[tuple[str, str]] = field(default_factory=list)
    approvals_by_action: dict[str, dict[str, int]] = field(default_factory=dict)


def audit_numbers(run: Run, start: datetime, end: datetime) -> AuditNumbers:
    p = {"start": start, "end": end}

    def one(name: str) -> int:
        return int(run(QUERIES[name], p)[0][0])

    diag = run(QUERIES["diagnosis_tokens"], p)[0]
    by_action: dict[str, dict[str, int]] = {}
    for action, decision, n in run(QUERIES["approvals_by_action"], p):
        by_action.setdefault(str(action), {})[str(decision)] = int(n)
    return AuditNumbers(
        investigations=one("investigations"),
        executed=one("executed"),
        verify_passed=one("verify_passed"),
        verify_failed=one("verify_failed"),
        rolled_back=one("rolled_back"),
        unsafe=one("unsafe"),
        approvals_rejected=[(str(a), str(r)) for a, r in run(QUERIES["approvals_rejected"], p)],
        freezes=one("freezes"),
        resolved=one("resolved"),
        confirmed=one("confirmed"),
        corrected=one("corrected"),
        diagnoses=int(diag[0]),
        tokens_in=int(diag[1]),
        tokens_out=int(diag[2]),
        temporary_fixes=[(str(a), str(b), str(c), str(d)) for a, b, c, d in run(QUERIES["temporary_fixes"], p)],
        memory_invalidated=[(str(a), str(b)) for a, b in run(QUERIES["memory_invalidated"], p)],
        approvals_by_action=by_action,
    )
