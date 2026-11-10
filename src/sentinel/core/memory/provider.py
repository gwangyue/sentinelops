"""L5 的 MemoryProvider 实现（04 §3.6）：检索 → MemoryHit。"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sentinel.contracts import FactSheet, Finding
from sentinel.core.context.changes import Change
from sentinel.core.context.memory_layer import MemoryHit
from sentinel.core.memory import fingerprint as fp
from sentinel.core.memory.retrieve import retrieve
from sentinel.core.memory.store import MemoryStore
from sentinel.core.memory.text import symptom_text


class StoreMemoryProvider:
    def __init__(
        self,
        store: MemoryStore,
        namespace: str,
        policy: dict[str, Any],
        embed: Callable[[Sequence[str]], list[list[float]]],
    ) -> None:
        self.store, self.namespace, self.policy, self.embed = store, namespace, policy, embed

    def similar(
        self, findings: Sequence[Finding], sheets: Sequence[FactSheet], changes: Sequence[Change], top_k: int
    ) -> list[MemoryHit]:
        now = datetime.now(UTC)
        f = fp.build(findings, sheets, changes)
        hits = retrieve(
            self.store,
            namespace=self.namespace,
            fingerprint=f,
            text=symptom_text(f),
            embed=self.embed,
            policy=self.policy,
            now=now,
            top_k=top_k,
        )
        out = []
        for s in hits:
            r = s.memory.record
            labels = tuple(
                sorted({m.record.root_cause_label for i in r.confused_with if (m := self.store.get(i)) is not None})
            )
            out.append(
                MemoryHit(
                    memory_id=r.id,
                    similarity=s.score,
                    age_days=int((now - r.created_at).days),
                    outcome=r.outcome,
                    corrected=r.corrected,
                    symptom=r.symptom_text,
                    root_cause_label=r.root_cause_label,
                    root_cause_text=r.root_cause_text,
                    action=r.action_taken,
                    confused_labels=labels,
                )
            )
        return out
