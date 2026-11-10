"""索引级只读工具：索引列表、索引设置、恢复进度。只读元数据，读不到文档内容（S1）。"""

from typing import Any

from sentinel.sdk import Args, ReadOnlyHttp, ReadTool


def _indices_fetch(http: ReadOnlyHttp, args: Args) -> Any:
    return http.get(
        "/_cat/indices", params={"format": "json", "h": "health,status,index,pri,rep,docs.count,store.size"}
    )


def _indices_summary(raw: Any, args: Args) -> str:
    lines = [f"indices={len(raw)} not_green={sum(1 for r in raw if r['health'] != 'green')}"]
    for r in sorted(raw, key=lambda r: r["index"]):
        if r["health"] != "green" or r["status"] != "open":
            lines.append(f"{r['index']} health={r['health']} status={r['status']} pri={r['pri']} rep={r['rep']}")
    return "\n".join(lines)


cat_indices = ReadTool(
    description="索引列表：总数，以及每个不是 green 或没有打开的索引的主分片数和副本数。",
    fetch=_indices_fetch,
    summarize=_indices_summary,
)


def _isettings_fetch(http: ReadOnlyHttp, args: Args) -> Any:
    return http.get(f"/{args['index']}/_settings", params={"flat_settings": "true"})


_INTERESTING = ("number_of_replicas", "number_of_shards", "routing.allocation", "blocks", "auto_expand_replicas")


def _isettings_summary(raw: Any, args: Args) -> str:
    lines = []
    for index, body in sorted(raw.items()):
        s = body.get("settings", {})
        picked = {k.removeprefix("index."): v for k, v in sorted(s.items()) if any(x in k for x in _INTERESTING)}
        lines.append(f"{index} " + " ".join(f"{k}={v}" for k, v in picked.items()))
    return "\n".join(lines)


index_settings = ReadTool(
    description="索引设置中和分配有关的项：分片数、副本数、分配过滤规则、只读块。参数 index 必填（可以用通配符）。",
    fetch=_isettings_fetch,
    summarize=_isettings_summary,
)


def _recovery_fetch(http: ReadOnlyHttp, args: Args) -> Any:
    return http.get(
        "/_cat/recovery",
        params={
            "format": "json",
            "active_only": "true",
            "h": "index,shard,stage,type,source_node,target_node,bytes_percent",
        },
    )


def _recovery_summary(raw: Any, args: Args) -> str:
    if not raw:
        return "没有进行中的恢复"
    return "\n".join(
        f"{r['index']}[{r['shard']}] {r['type']} {r['stage']} {r.get('source_node') or '-'}"
        f"→{r['target_node']} {r['bytes_percent']}"
        for r in raw
    )


cat_recovery = ReadTool(
    description="进行中的分片恢复（复制、迁移）及进度。",
    fetch=_recovery_fetch,
    summarize=_recovery_summary,
)
