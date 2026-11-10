"""集群级只读工具：健康状态、集群设置。"""

from typing import Any

from sentinel.sdk import Args, ReadOnlyHttp, ReadTool


def _health_fetch(http: ReadOnlyHttp, args: Args) -> Any:
    return http.get("/_cluster/health")


def _health_summary(raw: Any, args: Args) -> str:
    keys = (
        "status",
        "number_of_nodes",
        "number_of_data_nodes",
        "active_primary_shards",
        "active_shards",
        "relocating_shards",
        "initializing_shards",
        "unassigned_shards",
        "delayed_unassigned_shards",
    )
    return " ".join(f"{k}={raw.get(k)}" for k in keys)


health = ReadTool(
    description="集群健康：状态（green/yellow/red）、节点数、各类分片计数。排查任何问题的第一步。",
    fetch=_health_fetch,
    summarize=_health_summary,
)


def _settings_fetch(http: ReadOnlyHttp, args: Args) -> Any:
    # 只取显式设置过的项：默认值不会是"最近改了什么"，而 include_defaults 的输出有上百 KB
    return http.get("/_cluster/settings", params={"flat_settings": "true"})


def _settings_summary(raw: Any, args: Args) -> str:
    lines = [f"{scope} {k}={v}" for scope in ("persistent", "transient") for k, v in sorted(raw.get(scope, {}).items())]
    return "\n".join(lines) if lines else "没有显式设置的集群参数（全部是默认值）"


settings = ReadTool(
    description="集群设置中被显式修改过的项（persistent 和 transient），例如分配开关、磁盘水位、分片上限。",
    fetch=_settings_fetch,
    summarize=_settings_summary,
)
