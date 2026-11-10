"""记忆的存储（05 §3.3）：es-memory 上的一个索引，文档就是 MemoryRecord（03 §6.4）加两个只存在索引里的字段。

只存在索引里、不进契约的字段：
- embedding：向量（03 §6.4 明确规定不进契约）；
- harm_count：记忆伤害次数（05 §3.7 的失效条件要用；契约里没有这个字段，加字段要改契约，所以先放在索引里）。
fingerprint_tokens 由 symptom_fingerprint 推出，只为了能做 terms 查询。

负责：建索引、读写、三路召回各自的查询（都带 namespace 和 invalidated 过滤）。
不负责：融合和精排（retrieve.py）、写入规则（write.py）。
线上用 incidents-<pack>；事故流每次运行新建 mem-eval-<运行 ID>，用完删掉（05 §4.2）。
"""

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from sentinel.contracts import MemoryRecord
from sentinel.core.memory.fingerprint import tokens

ESM_URL = os.environ.get("SENTINEL_ESM_URL", "http://localhost:9201")


@dataclass
class StoredMemory:
    record: MemoryRecord
    embedding: list[float]
    harm_count: int = 0


class MemoryStore(Protocol):
    def ensure(self, dims: int) -> None: ...
    def drop(self) -> None: ...
    def put(self, m: StoredMemory) -> None: ...
    def get(self, memory_id: str) -> StoredMemory | None: ...
    def all(self, namespace: str) -> list[StoredMemory]: ...
    def search_terms(self, toks: Sequence[str], namespace: str, n: int) -> list[str]: ...
    def search_text(self, text: str, namespace: str, n: int) -> list[str]: ...
    def search_knn(self, vec: Sequence[float], namespace: str, n: int) -> list[str]: ...


def _doc(m: StoredMemory) -> dict[str, Any]:
    return {
        **m.record.model_dump(mode="json"),
        "fingerprint_tokens": tokens(m.record.symptom_fingerprint),
        "embedding": m.embedding,
        "harm_count": m.harm_count,
    }


def _from_doc(src: dict[str, Any]) -> StoredMemory:
    extra = {k: src.pop(k) for k in ("fingerprint_tokens", "embedding", "harm_count") if k in src}
    return StoredMemory(
        MemoryRecord.model_validate(src), list(extra.get("embedding", [])), int(extra.get("harm_count", 0))
    )


class EsMemoryStore:
    def __init__(self, index: str) -> None:
        self.index = index
        self._c = httpx.Client(base_url=ESM_URL, timeout=30)

    def _filter(self, namespace: str) -> list[dict[str, Any]]:
        return [{"term": {"namespace": namespace}}, {"term": {"invalidated": False}}]

    def ensure(self, dims: int) -> None:
        if self._c.head(f"/{self.index}").status_code == 200:
            return
        self._c.put(
            f"/{self.index}",
            json={
                "mappings": {
                    "properties": {
                        "namespace": {"type": "keyword"},
                        "root_cause_label": {"type": "keyword"},
                        "fingerprint_tokens": {"type": "keyword"},
                        "symptom_text": {"type": "text"},
                        "invalidated": {"type": "boolean"},
                        "symptom_fingerprint": {"type": "object", "enabled": False},
                        "embedding": {"type": "dense_vector", "dims": dims, "similarity": "cosine"},
                    }
                }
            },
        ).raise_for_status()

    def drop(self) -> None:
        self._c.delete(f"/{self.index}")

    def put(self, m: StoredMemory) -> None:
        self._c.put(f"/{self.index}/_doc/{m.record.id}?refresh=true", json=_doc(m)).raise_for_status()

    def get(self, memory_id: str) -> StoredMemory | None:
        r = self._c.get(f"/{self.index}/_doc/{memory_id}")
        return _from_doc(r.json()["_source"]) if r.status_code == 200 else None

    def all(self, namespace: str) -> list[StoredMemory]:
        # 记忆的规模是几十到几百条：合并时全量比较指纹就够了。TODO(edge)：规模变大后改成先用指纹召回再比较
        r = self._c.post(
            f"/{self.index}/_search",
            json={"size": 1000, "query": {"bool": {"filter": [{"term": {"namespace": namespace}}]}}},
        )
        return [_from_doc(h["_source"]) for h in r.json().get("hits", {}).get("hits", [])]

    def _ids(self, body: dict[str, Any]) -> list[str]:
        r = self._c.post(f"/{self.index}/_search", json={**body, "_source": False})
        return [h["_id"] for h in r.json().get("hits", {}).get("hits", [])]

    def search_terms(self, toks: Sequence[str], namespace: str, n: int) -> list[str]:
        return self._ids(
            {
                "size": n,
                "query": {
                    "bool": {
                        "filter": self._filter(namespace),
                        "should": [{"term": {"fingerprint_tokens": t}} for t in toks],
                        "minimum_should_match": 1,
                    }
                },
            }
        )

    def search_text(self, text: str, namespace: str, n: int) -> list[str]:
        return self._ids(
            {
                "size": n,
                "query": {"bool": {"filter": self._filter(namespace), "must": {"match": {"symptom_text": text}}}},
            }
        )

    def search_knn(self, vec: Sequence[float], namespace: str, n: int) -> list[str]:
        return self._ids(
            {
                "size": n,
                "knn": {
                    "field": "embedding",
                    "query_vector": list(vec),
                    "k": n,
                    "num_candidates": max(50, n * 5),
                    "filter": {"bool": {"filter": self._filter(namespace)}},
                },
            }
        )


class LocalMemoryStore:
    """进程内的实现：测试用（测试禁用网络），行为和 EsMemoryStore 保持一致（过滤、排序口径）。"""

    def __init__(self) -> None:
        self.items: dict[str, StoredMemory] = {}

    def ensure(self, dims: int) -> None:
        pass

    def drop(self) -> None:
        self.items.clear()

    def put(self, m: StoredMemory) -> None:
        self.items[m.record.id] = m

    def get(self, memory_id: str) -> StoredMemory | None:
        return self.items.get(memory_id)

    def all(self, namespace: str) -> list[StoredMemory]:
        return [m for m in self.items.values() if m.record.namespace == namespace]

    def _live(self, namespace: str) -> list[StoredMemory]:
        return [m for m in self.all(namespace) if not m.record.invalidated]

    def search_terms(self, toks: Sequence[str], namespace: str, n: int) -> list[str]:
        scored = [
            (len(set(toks) & set(tokens(m.record.symptom_fingerprint))), m.record.id) for m in self._live(namespace)
        ]
        return [i for s, i in sorted(scored, key=lambda x: (-x[0], x[1])) if s > 0][:n]

    def search_text(self, text: str, namespace: str, n: int) -> list[str]:
        q = set(text.lower().replace(",", " ").split())
        scored = [
            (len(q & set(m.record.symptom_text.lower().replace(",", " ").split())), m.record.id)
            for m in self._live(namespace)
        ]
        return [i for s, i in sorted(scored, key=lambda x: (-x[0], x[1])) if s > 0][:n]

    def search_knn(self, vec: Sequence[float], namespace: str, n: int) -> list[str]:
        def cos(a: Sequence[float], b: Sequence[float]) -> float:
            na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
            return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb) if na and nb else 0.0

        scored = [(cos(vec, m.embedding), m.record.id) for m in self._live(namespace)]
        return [i for _, i in sorted(scored, key=lambda x: (-x[0], x[1]))][:n]
