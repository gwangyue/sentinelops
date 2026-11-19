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
        namespace: str | None,
        policy: dict[str, Any],
        embed: Callable[[Sequence[str]], list[list[float]]],
    ) -> None:
        self.store, self.namespace, self.policy, self.embed = store, namespace, policy, embed

    def similar(
        self, findings: Sequence[Finding], sheets: Sequence[FactSheet], changes: Sequence[Change], top_k: int
    ) -> list[MemoryHit]:
        now = datetime.now(UTC)
        f = fp.build(findings, sheets, changes)
        # 命名空间 = 领域/集群（05 §3.6 第 3 道防线）；没有显式指定时，从问题事件推出来
        ns = self.namespace or f"{findings[0].pack}/{findings[0].target}"
        hits = retrieve(
            self.store,
            namespace=ns,
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
            # 易混淆的根因标签，由ID，替换为文本标签，便于推理。
            # 已失效的不算：失效 = 不再参与检索（05 §3.7），也包括不再以"易混淆"提示进入 L5
            # （否则因伤害失效的记忆，它的根因仍会通过配对记忆影响诊断，教程 01 §3.8 实测）
            labels = tuple(
                sorted(
                    {
                        m.record.root_cause_label
                        for i in r.confused_with
                        if (m := self.store.get(i)) is not None and not m.record.invalidated
                    }
                )
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
