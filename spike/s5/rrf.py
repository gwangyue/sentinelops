# /// script
# requires-python = ">=3.12"
# dependencies = ["google-genai", "httpx"]
# ///
"""S5：es-memory 混合检索在 basic 授权下能否用（05 §3.3）。

负责：建索引、写 10 条假事故记忆、按三种模式检索、看正确答案（mem_disk）是否进 top-3。
不负责：精排、阈值、指纹生成（M2 D6–D7 实现）。用完即弃。
"""
import argparse
import json

import httpx
from google import genai
from google.genai import types

ES = "http://localhost:9201"
IDX = "spike-memory"
EMB_MODEL, DIMS = "gemini-embedding-001", 768

# (id, fingerprint, symptom_text)；mem_disk 是正确答案，mem_flood / mem_nodeleft 是易混淆项
DOCS = [
    ("mem_disk", ["det:es.unassigned_shards", "fact:node.disk_used_pct:high", "obj:data_node"], "未分配分片，data 节点磁盘使用率超过高水位，集群黄色"),
    ("mem_flood", ["det:es.disk_watermark", "fact:node.disk_used_pct:critical", "obj:index"], "磁盘超过 flood stage，索引被设为只读，写入报错"),
    ("mem_nodeleft", ["det:es.unassigned_shards", "fact:cluster.nodes:decreased", "obj:data_node"], "节点离开集群，副本分片未分配，集群黄色"),
    ("mem_heap", ["det:es.jvm_heap", "fact:node.heap_used_pct:high", "obj:data_node"], "JVM 堆使用率过高，频繁 GC，查询变慢"),
    ("mem_shardlimit", ["det:es.shard_limit", "fact:cluster.shards:at_limit", "obj:cluster"], "分片数达到上限，新建索引失败"),
    ("mem_alloc_off", ["det:es.unassigned_shards", "chg:cluster_settings", "obj:cluster"], "分配被关闭 allocation.enable=none，重启后分片未分配"),
    ("mem_hot", ["det:es.search_latency", "fact:node.cpu_pct:high", "obj:shard"], "热点分片导致单节点 CPU 高，搜索延迟上升"),
    ("mem_mapping", ["det:es.mapping_explosion", "fact:index.field_count:high", "obj:index"], "字段数爆炸，映射更新慢"),
    ("mem_snapshot", ["det:es.snapshot_failed", "obj:repository"], "快照失败，仓库不可访问"),
    ("mem_other_ns", ["det:es.unassigned_shards", "fact:node.disk_used_pct:high", "obj:data_node"], "未分配分片，磁盘使用率高（另一个集群）"),
]
QUERY_FP = ["det:es.unassigned_shards", "fact:node.disk_used_pct:high"]
QUERY_TEXT = "未分配分片 磁盘使用率高 集群黄色"

g = genai.Client()


def embed(texts: list[str]) -> list[list[float]]:
    r = g.models.embed_content(model=EMB_MODEL, contents=texts, config=types.EmbedContentConfig(output_dimensionality=DIMS))
    return [e.values for e in r.embeddings]


def setup(c: httpx.Client) -> None:
    c.delete(f"/{IDX}")
    c.put(f"/{IDX}", json={"mappings": {"properties": {
        "fingerprint": {"type": "keyword"},
        "symptom_text": {"type": "text"},  # spike 用 standard 分词；中文分词器的选择留给 M2
        "embedding": {"type": "dense_vector", "dims": DIMS, "similarity": "cosine"},
        "namespace": {"type": "keyword"},
        "invalidated": {"type": "boolean"},
    }}}).raise_for_status()
    vecs = embed([d[2] for d in DOCS])
    lines = []
    for (i, fp, txt), v in zip(DOCS, vecs):
        ns = "es/other" if i == "mem_other_ns" else "es/es-target"  # 过滤条件应该把它排除
        lines += [json.dumps({"index": {"_index": IDX, "_id": i}}),
                  json.dumps({"fingerprint": fp, "symptom_text": txt, "embedding": v, "namespace": ns, "invalidated": False}, ensure_ascii=False)]
    c.post("/_bulk?refresh=true", content="\n".join(lines) + "\n", headers={"content-type": "application/x-ndjson"}).raise_for_status()


FILTER = {"bool": {"filter": [{"term": {"namespace": "es/es-target"}}, {"term": {"invalidated": False}}]}}


def sub_queries(qv):
    return (
        {"terms": {"fingerprint": QUERY_FP}},
        {"match": {"symptom_text": QUERY_TEXT}},
        {"field": "embedding", "query_vector": qv, "k": 20, "num_candidates": 100},
    )


def server(c: httpx.Client, qv, weighted: bool):
    fp, txt, knn = sub_queries(qv)
    rets = [{"standard": {"query": fp}}, {"standard": {"query": txt}}, {"knn": knn}]
    if weighted:
        # 05 §3.3 原文写法：第一路用包装格式带 weight，其余两路是普通格式
        rets[0] = {"retriever": rets[0], "weight": 2.0}
    body = {"retriever": {"rrf": {"retrievers": rets, "rank_window_size": 20, "filter": FILTER}}, "_source": False, "size": 5}
    r = c.post(f"/{IDX}/_search", json=body)
    return r.status_code, r.json()


def client_side(c: httpx.Client, qv, k: int = 60):
    # 客户端 RRF：score = Σ w / (k + rank)；k=60 和 ES 默认 rank_constant 一致
    fp, txt, knn = sub_queries(qv)
    bodies = [
        (2.0, {"query": {"bool": {"must": fp, "filter": FILTER}}}),
        (1.0, {"query": {"bool": {"must": txt, "filter": FILTER}}}),
        (1.0, {"knn": {**knn, "filter": FILTER}}),
    ]
    scores: dict[str, float] = {}
    for w, b in bodies:
        hits = c.post(f"/{IDX}/_search", json={**b, "size": 20, "_source": False}).raise_for_status().json()["hits"]["hits"]
        for rank, h in enumerate(hits, 1):
            scores[h["_id"]] = scores.get(h["_id"], 0) + w / (k + rank)
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:5]
    return 200, {"hits": {"hits": [{"_id": i, "_score": s} for i, s in ranked]}}


ap = argparse.ArgumentParser()
ap.add_argument("--mode", choices=["server-weighted", "server-plain", "client"], required=True)
args = ap.parse_args()
with httpx.Client(base_url=ES, timeout=30) as c:
    setup(c)
    qv = embed([QUERY_TEXT])[0]
    status, res = client_side(c, qv) if args.mode == "client" else server(c, qv, args.mode == "server-weighted")
    print("mode:", args.mode, "status:", status)
    if status != 200:
        print("error:", json.dumps(res.get("error", res), ensure_ascii=False)[:600])
    else:
        ids = [h["_id"] for h in res["hits"]["hits"]]
        print("top5:", ids)
        print("PASS" if "mem_disk" in ids[:3] and "mem_other_ns" not in ids else "FAIL", "mem_disk 进 top-3 且 mem_other_ns 被过滤")
