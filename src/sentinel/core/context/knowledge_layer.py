"""L4 知识层的接口（04 §3.5）：按检测器过滤 + 问题描述检索，只放最相关的 1–2 段，每段标注来源。"""

from collections.abc import Sequence
from typing import Protocol

from sentinel.core.knowledge.es_index import KnowledgeHit


class KnowledgeRetriever(Protocol):
    def search(self, detectors: Sequence[str], query: str, top_k: int) -> list[KnowledgeHit]: ...


class NoKnowledge:
    def search(self, detectors: Sequence[str], query: str, top_k: int) -> list[KnowledgeHit]:
        return []


class PerPackKnowledge:
    """多个专家包各有自己的知识库：按检测器 ID 的前缀（<pack>.<名字>）选库（M4 D13 通用化）。"""

    def __init__(self, by_pack: dict[str, KnowledgeRetriever]) -> None:
        self.by_pack = by_pack

    def search(self, detectors: Sequence[str], query: str, top_k: int) -> list[KnowledgeHit]:
        packs = {d.split(".", 1)[0] for d in detectors}
        # TODO(edge)：一次调查涉及多个领域时合并各库的结果；现在一次调查只属于一个专家包（route 节点）
        r = self.by_pack.get(next(iter(packs))) if len(packs) == 1 else None
        return r.search(detectors, query, top_k) if r else []


def render_hit(h: KnowledgeHit) -> str:
    return f"[{h.title} › {h.heading}]（来源：{h.source}）\n{h.text}"
