"""检索：三路召回 → 客户端 RRF → 特征精排 → 阈值（05 §3.3）。和搜索、推荐的排序链路同构。

负责：召回和精排的计算；返回每条候选的分数和各个特征，方便调试"这条记忆为什么被选中"。
不负责：渲染（provider.py）、向量的生成（调用方传入 embed 函数）。
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sentinel.core.memory.fingerprint import tokens, weighted_jaccard
from sentinel.core.memory.store import MemoryStore, StoredMemory
from sentinel.core.retrieval.rrf import rrf

# 融合候选的排序用 k=60（和 ES 的 rank_constant 一致，05 §3.3）。
# 精排特征 rrf_score 用 k=1：k=60 时归一化后的分数几乎是平的（第 1 名 1.00、第 2 名 0.98），
# 记忆少的时候每条记忆都会出现在每一路里（kNN 总会返回最近的几条，不管多不相关），
# 这个特征就给任何候选都加上约 0.29 分，阈值形同虚设（M2 D7 单元测试发现）。k=1 时第 1、2、3 名是 1.0、0.67、0.5。
FEATURE_K = 1


@dataclass(frozen=True)
class Scored:
    memory: StoredMemory
    score: float
    features: dict[str, float]


def _rank_feature(rankings: Mapping[str, Sequence[str]], weights: Mapping[str, float]) -> dict[str, float]:
    """各路按名次给分（w / (k + rank)），再除以"每一路都排第一"的满分，归一化到 [0, 1]。"""
    best = sum(weights.values()) / (FEATURE_K + 1)
    out: dict[str, float] = {}
    for route, ids in rankings.items():
        for rank, i in enumerate(ids, 1):
            out[i] = out.get(i, 0.0) + weights.get(route, 1.0) / (FEATURE_K + rank)
    return {i: v / best for i, v in out.items()}


def retrieve(
    store: MemoryStore,
    *,
    namespace: str,
    fingerprint: Mapping[str, Sequence[str]],
    text: str,
    embed: Callable[[Sequence[str]], list[list[float]]],
    policy: Mapping[str, Any],
    now: datetime,
    top_k: int,
) -> list[Scored]:
    n = int(policy["recall"]["candidates"])
    toks = tokens(fingerprint)
    rankings = {
        "fingerprint": store.search_terms(toks, namespace, n),
        "text": store.search_text(text, namespace, n),
        "knn": store.search_knn(embed([text])[0], namespace, n),
    }
    # 融合排序，返回融合后的 ID 列表（分数相同时按 ID 排序，保证确定）
    order = rrf(rankings, policy["recall"]["weights"])[:n]
    # 各路按名次给分（w / (k + rank)），再除以"每一路都排第一"的满分，归一化到 [0, 1]。
    rrf_norm = _rank_feature(rankings, policy["recall"]["weights"])
    w = policy["rerank"]["weights"]
    out = []  # 存储最终的候选记忆
    for mid in order:
        m = store.get(mid)
        if m is None or m.record.invalidated:
            continue
        r = m.record
        age = max((now - r.created_at).total_seconds() / 86400, 0.0)
        f = {
            "rrf_score": rrf_norm.get(mid, 0.0),
            "fp_jaccard": weighted_jaccard(toks, tokens(r.symptom_fingerprint), policy["fingerprint_weights"]),
            "recency": math.exp(-age / float(policy["rerank"]["recency_days"])),
            "outcome": float(policy["rerank"]["outcome_values"].get(r.outcome, 0.0)),
            "corrected": 1.0 if r.corrected else 0.0,
            "helpful_ratio": (r.helpful_count + 1) / (r.hit_count + 2),  # 平滑：样本少时不出现极端值
        }
        out.append(Scored(m, sum(float(w[k]) * v for k, v in f.items()), f))
    # 阈值：一条不相关的记忆比没有记忆更有害（05 §3.3）
    kept = [s for s in sorted(out, key=lambda s: (-s.score, s.memory.record.id)) if s.score >= policy["min_score"]]
    return kept[:top_k]
