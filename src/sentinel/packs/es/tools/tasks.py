"""任务列表只读工具。"""

from typing import Any

from sentinel.sdk import Args, ReadOnlyHttp, ReadTool

# 运行超过这个时间的任务才逐条列出
_LONG_MS = 10_000


def _tasks_fetch(http: ReadOnlyHttp, args: Args) -> Any:
    # detailed=false：detailed 的描述里可能带用户写的查询内容（不可信，07 篇 T1），诊断用不到
    return http.get("/_tasks", params={"group_by": "none", "detailed": "false"})


def _tasks_summary(raw: Any, args: Args) -> str:
    tasks = raw.get("tasks", [])
    # action 以 "[c]" 结尾的是持久任务（例如 health-node、geoip-downloader），常驻运行，不是异常
    long = [t for t in tasks if t["running_time_in_nanos"] // 1_000_000 >= _LONG_MS and not t["action"].endswith("[c]")]
    lines = [f"tasks={len(tasks)} long_running={len(long)}"]
    for t in sorted(long, key=lambda t: -t["running_time_in_nanos"])[:10]:
        lines.append(
            f"{t['action']} node={t['node']} running={t['running_time_in_nanos'] // 10**9}s "
            f"cancellable={t.get('cancellable')}"
        )
    return "\n".join(lines)


tasks = ReadTool(
    description="正在执行的任务：总数，以及运行超过 10 秒的任务（最多 10 条）。",
    fetch=_tasks_fetch,
    summarize=_tasks_summary,
)
