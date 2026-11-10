"""ES 事实卡片（03 §3.4；04 §3.2 L1）。

负责：从集群健康、磁盘分配、节点统计、集群设置里抽出预先定义的事实键，判断状态，交给 sdk 筛选。
不负责：滚动基线（05 §3.5，M2）。现在处于冷启动：baseline 一律为 None；
有绝对阈值的事实按阈值判断正常/异常，没有阈值的记为 unknown。
"""

from typing import Literal

from sentinel.contracts import Fact, FactSheet
from sentinel.packs.es.thresholds import SHARD_USAGE_WARN, setting
from sentinel.sdk import DetectContext, assemble_fact_sheet

CLUSTER = "cluster"
Status = Literal["normal", "abnormal", "unknown"]


def cluster_facts(ctx: DetectContext) -> FactSheet:
    h = ctx.tools.call("es.cluster_health")
    settings = ctx.tools.call("es.cluster_settings")
    alloc = ctx.tools.call("es.cat_allocation")
    stats = ctx.tools.call("es.nodes_stats")
    s_raw = settings.raw if settings.ok else None
    facts: list[Fact] = []

    def add(key: str, subject: str, value: float | str, status: Status, obs: str) -> None:
        facts.append(Fact(key=key, subject=subject, value=value, baseline=None, status=status, observation_id=obs))

    if h.ok:
        r = h.raw
        add(
            "cluster.status", CLUSTER, r["status"], "normal" if r["status"] == "green" else "abnormal", h.observation_id
        )
        add(
            "cluster.unassigned_shards",
            CLUSTER,
            r["unassigned_shards"],
            "abnormal" if r["unassigned_shards"] else "normal",
            h.observation_id,
        )
        expected = ctx.env.get("expected_nodes")
        node_status: Status = (
            "unknown" if expected is None else ("abnormal" if r["number_of_nodes"] < expected else "normal")
        )
        add("cluster.nodes", CLUSTER, r["number_of_nodes"], node_status, h.observation_id)
        limit = setting(s_raw, "cluster.max_shards_per_node") * max(r["number_of_data_nodes"], 1)
        ratio = round(r["active_shards"] / limit, 3)
        add(
            "cluster.shard_usage_ratio",
            CLUSTER,
            ratio,
            "abnormal" if ratio >= SHARD_USAGE_WARN else "normal",
            h.observation_id,
        )

    if alloc.ok:
        high = setting(s_raw, "cluster.routing.allocation.disk.watermark.high")
        for row in alloc.raw:
            if row.get("node") == "UNASSIGNED" or row.get("disk.percent") is None:
                continue
            pct = float(row["disk.percent"])
            add("node.disk_used_pct", row["node"], pct, "abnormal" if pct >= high else "normal", alloc.observation_id)

    if stats.ok:
        for n in stats.raw.get("nodes", {}).values():
            # TODO(edge): heap、CPU、拒绝数没有绝对阈值，要等 M2 的滚动基线才能判断正常与否
            add("node.heap_used_pct", n["name"], n["jvm"]["mem"]["heap_used_percent"], "unknown", stats.observation_id)
            add("node.cpu_pct", n["name"], n["os"]["cpu"]["percent"], "unknown", stats.observation_id)
            add(
                "threadpool.write.rejected",
                n["name"],
                n["thread_pool"]["write"]["rejected"],
                "unknown",
                stats.observation_id,
            )

    # 集群级的摘要项总是保留（04 §3.2 第 3 步）
    return assemble_fact_sheet(ctx.target, facts, always_keep=lambda f: f.subject == CLUSTER)
