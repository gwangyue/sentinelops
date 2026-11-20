"""Postgres 实现（compose 和 kind）。连接串来自 SENTINEL_PG_URL。

审计追加在一个事务里先锁表、取最后一条、算 prev_hash、再插入，保证并发时哈希链不分叉。
"""

import dataclasses
import json
from collections.abc import Collection
from datetime import datetime
from importlib.resources import files
from typing import Any

import psycopg
from psycopg.rows import dict_row

from sentinel.contracts import AgentVersion, AuditEvent, Observation
from sentinel.core.store.base import InvestigationRecord
from sentinel.governance.audit import pg_append

_JSON_FIELDS = {"findings", "fact_sheets", "context_bundle", "diagnosis"}


class PgStore:
    def __init__(self, url: str) -> None:
        self.url = url

    def _conn(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self.url, row_factory=dict_row, autocommit=False)

    def init(self) -> None:
        sql_dir = files("sentinel.core.store").joinpath("sql")
        with self._conn() as c:
            # 按文件名顺序执行；每个文件都只用 IF NOT EXISTS，可以重复执行
            for f in sorted((p for p in sql_dir.iterdir() if p.name.endswith(".sql")), key=lambda p: p.name):
                c.execute(f.read_text(encoding="utf-8"))  # 文件内容是受信任的 DDL

    # ---------- 调查 ----------
    def create_investigation(self, rec: InvestigationRecord) -> None:
        d = dataclasses.asdict(rec)
        cols = ", ".join(d)
        vals = ", ".join(f"%({k})s" for k in d)
        with self._conn() as c:
            c.execute(
                f"INSERT INTO investigations ({cols}) VALUES ({vals})",
                {k: json.dumps(v) if k in _JSON_FIELDS and v is not None else v for k, v in d.items()},
            )

    def update_investigation(self, inv_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k} = %({k})s" for k in fields)
        params = {k: json.dumps(v) if k in _JSON_FIELDS and v is not None else v for k, v in fields.items()}
        with self._conn() as c:
            c.execute(f"UPDATE investigations SET {sets} WHERE id = %(id)s", {**params, "id": inv_id})

    def get_investigation(self, inv_id: str) -> InvestigationRecord | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM investigations WHERE id = %s", (inv_id,)).fetchone()
        return InvestigationRecord(**row) if row else None

    def list_investigations(self, limit: int = 20) -> list[InvestigationRecord]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM investigations ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
        return [InvestigationRecord(**r) for r in rows]

    def recent_fingerprints(self, target: str, since: datetime, exclude: str) -> set[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT f->>'fingerprint' AS fp FROM investigations, jsonb_array_elements(findings) AS f "
                # 被判为重复的调查不算（见 base.py 的 recent_fingerprints）
                "WHERE target = %s AND created_at >= %s AND id <> %s AND status <> 'deduplicated'",
                (target, since, exclude),
            ).fetchall()
        return {r["fp"] for r in rows}

    # ---------- 审计 ----------
    def append_audit(
        self,
        *,
        investigation_id: str | None,
        agent_version: str | None,
        actor: str,
        type: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        with self._conn() as c:
            return pg_append(
                c,
                investigation_id=investigation_id,
                agent_version=agent_version,
                actor=actor,
                type=type,
                payload=payload,
            )

    def audit_events(self, investigation_id: str | None = None) -> list[AuditEvent]:
        with self._conn() as c:
            if investigation_id is None:
                rows = c.execute("SELECT * FROM audit_events ORDER BY seq").fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM audit_events WHERE investigation_id = %s ORDER BY seq", (investigation_id,)
                ).fetchall()
        return [_audit(r) for r in rows]

    def audit_events_since(
        self, after_seq: int, types: Collection[str], limit: int = 100
    ) -> list[tuple[int, AuditEvent]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM audit_events WHERE seq > %s AND type = ANY(%s) ORDER BY seq LIMIT %s",
                (after_seq, list(types), limit),
            ).fetchall()
        return [(r["seq"], _audit(r)) for r in rows]

    def latest_audit_seq(self) -> int:
        with self._conn() as c:
            row = c.execute("SELECT coalesce(max(seq), 0) AS s FROM audit_events").fetchone()
        return int(row["s"]) if row else 0

    def get_notify_cursor(self, env: str) -> int | None:
        with self._conn() as c:
            row = c.execute("SELECT last_seq FROM notify_cursor WHERE env = %s", (env,)).fetchone()
        return int(row["last_seq"]) if row else None

    def set_notify_cursor(self, env: str, seq: int) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO notify_cursor (env, last_seq) VALUES (%s, %s) "
                "ON CONFLICT (env) DO UPDATE SET last_seq = excluded.last_seq, updated_at = now()",
                (env, seq),
            )

    # ---------- 版本 ----------
    def get_version(self, hash_or_seq: str) -> AgentVersion | None:
        seq = hash_or_seq.removeprefix("v")
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM agent_versions WHERE hash = %s OR seq::text = %s", (hash_or_seq, seq)
            ).fetchone()
        return AgentVersion.model_validate(row) if row else None

    def latest_version(self) -> AgentVersion | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM agent_versions ORDER BY seq DESC LIMIT 1").fetchone()
        return AgentVersion.model_validate(row) if row else None

    def insert_version(self, v: AgentVersion) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO agent_versions (seq, hash, parent, created_at, note, components) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (v.seq, v.hash, v.parent, v.created_at, v.note, v.components.model_dump_json()),
            )

    def list_versions(self) -> list[AgentVersion]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM agent_versions ORDER BY seq").fetchall()
        return [AgentVersion.model_validate(r) for r in rows]

    # ---------- 配置快照 ----------
    def latest_snapshot(self, target: str, tool: str) -> tuple[Any, datetime] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT raw, ts FROM snapshots WHERE target = %s AND tool = %s ORDER BY ts DESC LIMIT 1", (target, tool)
            ).fetchone()
        return (row["raw"], row["ts"]) if row else None

    def save_snapshot(self, target: str, tool: str, raw: Any, ts: datetime) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO snapshots (target, tool, ts, raw) VALUES (%s, %s, %s, %s)",
                (target, tool, ts, json.dumps(raw)),
            )

    # ---------- 观测索引 ----------
    def add_observation(self, obs: Observation) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO observations (id, investigation_id, created_at, body) VALUES (%s, %s, %s, %s)",
                (obs.id, obs.investigation_id, obs.created_at, obs.model_dump_json()),
            )

    def observations(self, investigation_id: str) -> list[Observation]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT body FROM observations WHERE investigation_id = %s ORDER BY created_at", (investigation_id,)
            ).fetchall()
        return [Observation.model_validate(r["body"]) for r in rows]


def _audit(row: dict[str, Any]) -> AuditEvent:
    return AuditEvent.model_validate({k: v for k, v in row.items() if k != "seq"})
