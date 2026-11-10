"""分片分配相关的只读工具：每个节点的磁盘、分片列表、分配解释。"""

from collections import Counter
from typing import Any

from pydantic import BaseModel, Field

from sentinel.sdk import Args, ReadOnlyHttp, ReadTool


class ShardsArgs(BaseModel):
    index: str | None = Field(None, description="只看这个索引（可以用通配符）；不填就是全部索引")


class ExplainArgs(BaseModel):
    index: str | None = Field(None, description="索引名；三个参数都不填时，解释第一个未分配的分片")
    shard: int | None = Field(None, description="分片编号")
    primary: bool | None = Field(None, description="true = 主分片，false = 副本")


def _alloc_fetch(http: ReadOnlyHttp, args: Args) -> Any:
    return http.get("/_cat/allocation", params={"format": "json", "bytes": "b"})


def _alloc_summary(raw: Any, args: Args) -> str:
    lines = []
    for r in raw:
        if r.get("node") == "UNASSIGNED":
            lines.append(f"UNASSIGNED shards={r.get('shards')}")
            continue
        lines.append(
            f"{r['node']} disk.percent={r.get('disk.percent')} shards={r.get('shards')} "
            f"disk.avail={int(r.get('disk.avail') or 0) // 2**20}MB"
        )
    return "\n".join(lines)


cat_allocation = ReadTool(
    description="每个节点的磁盘使用率、可用空间和分片数。判断磁盘水位问题时使用。",
    fetch=_alloc_fetch,
    summarize=_alloc_summary,
)


def _shards_fetch(http: ReadOnlyHttp, args: Args) -> Any:
    path = f"/_cat/shards/{args['index']}" if args.get("index") else "/_cat/shards"
    return http.get(path, params={"format": "json", "h": "index,shard,prirep,state,node,unassigned.reason"})


def _shards_summary(raw: Any, args: Args) -> str:
    # 正常的 STARTED 分片只计数，不逐行列出：模型关心的是不正常的那些
    states = Counter(r["state"] for r in raw)
    lines = [" ".join(f"{s}={n}" for s, n in sorted(states.items()))]
    for r in raw:
        if r["state"] != "STARTED":
            kind = "primary" if r["prirep"] == "p" else "replica"
            lines.append(
                f"{r['index']}[{r['shard']}] {kind} {r['state']} node={r.get('node') or '-'} "
                f"reason={r.get('unassigned.reason') or '-'}"
            )
    return "\n".join(lines)


cat_shards = ReadTool(
    description="分片列表：只逐条列出不是 STARTED 的分片（未分配、初始化中、迁移中）及原因；"
    "可选参数 index 按索引过滤。",
    fetch=_shards_fetch,
    summarize=_shards_summary,
    defaults={"index": None},
    args_schema=ShardsArgs,
)


def _explain_fetch(http: ReadOnlyHttp, args: Args) -> Any:
    # 不带参数时，ES 解释它找到的第一个未分配分片
    body = {k: args[k] for k in ("index", "shard", "primary") if args.get(k) is not None}
    return http.get("/_cluster/allocation/explain", body=body or None)


def _explain_summary(raw: Any, args: Args) -> str:
    # 04 §3.3 的例子：只提取每个节点的决定和原因
    head = (
        f"{raw.get('index')}[{raw.get('shard')}] {'primary' if raw.get('primary') else 'replica'} "
        f"state={raw.get('current_state')} can_allocate={raw.get('can_allocate')}"
    )
    lines = [head]
    if raw.get("allocate_explanation"):
        lines.append(f"explanation: {raw['allocate_explanation']}")
    unassigned = raw.get("unassigned_info") or {}
    if unassigned:
        lines.append(
            f"unassigned: reason={unassigned.get('reason')} last_status={unassigned.get('last_allocation_status')}"
        )
    for n in raw.get("node_allocation_decisions", []):
        reasons = "; ".join(
            f"[{d['decider']}] {d['explanation']}" for d in n.get("deciders", []) if d.get("decision") == "NO"
        )
        lines.append(f"{n.get('node_name')}: {n.get('node_decision')} {reasons}".rstrip())
    return "\n".join(lines)


allocation_explain = ReadTool(
    description="解释某个分片为什么没有被分配：每个节点的分配决定和拒绝原因（例如磁盘超过水位、分配被关闭）。"
    "参数 index、shard、primary 都不填时，解释第一个未分配的分片。",
    fetch=_explain_fetch,
    summarize=_explain_summary,
    defaults={"index": None, "shard": None, "primary": None},
    args_schema=ExplainArgs,
)
