"""es.disk_watermark：某个节点的磁盘使用率超过高水位（超过 flood stage 时是 critical）。"""

from sentinel.contracts import Finding
from sentinel.packs.es.thresholds import setting
from sentinel.sdk import DetectContext, Severity, make_finding

ID = "es.disk_watermark"
HIGH = "cluster.routing.allocation.disk.watermark.high"
FLOOD = "cluster.routing.allocation.disk.watermark.flood_stage"


def detect(ctx: DetectContext) -> list[Finding]:
    alloc = ctx.tools.call("es.cat_allocation")
    settings = ctx.tools.call("es.cluster_settings")
    if not alloc.ok:
        return []
    high = setting(settings.raw if settings.ok else None, HIGH)
    flood = setting(settings.raw if settings.ok else None, FLOOD)
    findings = []
    for row in alloc.raw:
        pct = row.get("disk.percent")
        if row.get("node") == "UNASSIGNED" or pct is None or float(pct) < high:
            continue
        severity: Severity = "critical" if float(pct) >= flood else "warn"
        findings.append(
            make_finding(
                ctx,
                ID,
                severity=severity,
                object_ref=row["node"],
                summary=f"{row['node']} 磁盘使用率 {pct}%，超过高水位 {high:g}%",
                metrics={"disk_used_pct": float(pct), "watermark_high_pct": high, "flood_stage_pct": flood},
                observation_ids=[alloc.observation_id, settings.observation_id],
            )
        )
    return findings
