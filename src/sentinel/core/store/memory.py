"""内存实现：只给测试用（测试禁用网络，连不上 Postgres）。行为和 PgStore 保持一致。"""

import dataclasses
from datetime import datetime
from typing import Any

from sentinel.contracts import AgentVersion, AuditEvent, Observation
from sentinel.core.store.base import InvestigationRecord
from sentinel.governance.audit import new_event


class MemoryStore:
    def __init__(self) -> None:
        self.invs: dict[str, InvestigationRecord] = {}
        self.audit: list[AuditEvent] = []
        self.versions: list[AgentVersion] = []
        self.obs: list[Observation] = []
        self.snapshots: dict[tuple[str, str], tuple[Any, datetime]] = {}

    def init(self) -> None:
        pass

    def create_investigation(self, rec: InvestigationRecord) -> None:
        self.invs[rec.id] = rec

    def update_investigation(self, inv_id: str, **fields: Any) -> None:
        self.invs[inv_id] = dataclasses.replace(self.invs[inv_id], **fields)

    def get_investigation(self, inv_id: str) -> InvestigationRecord | None:
        return self.invs.get(inv_id)

    def list_investigations(self, limit: int = 20) -> list[InvestigationRecord]:
        return sorted(self.invs.values(), key=lambda r: r.created_at, reverse=True)[:limit]

    def recent_fingerprints(self, target: str, since: datetime, exclude: str) -> set[str]:
        return {
            f["fingerprint"]
            for r in self.invs.values()
            if r.target == target and r.created_at >= since and r.id != exclude
            for f in r.findings
        }

    def append_audit(
        self,
        *,
        investigation_id: str | None,
        agent_version: str | None,
        actor: str,
        type: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        e = new_event(
            self.audit[-1] if self.audit else None,
            investigation_id=investigation_id,
            agent_version=agent_version,
            actor=actor,
            type=type,
            payload=payload,
        )
        self.audit.append(e)
        return e

    def audit_events(self, investigation_id: str | None = None) -> list[AuditEvent]:
        return [e for e in self.audit if investigation_id is None or e.investigation_id == investigation_id]

    def get_version(self, hash_or_seq: str) -> AgentVersion | None:
        return next((v for v in self.versions if hash_or_seq in (v.hash, f"v{v.seq}", str(v.seq))), None)

    def latest_version(self) -> AgentVersion | None:
        return max(self.versions, key=lambda v: v.seq, default=None)

    def insert_version(self, v: AgentVersion) -> None:
        self.versions.append(v)

    def list_versions(self) -> list[AgentVersion]:
        return sorted(self.versions, key=lambda v: v.seq)

    def latest_snapshot(self, target: str, tool: str) -> tuple[Any, datetime] | None:
        return self.snapshots.get((target, tool))

    def save_snapshot(self, target: str, tool: str, raw: Any, ts: datetime) -> None:
        self.snapshots[(target, tool)] = (raw, ts)

    def add_observation(self, obs: Observation) -> None:
        self.obs.append(obs)

    def observations(self, investigation_id: str) -> list[Observation]:
        return [o for o in self.obs if o.investigation_id == investigation_id]
