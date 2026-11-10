"""es.unassigned_shards：有未分配分片。磁盘水位、节点不可用、分配被关这几类故障的共同症状（S2）。"""

from sentinel.contracts import Finding
from sentinel.sdk import DetectContext, Severity, make_finding

ID = "es.unassigned_shards"


def detect(ctx: DetectContext) -> list[Finding]:
    h = ctx.tools.call("es.cluster_health")
    if not h.ok or h.raw["unassigned_shards"] == 0:
        return []  # TODO(edge): 健康接口本身调不通时，应该产出一个"无法检测"的 Finding
    n = h.raw["unassigned_shards"]
    # 03 §3.1 的 severity_rules：>0 是 warn；集群 red（有主分片未分配）是 critical
    severity: Severity = "critical" if h.raw["status"] == "red" else "warn"
    return [
        make_finding(
            ctx,
            ID,
            severity=severity,
            summary=f"{n} 个分片未分配，集群状态 {h.raw['status']}",
            metrics={"unassigned": n, "initializing": h.raw["initializing_shards"]},
            observation_ids=[h.observation_id],
        )
    ]
