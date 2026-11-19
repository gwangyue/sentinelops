"""记忆的存储（05 §3.3）：es-memory 上的一个索引，文档就是 MemoryRecord（03 §6.4）加两个只存在索引里的字段。

只存在索引里、不进契约的字段：
- embedding：向量（03 §6.4 明确规定不进契约）；
- harm_count：记忆伤害次数（05 §3.7 的失效条件要用；契约里没有这个字段，加字段要改契约，所以先放在索引里）。
fingerprint_tokens 由 symptom_fingerprint 推出，只为了能做 terms 查询。
ES 9 默认不在 _source 里返回 dense_vector（M2 D8 实测 9.5.3），读记忆时要显式要回向量：
否则合并、易混淆标记、反馈这些"读出来改了再写回"的操作会把向量写成空数组，ES 拒绝写入。
"读出来改了再写回"带乐观锁（读到的 _seq_no/_primary_term 写回时校验，冲突就重读重算，见 update()）：
两次调查同时确认、L5 里有同一条记忆时，不加锁会丢一次更新（教程 01 §3.7 在 ES 9.5.3 上复现）。
三路召回同分时按记忆 ID 排序：ES 默认按内部文档顺序，而每次反馈都会重写文档，
易混淆的两条记忆（指纹、文本、向量都相同）谁排第一会随之翻转（教程 01 §3.5 实测）。

负责：建索引、读写、三路召回各自的查询（都带 namespace 和 invalidated 过滤）。
不负责：融合和精排（retrieve.py）、写入规则（write.py）。
线上用 incidents-<pack>；事故流每次运行新建 mem-eval-<运行 ID>，用完删掉（05 §4.2）。
"""

import copy
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from sentinel.contracts import MemoryRecord
from sentinel.core.memory.fingerprint import tokens

ESM_URL = os.environ.get("SENTINEL_ESM_URL", "http://localhost:9201")
# 同分时按记忆 ID（ULID，先写入的在前）排序，和 LocalMemoryStore 的 (-分数, id) 一致。
# id 字段走动态映射（text + keyword）；索引里还没有文档时 id.keyword 不存在，unmapped_type 防止查询报错
TIE_SORT = ["_score", {"id.keyword": {"order": "asc", "unmapped_type": "keyword"}}]
UPDATE_ATTEMPTS = 3  # 写回时版本对不上：读出来之后，别的调查已经改过这条记忆。


class MemoryConflict(Exception):
    """写回时版本对不上：读出来之后，别的调查已经改过这条记忆。"""


@dataclass
class StoredMemory:
    record: MemoryRecord
    embedding: list[float]
    harm_count: int = 0
    # 读出来时的版本（ES 的 _seq_no、_primary_term）。put 时带着它就做乐观锁；新建的记忆为 None，不校验
    seq: tuple[int, int] | None = None


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
        **m.record.model_dump(mode="json"),  # 把 MemoryRecord 转换为 JSON 字典
        "fingerprint_tokens": tokens(m.record.symptom_fingerprint),
        "embedding": m.embedding,
        "harm_count": m.harm_count,
    }


def _from_doc(src: dict[str, Any], seq: tuple[int, int] | None = None) -> StoredMemory:
    extra = {k: src.pop(k) for k in ("fingerprint_tokens", "embedding", "harm_count") if k in src}
    return StoredMemory(
        # 把 JSON 字典转换为 MemoryRecord
        MemoryRecord.model_validate(src),
        list(extra.get("embedding", [])),
        int(extra.get("harm_count", 0)),
        seq,
    )


# 从 ES 的 _seq_no/_primary_term 中提取版本号（_seq_no: 序列号，_primary_term: 主分片号）
def _seq(hit: dict[str, Any]) -> tuple[int, int] | None:
    return (int(hit["_seq_no"]), int(hit["_primary_term"])) if "_seq_no" in hit else None


def update(store: "MemoryStore", memory_id: str, change: Callable[[StoredMemory], StoredMemory]) -> StoredMemory:
    """读 → change → 带版本写回；版本冲突就重读、重新计算（change 必须基于传入的当前值计算）。

    返回写入后的记忆。重试 UPDATE_ATTEMPTS 次仍冲突就抛 MemoryConflict。
    """
    for _ in range(UPDATE_ATTEMPTS):
        cur = store.get(memory_id)
        # 记忆只失效、不删除（05 §3.7），读不到说明调用方给错了 ID
        if cur is None:
            raise KeyError(f"记忆 {memory_id} 不存在")
        new = change(cur)  # 调用 change 函数，传入当前记忆，返回新的记忆
        try:
            store.put(new)
            return new
        except MemoryConflict:
            continue  # TODO(edge): 冲突次数没有写审计，也没有退避
    raise MemoryConflict(f"记忆 {memory_id} 连续 {UPDATE_ATTEMPTS} 次写回冲突")


class EsMemoryStore:
    def __init__(self, index: str) -> None:
        self.index = index
        self._c = httpx.Client(base_url=ESM_URL, timeout=30)

    def _filter(self, namespace: str) -> list[dict[str, Any]]:
        return [{"term": {"namespace": namespace}}, {"term": {"invalidated": False}}]

    # 确保索引存在
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
        params: dict[str, Any] = {"refresh": "true"}  # 刷新索引，确保写入的记忆立即可见
        if m.seq is not None:
            # 乐观锁，确保写入的记忆是读出来之后的版本
            params |= {"if_seq_no": m.seq[0], "if_primary_term": m.seq[1]}
        r = self._c.put(f"/{self.index}/_doc/{m.record.id}", params=params, json=_doc(m))
        if r.status_code == 409:
            # 写回时版本对不上：读出来之后，别的调查已经改过这条记忆
            raise MemoryConflict(f"记忆 {m.record.id} 在读出之后被改过")
        if r.status_code >= 400:
            # 带上 ES 的错误原因：只看状态码查不出是哪个字段出的问题
            raise RuntimeError(f"写入记忆 {m.record.id} 失败（{r.status_code}）：{r.text[:500]}")

    def get(self, memory_id: str) -> StoredMemory | None:
        # 读取记忆，连同向量一起返回
        r = self._c.get(f"/{self.index}/_doc/{memory_id}", params={"_source_exclude_vectors": "false"})
        if r.status_code != 200:
            return None
        d = r.json()
        return _from_doc(d["_source"], _seq(d))

    def all(self, namespace: str) -> list[StoredMemory]:
        # 记忆的规模是几十到几百条：合并时全量比较指纹就够了。TODO(edge)：规模变大后改成先用指纹召回再比较
        r = self._c.post(
            f"/{self.index}/_search",
            json={
                "size": 1000,
                "_source": {"exclude_vectors": False},
                "seq_no_primary_term": True,
                "query": {"bool": {"filter": [{"term": {"namespace": namespace}}]}},
            },
        )
        return [_from_doc(h["_source"], _seq(h)) for h in r.json().get("hits", {}).get("hits", [])]

    def _ids(self, body: dict[str, Any]) -> list[str]:
        r = self._c.post(f"/{self.index}/_search", json={**body, "sort": TIE_SORT, "_source": False})
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
    """进程内的实现：测试用（测试禁用网络），行为和 EsMemoryStore 保持一致（过滤、排序口径、乐观锁）。

    读出来的是副本、带版本号：和 ES 一样，改副本不影响存储，写回时版本对不上就抛 MemoryConflict。
    以前直接返回存储里的对象，"改完写回"永远不会冲突，测不出丢更新。
    """

    def __init__(self) -> None:
        self.items: dict[str, StoredMemory] = {}
        self._ver: dict[str, int] = {}

    def ensure(self, dims: int) -> None:
        pass

    def drop(self) -> None:
        self.items.clear()
        self._ver.clear()

    def put(self, m: StoredMemory) -> None:
        mid = m.record.id
        if m.seq is not None and m.seq[0] != self._ver.get(mid):
            raise MemoryConflict(f"记忆 {mid} 在读出之后被改过")
        self._ver[mid] = self._ver.get(mid, -1) + 1
        self.items[mid] = copy.deepcopy(m)
        self.items[mid].seq = None

    def _copy(self, m: StoredMemory) -> StoredMemory:
        c = copy.deepcopy(m)
        c.seq = (self._ver.get(m.record.id, 0), 1)
        return c

    def get(self, memory_id: str) -> StoredMemory | None:
        m = self.items.get(memory_id)
        return self._copy(m) if m is not None else None

    def all(self, namespace: str) -> list[StoredMemory]:
        return [self._copy(m) for m in self.items.values() if m.record.namespace == namespace]

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
