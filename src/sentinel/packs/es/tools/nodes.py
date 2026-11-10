"""节点级只读工具：资源统计、热点线程。"""

from typing import Any

from pydantic import BaseModel, Field

from sentinel.sdk import Args, ReadOnlyHttp, ReadTool


class HotThreadsArgs(BaseModel):
    threads: int = Field(3, description="每个节点列出几个最热的线程")


# 只取诊断用得到的几类指标，_nodes/stats 全量输出太大（spike-results：大小待测）
_METRICS = "fs,jvm,os,thread_pool,breaker"


def _stats_fetch(http: ReadOnlyHttp, args: Args) -> Any:
    return http.get(f"/_nodes/stats/{_METRICS}")


def _stats_summary(raw: Any, args: Args) -> str:
    lines = []
    for n in sorted(raw.get("nodes", {}).values(), key=lambda n: n["name"]):
        fs = n["fs"]["total"]
        disk_pct = round(100 * (1 - fs["available_in_bytes"] / fs["total_in_bytes"]), 1)
        tp = n["thread_pool"]
        rejected = {k: tp[k]["rejected"] for k in ("write", "search", "get") if k in tp and tp[k]["rejected"]}
        tripped = {k: v["tripped"] for k, v in n.get("breakers", {}).items() if v.get("tripped")}
        lines.append(
            f"{n['name']} heap={n['jvm']['mem']['heap_used_percent']}% cpu={n['os']['cpu']['percent']}% "
            f"disk={disk_pct}% rejected={rejected or 0} breaker_tripped={tripped or 0}"
        )
    return "\n".join(lines)


nodes_stats = ReadTool(
    description="每个节点的 heap、CPU、磁盘使用率，线程池拒绝次数和熔断器触发次数（累计值）。",
    fetch=_stats_fetch,
    summarize=_stats_summary,
)


def _hot_fetch(http: ReadOnlyHttp, args: Args) -> Any:
    return http.get("/_nodes/hot_threads", params={"threads": args.get("threads", 3)})


def _hot_summary(raw: Any, args: Args) -> str:
    # hot_threads 是纯文本。标题行 "::: {节点名}{id}{...属性}" 只留节点名；
    # 每个线程只留第一行（占用比例和线程名），丢掉堆栈
    keep = []
    for ln in str(raw).splitlines():
        if ln.startswith(":::"):
            keep.append("node " + ln.split("}", 1)[0].removeprefix("::: {"))
        elif "cpu usage by thread" in ln:
            keep.append("  " + ln.strip())
    return "\n".join(keep) if keep else "没有热点线程"


hot_threads = ReadTool(
    description="每个节点最热的几个线程及其 CPU 占用（不含堆栈）。参数 threads 默认 3。",
    fetch=_hot_fetch,
    summarize=_hot_summary,
    defaults={"threads": 3},
    args_schema=HotThreadsArgs,
)
