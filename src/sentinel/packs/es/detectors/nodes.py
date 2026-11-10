"""es.node_count：节点数少于期望值。宕机和网络分区从只读 API 看症状相同（06 §5.3），都由它发现。

期望节点数来自环境记忆 env/<集群>.yaml 的 expected_nodes（05 §2，由人维护）。
"""

from sentinel.contracts import Finding
from sentinel.sdk import DetectContext, Severity, make_finding

ID = "es.node_count"


def detect(ctx: DetectContext) -> list[Finding]:
    expected = ctx.env.get("expected_nodes")
    if expected is None:
        return []  # TODO(edge): 没有配置期望值时，可以和上一次巡检的节点数比较（需要历史，M2）
    h = ctx.tools.call("es.cluster_health")
    if not h.ok or h.raw["number_of_nodes"] >= expected:
        return []
    n = h.raw["number_of_nodes"]
    severity: Severity = "critical" if h.raw["status"] == "red" else "warn"
    return [
        make_finding(
            ctx,
            ID,
            severity=severity,
            summary=f"集群只有 {n} 个节点，期望 {expected} 个",
            metrics={"nodes": n, "expected_nodes": expected},
            observation_ids=[h.observation_id],
        )
    ]
