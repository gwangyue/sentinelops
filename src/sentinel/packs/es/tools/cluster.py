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


def _nodes_fetch(http: ReadOnlyHttp, args: Args) -> Any:
    # 只取身份信息（名称、角色、版本）：比较节点列表的快照用，不用 nodes_stats（全是会变的数值）
    return http.get("/_nodes", params={"filter_path": "nodes.*.name,nodes.*.roles,nodes.*.version"})


def _nodes_summary(raw: Any, args: Args) -> str:
    rows = sorted(raw.get("nodes", {}).items(), key=lambda kv: kv[1]["name"])
    return "\n".join(
        f"{n['name']} id={nid[:8]} version={n['version']} roles={','.join(sorted(n['roles']))}" for nid, n in rows
    )


nodes_info = ReadTool(
    description="集群里的节点列表：名称、ID、版本、角色。看哪些节点在、哪些不在。",
    fetch=_nodes_fetch,
    summarize=_nodes_summary,
)


def _templates_fetch(http: ReadOnlyHttp, args: Args) -> Any:
    return http.get(
        "/_index_template",
        params={
            "filter_path": "index_templates.name,index_templates.index_template.index_patterns,"
            "index_templates.index_template.priority,index_templates.index_template._meta.managed"
        },
    )


def _builtin(t: dict[str, Any]) -> bool:
    # ES 自带的模板：_meta.managed=true，或者名字以 . 开头（monitoring 模板没有 managed 标记，M2 D6 实测）
    meta = (t.get("index_template") or {}).get("_meta") or {}
    return bool(meta.get("managed")) or str(t["name"]).startswith(".")


def _templates_summary(raw: Any, args: Args) -> str:
    rows = sorted(raw.get("index_templates", []), key=lambda t: t["name"])
    user = [t for t in rows if not _builtin(t)]  # 内置模板很多且和业务无关，只计数
    lines = [f"templates={len(rows)} user_defined={len(user)}"]
    for t in user:
        it = t.get("index_template") or {}
        lines.append(f"{t['name']} patterns={','.join(it.get('index_patterns', []))} priority={it.get('priority')}")
    return "\n".join(lines)


index_templates = ReadTool(
    description="索引模板：总数，以及用户自定义模板的匹配模式和优先级（内置模板只计数）。",
    fetch=_templates_fetch,
    summarize=_templates_summary,
)
