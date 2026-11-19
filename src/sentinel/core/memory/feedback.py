"""确认之后的反馈更新和自动失效（05 §3.7）。失效的记忆不删除，只是不再参与检索，审计链保留。"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from functools import partial
from typing import Any

from sentinel.core.memory.store import MemoryStore, StoredMemory
from sentinel.core.memory.store import update as update_memory
from sentinel.core.memory.write import Audit


# 判断这条记忆该不该失效；返回原因字符串（写入审计），不失效则 None。
# hits / helpful / harm：加上「这一次反馈」之后的累计（见 _apply）。
#   hits    = 进过 L5 的次数（hit_count）
#   helpful = 其中最终根因和这条记忆一致的次数（helpful_count）
#   harm    = top-1 来自它且被人事后改掉的次数（harm_count，只在索引里）
# inv = policy["invalidate"]，线上是 {min_hits: 5, min_helpful_ratio: 0.3, max_harm: 2}
# 例（test_feedback_*）：
#   (hits=1, helpful=1, harm=0, inv) → None          # 刚有用过一次，两条线都没到
#   (hits=3, helpful=1, harm=2, inv) → "记忆伤害 2 次"  # 伤害优先，不等满 5 次命中
#   (hits=5, helpful=0, harm=0, inv) → "命中 5 次，有用率 0.00"  # 总出现但从不对
def _reason(hits: int, helpful: int, harm: int, inv: Mapping[str, Any]) -> str | None:
    if harm >= inv["max_harm"]:
        return f"记忆伤害 {harm} 次"
    if hits >= inv["min_hits"] and helpful / hits < inv["min_helpful_ratio"]:
        return f"命中 {hits} 次，有用率 {helpful / hits:.2f}"
    return None


def _apply(cur: StoredMemory, *, confirmed_label: str, harmed: bool, inv: Mapping[str, Any]) -> StoredMemory:
    # 基于写回时的最新值计算：两次调查同时反馈同一条记忆，冲突的那次会重读再算（store.update）
    r = cur.record
    helpful = r.helpful_count + (1 if r.root_cause_label == confirmed_label else 0)
    hits = r.hit_count + 1
    harm = cur.harm_count + (1 if harmed else 0)
    # 失效不可逆（05 §3.7）：并发下已失效的记忆可能还会走到这里，不能因为重新计算又变回有效
    dead = r.invalidated or _reason(hits, helpful, harm, inv) is not None
    return replace(
        cur,
        record=r.model_copy(update={"hit_count": hits, "helpful_count": helpful, "invalidated": dead}),
        harm_count=harm,
    )


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
        if store.get(mid) is None:
            continue
        m = update_memory(store, mid, partial(_apply, confirmed_label=confirmed_label, harmed=mid in harmed, inv=inv))
        r = m.record
        # TODO(edge): 两次调查并发更新同一条已失效的记忆时，memory.invalidated 会重复写一次
        if r.invalidated:
            audit(
                "memory.invalidated",
                {"memory": mid, "reason": _reason(r.hit_count, r.helpful_count, m.harm_count, inv)},
            )
        # TODO(edge): 记忆伤害本身也应该单独写审计（05 §3.7）；03 §5.1 的事件表里还没有这个类型，加之前要先确认
