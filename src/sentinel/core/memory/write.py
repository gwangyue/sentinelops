"""确认之后写入记忆（05 §3.1、§3.4）。**只能在人确认或修正根因之后调用**：模型自己的结论不写进记忆。

负责：生成记忆文本（模板）、合并（同根因且指纹重合度够高）、标记易混淆（指纹几乎相同但根因不同）、审计。
不负责：确认这一步本身（调查图的 confirm 节点在 M3 D12；事故流评测里用真实答案模拟，05 §4.2）。
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from ulid import ULID

from sentinel.contracts import MemoryRecord
from sentinel.core.memory.fingerprint import tokens, weighted_jaccard
from sentinel.core.memory.store import MemoryStore, StoredMemory
from sentinel.core.memory.text import root_cause_text, symptom_text

Audit = Callable[[str, dict[str, Any]], None]  # (事件类型, payload)


def write_confirmed(
    store: MemoryStore,
    *,
    namespace: str,
    investigation_id: str,
    fingerprint: dict[str, list[str]],
    root_cause_label: str,
    root_cause_description: str,
    corrected: bool,
    confirmed_by: str,
    outcome: Literal["resolved", "mitigated", "no_effect"],
    action_taken: str | None,
    target_version: str,
    embed: Callable[[Sequence[str]], list[list[float]]],
    policy: dict[str, Any],
    audit: Audit,
    now: datetime | None = None,
) -> MemoryRecord:
    now = now or datetime.now(UTC)
    toks = tokens(fingerprint)
    weights = policy["fingerprint_weights"]
    threshold = float(policy["merge"]["jaccard"])
    existing = [m for m in store.all(namespace) if not m.record.invalidated]
    similar = [(weighted_jaccard(toks, tokens(m.record.symptom_fingerprint), weights), m) for m in existing]

    same = [m for j, m in similar if j >= threshold and m.record.root_cause_label == root_cause_label]
    if same:
        # 合并：同一类事故又发生了一次（05 §3.4）
        m = max(same, key=lambda x: x.record.occurrences)
        r = m.record.model_copy(
            update={
                "occurrences": m.record.occurrences + 1,
                "source_investigations": [*m.record.source_investigations, investigation_id],
                "corrected": m.record.corrected or corrected,
                "confirmed_by": confirmed_by,
            }
        )
        store.put(StoredMemory(r, m.embedding, m.harm_count))
        audit("memory.written", {"memory": r.id, "merged": True, "occurrences": r.occurrences})
        return r

    text = symptom_text(fingerprint)
    confused = [m for j, m in similar if j >= threshold and m.record.root_cause_label != root_cause_label]
    r = MemoryRecord(
        id=f"mem_{ULID()}",
        namespace=namespace,
        symptom_fingerprint=fingerprint,
        symptom_text=text,
        root_cause_label=root_cause_label,
        root_cause_text=root_cause_text(root_cause_label, root_cause_description),
        action_taken=action_taken,
        outcome=outcome,
        source_investigations=[investigation_id],
        confused_with=[m.record.id for m in confused],
        confirmed_by=confirmed_by,
        corrected=corrected,
        target_version=target_version,
        created_at=now,
        expires_at=None,
    )
    store.put(StoredMemory(r, embed([text])[0]))
    for m in confused:
        # 易混淆是双向的：以后检索到其中任何一条，都要提示另一种可能（05 §3.4）
        store.put(
            StoredMemory(
                m.record.model_copy(update={"confused_with": [*m.record.confused_with, r.id]}),
                m.embedding,
                m.harm_count,
            )
        )
    audit("memory.written", {"memory": r.id, "merged": False, "confused_with": r.confused_with})
    return r
