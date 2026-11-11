"""MySQL 事实卡片（03 §3.4；04 §3.2 L1）。冷启动：baseline 为 None，有绝对阈值的按阈值判断（thresholds.py）。

主体（subject）都是 instance：单实例，问题事件没有 object_ref，记忆指纹里的对象类型是 cluster（整个目标）。
"""

from typing import Literal

from sentinel.contracts import Fact, FactSheet
from sentinel.packs.mysql.thresholds import CONN_WARN, LOCK_WAIT_S, usage
from sentinel.sdk import DetectContext, assemble_fact_sheet

INSTANCE = "instance"
Status = Literal["normal", "abnormal", "unknown"]


def instance_facts(ctx: DetectContext) -> FactSheet:
    st = ctx.tools.call("mysql.global_status")
    var = ctx.tools.call("mysql.global_variables")
    trx = ctx.tools.call("mysql.innodb_trx")
    facts: list[Fact] = []

    def add(key: str, value: float | str, status: Status, obs: str) -> None:
        facts.append(Fact(key=key, subject=INSTANCE, value=value, baseline=None, status=status, observation_id=obs))

    if st.ok and var.ok:
        u = usage(st.raw, var.raw)
        add(
            "instance.connection_usage_pct",
            round(u * 100, 1),
            "abnormal" if u >= CONN_WARN else "normal",
            st.observation_id,
        )
        add("instance.max_connections", float(var.raw["max_connections"]), "normal", var.observation_id)
        add(
            "instance.connection_rejections",
            float(st.raw["Connection_errors_max_connections"]),
            "unknown",
            st.observation_id,
        )
    if var.ok:
        ro = str(var.raw.get("read_only")).upper()
        bad = ro == "ON" and ctx.env.get("role") == "primary"
        add("instance.read_only", ro.lower(), "abnormal" if bad else "normal", var.observation_id)
    if trx.ok:
        waits = [r for r in trx.raw if r["trx_state"] == "LOCK WAIT"]
        longest_wait = max((int(r["wait_s"]) for r in waits), default=0)
        add(
            "instance.lock_waiting_trx",
            float(len(waits)),
            "abnormal" if longest_wait >= LOCK_WAIT_S else "normal",
            trx.observation_id,
        )
        add(
            "instance.longest_trx_s",
            float(max((int(r["age_s"]) for r in trx.raw), default=0)),
            "unknown",
            trx.observation_id,
        )

    # 关键项（04 §3.2）：最大连接数和只读状态即使正常也保留，模型要知道"上限是多少""是不是可写"
    keep = {"instance.max_connections", "instance.read_only"}
    return assemble_fact_sheet(ctx.target, facts, always_keep=lambda f: f.key in keep)
