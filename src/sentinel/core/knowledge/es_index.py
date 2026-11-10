"""知识索引（es-memory 上的 knowledge-<pack> 索引）：BM25 + kNN 两路召回，客户端 RRF 融合（04 §3.5；05 §3.3）。

负责：建索引（每次全量重建，语料很小）、按检测器过滤的混合检索、索引版本校验。
不负责：语料解析（corpus.py）、向量生成（core/llm/embed.py）。
es-memory 是 Agent 自己的存储（和 Postgres 一样），不是被管系统，写它不涉及被管系统的写凭证。
"""

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from sentinel.core.knowledge.corpus import Section
from sentinel.core.retrieval.rrf import rrf

ESM_URL = os.environ.get("SENTINEL_ESM_URL", "http://localhost:9201")  # compose 里的 es-memory
META_ID = "_meta"


class KnowledgeStaleError(RuntimeError):
    pass


@dataclass(frozen=True)
class KnowledgeHit:
    doc_id: str
    title: str
    heading: str
    text: str
    source: str


def index_name(pack: str) -> str:
    return f"knowledge-{pack}"


def _client() -> httpx.Client:
    return httpx.Client(base_url=ESM_URL, timeout=30)


def build_index(
    pack: str, secs: Sequence[Section], kb_hash: str, embed: Callable[[Sequence[str]], list[list[float]]], dims: int
) -> int:
    vecs = embed([f"{s.title}. {s.heading}. {s.text}" for s in secs])
    with _client() as c:
        c.delete(f"/{index_name(pack)}")
        c.put(
            f"/{index_name(pack)}",
            json={
                "mappings": {
                    "properties": {
                        "text": {"type": "text"},
                        "heading": {"type": "text"},
                        "title": {"type": "text"},
                        "detectors": {"type": "keyword"},
                        "source": {"type": "keyword"},
                        "kb_hash": {"type": "keyword"},
                        "embedding": {"type": "dense_vector", "dims": dims, "similarity": "cosine"},
                    }
                }
            },
        ).raise_for_status()
        for s, v in zip(secs, vecs, strict=True):
            c.put(
                f"/{index_name(pack)}/_doc/{s.doc_id}",
                json={
                    "title": s.title,
                    "heading": s.heading,
                    "text": s.text,
                    "detectors": list(s.detectors),
                    "source": s.source,
                    "embedding": v,
                },
            ).raise_for_status()
        c.put(f"/{index_name(pack)}/_doc/{META_ID}?refresh=true", json={"kb_hash": kb_hash}).raise_for_status()
    return len(secs)


class EsKnowledge:
    """KnowledgeRetriever 的实现。创建时校验索引是不是由当前的 runbook 建的：不是就拒绝，避免悄悄用旧知识评测。"""

    def __init__(self, pack: str, kb_hash: str, embed: Callable[[Sequence[str]], list[list[float]]]) -> None:
        self.pack, self._embed = pack, embed
        with _client() as c:
            r = c.get(f"/{index_name(pack)}/_doc/{META_ID}")
        indexed = r.json().get("_source", {}).get("kb_hash") if r.status_code == 200 else None
        if indexed != kb_hash:
            raise KnowledgeStaleError(
                f"知识索引 {index_name(pack)} 和当前的 runbook 不一致，先运行 sentinel knowledge index"
            )

    def search(self, detectors: Sequence[str], query: str, top_k: int) -> list[KnowledgeHit]:
        # TODO(edge): 按 supports 版本范围过滤；现在只有一个 ES 主版本
        flt: dict[str, Any] = {"terms": {"detectors": list(detectors)}}
        qv = self._embed([query])[0]
        with _client() as c:
            bm = c.post(
                f"/{index_name(self.pack)}/_search",
                json={
                    "size": 10,
                    "_source": False,
                    "query": {
                        "bool": {
                            "filter": [flt],
                            "must": {"multi_match": {"query": query, "fields": ["heading^2", "title", "text"]}},
                        }
                    },
                },
            ).json()
            kn = c.post(
                f"/{index_name(self.pack)}/_search",
                json={
                    "size": 10,
                    "_source": False,
                    "knn": {"field": "embedding", "query_vector": qv, "k": 10, "num_candidates": 50, "filter": flt},
                },
            ).json()
            ids = rrf({"bm25": [h["_id"] for h in bm["hits"]["hits"]], "knn": [h["_id"] for h in kn["hits"]["hits"]]})[
                :top_k
            ]
            out = []
            for i in ids:
                s = c.get(f"/{index_name(self.pack)}/_doc/{i}").json()["_source"]
                out.append(KnowledgeHit(i, s["title"], s["heading"], s["text"], s["source"]))
        return out
