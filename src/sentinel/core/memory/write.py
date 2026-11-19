"""确认之后写入记忆（05 §3.1、§3.4）。**只能在人确认或修正根因之后调用**：模型自己的结论不写进记忆。

负责：生成记忆文本（模板）、合并（同根因且指纹重合度够高）、标记易混淆（指纹几乎相同但根因不同）、审计。
不负责：确认这一步本身（调查图的 confirm 节点在 M3 D12；事故流评测里用真实答案模拟，05 §4.2）。
"""

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

from ulid import ULID

from sentinel.contracts import MemoryRecord
from sentinel.core.memory.fingerprint import tokens, weighted_jaccard
from sentinel.core.memory.store import MemoryStore, StoredMemory, update
from sentinel.core.memory.text import root_cause_text, symptom_text

Audit = Callable[[str, dict[str, Any]], None]  # (事件类型, payload)


def _add_confused(cur: StoredMemory, new_id: str) -> StoredMemory:
    return replace(cur, record=cur.record.model_copy(update={"confused_with": [*cur.record.confused_with, new_id]}))


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
    threshold = float(policy["merge"]["jaccard"])  # 0.8 指纹重合度阈值
    existing = [m for m in store.all(namespace) if not m.record.invalidated]
    # 计算指纹重合度, (score, doc)
    similar = [(weighted_jaccard(toks, tokens(m.record.symptom_fingerprint), weights), m) for m in existing]

    # TODO(edge): 两次调查同时确认同一类新事故时，都看不到对方，会各建一条（不会丢数据，只是没合并）
    same = [m for j, m in similar if j >= threshold and m.record.root_cause_label == root_cause_label]
    if same:
        # 合并：同一类事故又发生了一次（05 §3.4）。基于写回时的最新值计算，冲突就重读（store.update）
        def merge(cur: StoredMemory) -> StoredMemory:
            return replace(
                cur,
                record=cur.record.model_copy(
                    update={
                        "occurrences": cur.record.occurrences + 1,
                        # 追加来源调查（不去重：一次调查只会写一次记忆）
                        "source_investigations": [*cur.record.source_investigations, investigation_id],
                        "corrected": cur.record.corrected or corrected,  # 是否被人修正过根因
                        "confirmed_by": confirmed_by,  # 最近一次的确认人
                    }
                ),
            )

        # 合并到出现次数最多的那条，返回更新后的记忆
        r = update(store, max(same, key=lambda x: x.record.occurrences).record.id, merge).record
        audit("memory.written", {"memory": r.id, "merged": True, "occurrences": r.occurrences})
        return r

    text = symptom_text(fingerprint)
    # 易混淆的记忆：指纹很像，但根因不同
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
        update(store, m.record.id, partial(_add_confused, new_id=r.id))
    audit("memory.written", {"memory": r.id, "merged": False, "confused_with": r.confused_with})
    return r
