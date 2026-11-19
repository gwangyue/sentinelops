"""客户端 RRF 融合（05 §3.3）。S5 实测：ES 的 RRF retriever 在 basic 授权下返回 403，所以在客户端做。

score(doc) = Σ_路 w_路 / (k + rank_路(doc))，k=60 和 ES 默认的 rank_constant 一致。
"""

from collections.abc import Mapping, Sequence


# { rrf_score: 0.30, fp_jaccard: 0.30, recency: 0.10, outcome: 0.10, corrected: 0.05, helpful_ratio: 0.15 }
# 权重：rrf_score: 0.30, fp_jaccard: 0.30, recency: 0.10, outcome: 0.10, corrected: 0.05, helpful_ratio: 0.15
def rrf(rankings: Mapping[str, Sequence[str]], weights: Mapping[str, float] | None = None, k: int = 60) -> list[str]:
    """rankings：路名 → 按相关性排好序的文档 ID。返回融合后的 ID 列表（分数相同时按 ID 排序，保证确定）。"""
    scores: dict[str, float] = {}
    for route, ids in rankings.items():
        w = (weights or {}).get(route, 1.0)
        for rank, doc_id in enumerate(ids, 1):
            scores[doc_id] = scores.get(doc_id, 0.0) + w / (k + rank)
    return [d for d, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]
