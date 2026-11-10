"""L4 知识层的接口（04 §3.5）：按检测器过滤 + 问题描述检索，只放最相关的 1–2 段，每段标注来源。"""

from collections.abc import Sequence
from typing import Protocol

from sentinel.core.knowledge.es_index import KnowledgeHit


class KnowledgeRetriever(Protocol):
    def search(self, detectors: Sequence[str], query: str, top_k: int) -> list[KnowledgeHit]: ...


class NoKnowledge:
    def search(self, detectors: Sequence[str], query: str, top_k: int) -> list[KnowledgeHit]:
        return []


def render_hit(h: KnowledgeHit) -> str:
    return f"[{h.title} › {h.heading}]（来源：{h.source}）\n{h.text}"
