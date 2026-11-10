"""es.shard_limit：活跃分片数接近 cluster.max_shards_per_node × 数据节点数。到达上限后新建索引会直接失败。"""

from sentinel.contracts import Finding
from sentinel.packs.es.thresholds import SHARD_USAGE_WARN, setting
from sentinel.sdk import DetectContext, Severity, make_finding

ID = "es.shard_limit"


def detect(ctx: DetectContext) -> list[Finding]:
    h = ctx.tools.call("es.cluster_health")
    settings = ctx.tools.call("es.cluster_settings")
    if not h.ok:
        return []
    per_node = setting(settings.raw if settings.ok else None, "cluster.max_shards_per_node")
    limit = per_node * max(h.raw["number_of_data_nodes"], 1)
    ratio = h.raw["active_shards"] / limit
    if ratio < SHARD_USAGE_WARN:
        return []
    severity: Severity = "critical" if ratio >= 1.0 else "warn"
    return [
        make_finding(
            ctx,
            ID,
            severity=severity,
            summary=f"活跃分片 {h.raw['active_shards']} / 上限 {limit:g}（{ratio:.0%}），新建索引可能失败",
            metrics={"active_shards": h.raw["active_shards"], "shard_limit": limit, "usage_ratio": round(ratio, 3)},
            observation_ids=[h.observation_id, settings.observation_id],
        )
    ]
