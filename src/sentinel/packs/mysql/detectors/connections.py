"""mysql.connection_usage：已用连接接近 max_connections（新连接会被拒绝："Too many connections"）。"""

from sentinel.contracts import Finding
from sentinel.packs.mysql.thresholds import CONN_CRITICAL, CONN_WARN, usage
from sentinel.sdk import DetectContext, Severity, make_finding

ID = "mysql.connection_usage"


def detect(ctx: DetectContext) -> list[Finding]:
    st = ctx.tools.call("mysql.global_status")
    var = ctx.tools.call("mysql.global_variables")
    if not (st.ok and var.ok):
        return []
    u = usage(st.raw, var.raw)
    if u < CONN_WARN:
        return []
    severity: Severity = "critical" if u >= CONN_CRITICAL else "warn"
    return [
        make_finding(
            ctx,
            ID,
            severity=severity,
            summary=f"连接数 {st.raw['Threads_connected']}/{var.raw['max_connections']}（{u:.0%}），接近上限",
            metrics={"connection_usage": round(u, 3), "max_connections": float(var.raw["max_connections"])},
            observation_ids=[st.observation_id, var.observation_id],
        )
    ]
