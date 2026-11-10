"""确认之后的反馈更新和自动失效（05 §3.7）。失效的记忆不删除，只是不再参与检索，审计链保留。"""

from collections.abc import Sequence
from typing import Any

from sentinel.core.memory.store import MemoryStore
from sentinel.core.memory.write import Audit


def update(
    store: MemoryStore,
    *,
    shown: Sequence[str],
    confirmed_label: str,
    harmed: Sequence[str],
    policy: dict[str, Any],
    audit: Audit,
) -> None:
    """shown：这次调查 L5 里出现过的记忆；harmed：top-1 来自它、并且 top-1 是错的那些记忆。"""
    inv = policy["invalidate"]
    for mid in shown:
        m = store.get(mid)
        if m is None:
            continue
        r = m.record
        helpful = r.helpful_count + (1 if r.root_cause_label == confirmed_label else 0)
        hits = r.hit_count + 1
        harm = m.harm_count + (1 if mid in harmed else 0)
        reason = None
        if harm >= inv["max_harm"]:
            reason = f"记忆伤害 {harm} 次"
        elif hits >= inv["min_hits"] and helpful / hits < inv["min_helpful_ratio"]:
            reason = f"命中 {hits} 次，有用率 {helpful / hits:.2f}"
        m.record = r.model_copy(update={"hit_count": hits, "helpful_count": helpful, "invalidated": reason is not None})
        m.harm_count = harm
        store.put(m)
        if reason:
            audit("memory.invalidated", {"memory": mid, "reason": reason})
        # TODO(edge): 记忆伤害本身也应该单独写审计（05 §3.7）；03 §5.1 的事件表里还没有这个类型，加之前要先确认
