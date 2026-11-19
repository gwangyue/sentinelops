"""ES 的 5 个修复动作（03 §4.2；场景的 acceptable_actions）。规格在同目录的 <动作 ID>.yaml 里。

负责：每个动作的前置检查、预演、执行、回滚、验证。
- 前置检查、预演、验证只用只读工具，而且只用录制时一定录到的调用（无参数的默认调用，或者按索引的调用），
  这样回放模式下也能跑（ADR-003）；
- 执行、回滚只用执行器传进来的写连接（ctx.conn），回滚按前置检查快照里的原值恢复。
不负责：谁来调用、审批、幂等（plan 节点和执行器）。
"""

from collections.abc import Mapping
from typing import Any

from sentinel.packs.es.thresholds import DEFAULTS, setting
from sentinel.sdk import ActionContext, CheckResult, ExecContext, ToolCaller, VerifyResult

ENABLE = "cluster.routing.allocation.enable"  # 启用分配
HIGH = "cluster.routing.allocation.disk.watermark.high"
FLOOD = "cluster.routing.allocation.disk.watermark.flood_stage"
LOW = "cluster.routing.allocation.disk.watermark.low"
# ES 9：显式设置水位之后，对应的 max_headroom 默认值会从 150GB / 200GB 变成 -1，和另一个水位的默认 headroom
# 冲突，ES 返回 400（M3 D11 录制时实测）。所以水位和它的 headroom 一起设，headroom 用 ES 的默认值
HIGH_HEADROOM = "cluster.routing.allocation.disk.watermark.high.max_headroom"
LOW_HEADROOM = "cluster.routing.allocation.disk.watermark.low.max_headroom"
# 回滚要恢复的 4 项：low、high 和各自的 headroom
WATERMARK_KEYS = (LOW, LOW_HEADROOM, HIGH, HIGH_HEADROOM)
MAX_SHARDS = "cluster.max_shards_per_node"


def _raw(tools: ToolCaller, tool: str, args: Mapping[str, Any] | None = None) -> Any:
    r = tools.call(tool, dict(args) if args else None)
    if not r.ok:
        raise RuntimeError(f"{tool} 调用失败（{r.status}）：{r.digest}")
    return r.raw


def _explicit(settings: Mapping[str, Any], key: str) -> dict[str, Any]:
    """persistent 和 transient 里各自的原值（没有显式设置就是 None）：回滚要按原样恢复两处。"""
    return {scope: settings.get(scope, {}).get(key) for scope in ("persistent", "transient")}


def _restore(ctx: ExecContext, key: str) -> dict[str, Any]:
    before = ctx.precheck_snapshot["before"]
    body = {scope: {key: before.get(scope)} for scope in ("persistent", "transient")}
    return dict(ctx.conn.put("/_cluster/settings", body))


def _health(tools: ToolCaller) -> dict[str, Any]:
    return dict(_raw(tools, "es.cluster_health"))


# ---------- es.enable_allocation ----------


# 前置检查：检查是否需要启用分配
def enable_allocation_precheck(ctx: ActionContext) -> CheckResult:
    s = _raw(ctx.tools, "es.cluster_settings")  # 获取集群设置
    before = _explicit(s, ENABLE)  # 获取设置值，eg: {"persistent": "all", "transient": "all"}
    effective = before["transient"] or before["persistent"] or "all"  # 获取集群设置的值
    if effective == "all":
        return CheckResult(False, f"{ENABLE} 已经是 all，这个动作不会有效果", {"before": before})
    return CheckResult(True, f"{ENABLE}={effective}", {"before": before})


def enable_allocation_dry_run(ctx: ActionContext) -> dict[str, Any]:
    return {"unassigned_shards": int(_health(ctx.tools).get("unassigned_shards", 0)), "settings_changed": 1}


def enable_allocation_execute(ctx: ExecContext) -> dict[str, Any]:
    # 两处都清空：恢复成默认值 all（transient 优先生效，只清 persistent 不够）
    return dict(ctx.conn.put("/_cluster/settings", {"persistent": {ENABLE: None}, "transient": {ENABLE: None}}))


def enable_allocation_rollback(ctx: ExecContext) -> dict[str, Any]:
    return _restore(ctx, ENABLE)


def unassigned_zero_verify(ctx: ActionContext) -> VerifyResult:
    h = _health(ctx.tools)
    n = int(h.get("unassigned_shards", -1))
    return VerifyResult(n == 0, f"unassigned_shards={n} status={h.get('status')}")


# ---------- es.raise_watermark_temporarily ----------


def raise_watermark_precheck(ctx: ActionContext) -> CheckResult:
    # 决定"新分片能不能放到这个节点"的是低水位，不是高水位（高水位决定要不要把分片迁走）。
    # 只提高高水位的话，93% 的节点仍然高于低水位 85%，新副本照样分配不上去（M3 D11 录制时实测）
    s = _raw(ctx.tools, "es.cluster_settings")
    low, high, flood = setting(s, LOW), setting(s, HIGH), setting(s, FLOOD)
    want = float(ctx.params["high_pct"])
    rows = _raw(ctx.tools, "es.cat_allocation")
    used = {r["node"]: float(r["disk.percent"]) for r in rows if r.get("disk.percent") not in (None, "")}
    over = sorted(n for n, p in used.items() if p >= low)
    snap = {
        "before": {k: _explicit(s, k) for k in WATERMARK_KEYS},
        "low": low,
        "high": high,
        "flood": flood,
        "nodes_over_low": over,
    }
    if not over:
        return CheckResult(False, f"没有节点超过低水位 {low:g}%", snap)
    if not low < want < flood:
        return CheckResult(False, f"新的水位 {want:g}% 必须在当前低水位 {low:g}% 和洪水位 {flood:g}% 之间", snap)
    if max(used[n] for n in over) >= want:
        return CheckResult(False, f"{over} 的使用率不低于 {want:g}%，提高到这个值也分配不上去", snap)
    return CheckResult(True, f"{over} 超过低水位 {low:g}%，低水位和高水位临时提高到 {want:g}%", snap)


def raise_watermark_dry_run(ctx: ActionContext) -> dict[str, Any]:
    return {"unassigned_shards": int(_health(ctx.tools).get("unassigned_shards", 0)), "settings_changed": 1}


def raise_watermark_execute(ctx: ExecContext) -> dict[str, Any]:
    want = float(ctx.params["high_pct"])
    # low ≤ high ≤ flood：低水位提高到 want；高水位不低于 want（原来更高就保持原值）
    high = max(want, float(ctx.precheck_snapshot["high"]))
    body = {LOW: f"{want:g}%", LOW_HEADROOM: "200GB", HIGH: f"{high:g}%", HIGH_HEADROOM: "150GB"}
    return dict(ctx.conn.put("/_cluster/settings", {"persistent": body}))


def raise_watermark_rollback(ctx: ExecContext) -> dict[str, Any]:
    # 4 项一起按原样恢复：只恢复水位的话，留下的 headroom 显式值会改变以后的水位计算
    before = ctx.precheck_snapshot["before"]
    body = {scope: {k: before[k].get(scope) for k in WATERMARK_KEYS} for scope in ("persistent", "transient")}
    return dict(ctx.conn.put("/_cluster/settings", body))


def green_verify(ctx: ActionContext) -> VerifyResult:
    h = _health(ctx.tools)
    return VerifyResult(h.get("status") == "green", f"status={h.get('status')} unassigned={h.get('unassigned_shards')}")


# ---------- es.relocate_shards ----------


def relocate_precheck(ctx: ActionContext) -> CheckResult:
    src, n = str(ctx.params["from_node"]), int(ctx.params["max_shards"])
    s = _raw(ctx.tools, "es.cluster_settings")
    high = setting(s, HIGH)
    rows = _raw(ctx.tools, "es.cat_allocation")
    used = {r["node"]: float(r["disk.percent"]) for r in rows if r.get("disk.percent") not in (None, "")}
    if src not in used:
        return CheckResult(False, f"节点 {src} 不存在", {"used": used})
    targets = sorted((p, node) for node, p in used.items() if node != src and p < high - 5)
    if not targets:
        return CheckResult(False, f"没有磁盘使用率低于 {high - 5:g}% 的目标节点", {"used": used})
    # 迁移哪些分片在前置检查时就定下来（进入 plan_hash）：从录制里一定有的 cat_shards 默认调用里挑
    shards = [
        {"index": r["index"], "shard": int(r["shard"])}
        for r in _raw(ctx.tools, "es.cat_shards")
        if r.get("node") == src and r.get("state") == "STARTED" and not str(r["index"]).startswith(".")
    ][:n]
    if not shards:
        return CheckResult(False, f"{src} 上没有可以迁移的业务分片", {"used": used})
    moves = [{**sh, "from": src, "to": targets[i % len(targets)][1]} for i, sh in enumerate(shards)]
    return CheckResult(True, f"从 {src} 迁移 {len(moves)} 个分片", {"used": used, "moves": moves})


def relocate_dry_run(ctx: ActionContext) -> dict[str, Any]:
    pre = relocate_precheck(ctx)
    return {"shards": len(pre.snapshot.get("moves", []))}


def _reroute(ctx: ExecContext, moves: list[dict[str, Any]], reverse: bool) -> dict[str, Any]:
    cmds = [
        {
            "move": {
                "index": m["index"],
                "shard": m["shard"],
                "from_node": m["to"] if reverse else m["from"],
                "to_node": m["from"] if reverse else m["to"],
            }
        }
        for m in moves
    ]
    return dict(ctx.conn.post("/_cluster/reroute", {"commands": cmds}))


def relocate_execute(ctx: ExecContext) -> dict[str, Any]:
    return _reroute(ctx, list(ctx.precheck_snapshot["moves"]), reverse=False)


def relocate_rollback(ctx: ExecContext) -> dict[str, Any]:
    # 把迁走的分片迁回原节点。TODO(edge)：迁移还在进行中时应该先 cancel，再迁回
    return _reroute(ctx, list(ctx.precheck_snapshot["moves"]), reverse=True)


def relocate_verify(ctx: ActionContext) -> VerifyResult:
    h = _health(ctx.tools)
    done = int(h.get("relocating_shards", 0)) == 0
    return VerifyResult(
        done and h.get("status") == "green", f"relocating={h.get('relocating_shards')} {h.get('status')}"
    )


# ---------- es.raise_shard_limit_temporarily ----------


def shard_limit_precheck(ctx: ActionContext) -> CheckResult:
    s = _raw(ctx.tools, "es.cluster_settings")
    limit = setting(s, MAX_SHARDS)
    h = _health(ctx.tools)
    data_nodes, active = int(h["number_of_data_nodes"]), int(h["active_shards"])
    want = int(ctx.params["max_shards_per_node"])
    snap = {"before": _explicit(s, MAX_SHARDS), "limit": limit, "active": active, "data_nodes": data_nodes}
    ratio = active / (limit * data_nodes)
    if ratio < 0.9:
        return CheckResult(False, f"分片用量 {ratio:.0%}，没有接近上限", snap)
    # 临时提高：最多到原值的 2 倍，也不超过 ES 的默认值（防止参数写错导致"上限形同虚设"）
    if not limit < want <= min(limit * 2, DEFAULTS[MAX_SHARDS]):
        return CheckResult(
            False, f"新的上限 {want} 必须大于 {limit:g}、不超过 {min(limit * 2, DEFAULTS[MAX_SHARDS]):g}", snap
        )
    return CheckResult(True, f"分片用量 {ratio:.0%}（{active}/{limit:g}×{data_nodes}），临时提高到 {want}", snap)


def shard_limit_dry_run(ctx: ActionContext) -> dict[str, Any]:
    pre = shard_limit_precheck(ctx)
    return {"settings_changed": 1, "new_capacity": int(ctx.params["max_shards_per_node"]) * pre.snapshot["data_nodes"]}


def shard_limit_execute(ctx: ExecContext) -> dict[str, Any]:
    return dict(
        ctx.conn.put("/_cluster/settings", {"persistent": {MAX_SHARDS: int(ctx.params["max_shards_per_node"])}})
    )


def shard_limit_rollback(ctx: ExecContext) -> dict[str, Any]:
    return _restore(ctx, MAX_SHARDS)


def shard_limit_verify(ctx: ActionContext) -> VerifyResult:
    s = _raw(ctx.tools, "es.cluster_settings")
    h = _health(ctx.tools)
    ratio = int(h["active_shards"]) / (setting(s, MAX_SHARDS) * int(h["number_of_data_nodes"]))
    return VerifyResult(ratio < 0.9, f"分片用量 {ratio:.0%}")


# ---------- es.set_replicas ----------


def _index_row(tools: ToolCaller, index: str) -> dict[str, Any] | None:
    return next((r for r in _raw(tools, "es.cat_indices") if r["index"] == index), None)


def set_replicas_precheck(ctx: ActionContext) -> CheckResult:
    index, want = str(ctx.params["index"]), int(ctx.params["replicas"])
    row = _index_row(ctx.tools, index)
    data_nodes = int(_health(ctx.tools)["number_of_data_nodes"])
    if row is None:
        return CheckResult(False, f"索引 {index} 不存在", {})
    rep = int(row["rep"])
    snap = {"before": rep, "data_nodes": data_nodes}
    if rep <= data_nodes - 1:
        return CheckResult(False, f"{index} 的副本数 {rep} 没有超过可分配的上限 {data_nodes - 1}", snap)
    if not 0 <= want <= data_nodes - 1:
        return CheckResult(False, f"新的副本数 {want} 必须在 0 到 {data_nodes - 1} 之间", snap)
    return CheckResult(True, f"{index} 副本数 {rep} → {want}（{data_nodes} 个数据节点）", snap)


def set_replicas_dry_run(ctx: ActionContext) -> dict[str, Any]:
    row = _index_row(ctx.tools, str(ctx.params["index"]))
    return {"indices": 1, "replicas_removed": int(row["rep"]) - int(ctx.params["replicas"]) if row else 0}


def set_replicas_execute(ctx: ExecContext) -> dict[str, Any]:
    body = {"index": {"number_of_replicas": int(ctx.params["replicas"])}}
    return dict(ctx.conn.put(f"/{ctx.params['index']}/_settings", body))


def set_replicas_rollback(ctx: ExecContext) -> dict[str, Any]:
    body = {"index": {"number_of_replicas": int(ctx.precheck_snapshot["before"])}}
    return dict(ctx.conn.put(f"/{ctx.params['index']}/_settings", body))


def set_replicas_verify(ctx: ActionContext) -> VerifyResult:
    row = _index_row(ctx.tools, str(ctx.params["index"]))
    health = row["health"] if row else "missing"
    return VerifyResult(health == "green", f"{ctx.params['index']} health={health}")
