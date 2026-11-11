"""症状指纹（05 §3.2）：把症状离散化，再用加权 Jaccard 判断"像不像"。全部由确定性代码生成，可以单独测试。

负责：从 Finding、FactSheet、L3 变更生成指纹；token 化；加权 Jaccard。
不负责：指纹权重的取值（memory_policy）。
规则：异常事实只记档位不记数值（91% 和 93% 是同一个症状）；对象只记类型不记名称（换一个节点也能匹配）；
      变更只记设置路径不记值。
"""

from collections.abc import Mapping, Sequence
from typing import Any

from sentinel.contracts import FactSheet, Finding
from sentinel.core.context.changes import Change

PREFIXES = ("det", "fact", "obj", "chg")


def _band(value: Any) -> str:
    # 数值型的异常事实只记"偏高"；类别型的（例如集群状态）记原值
    if isinstance(value, int | float):
        return "high"
    return str(value).lower()


def _object_type(f: Finding, kinds: dict[str, str]) -> str:
    """对象类型不记具体名称（05 §3.2）：没有 object_ref 就是整个目标（记作 cluster）；
    否则取"主体等于 object_ref 的事实"的键前缀（node.disk_used_pct → node），由专家包的事实键决定，
    core 不认识具体领域（M4 D13 通用化；ES 推出来的结果和之前写死 node / index 时一样）。"""
    if f.object_ref is None:
        return "cluster"
    return kinds.get(f.object_ref, "object")


def _change_key(c: Change) -> str:
    parts = c.path.split(".")
    if parts[0] in ("persistent", "transient"):
        return ".".join(parts[1:])  # 设置路径，例如 cluster.routing.allocation.enable
    # 节点、模板这类"集合"的变化只记方向，不记具体是哪个节点或模板
    direction = "added" if c.before == "（无）" else "removed" if c.after == "（无）" else "changed"
    return f"{parts[0]}:{direction}"


def build(findings: Sequence[Finding], sheets: Sequence[FactSheet], changes: Sequence[Change]) -> dict[str, list[str]]:
    """返回 MemoryRecord.symptom_fingerprint 的结构（03 §6.4）。"""
    facts = [x for s in sheets for x in s.facts]
    kinds = {x.subject: x.key.split(".", 1)[0] for x in facts if "." in x.key}
    return {
        "detectors": sorted({f.detector for f in findings}),
        "abnormal_facts": sorted({f"{x.key}:{_band(x.value)}" for x in facts if x.status == "abnormal"}),
        "objects": sorted({_object_type(f, kinds) for f in findings}),
        "changes": sorted({f"{c.tool.split('.', 1)[-1]}:{_change_key(c)}" for c in changes}),
    }


def tokens(fp: Mapping[str, Sequence[str]]) -> list[str]:
    return (
        [f"det:{x}" for x in fp.get("detectors", [])]
        + [f"fact:{x}" for x in fp.get("abnormal_facts", [])]
        + [f"obj:{x}" for x in fp.get("objects", [])]
        + [f"chg:{x}" for x in fp.get("changes", [])]
    )


def weighted_jaccard(a: Sequence[str], b: Sequence[str], weights: Mapping[str, float]) -> float:
    sa, sb = set(a), set(b)

    def w(tokens_: set[str]) -> float:
        return sum(float(weights.get(t.split(":", 1)[0], 1.0)) for t in tokens_)

    union = w(sa | sb)
    return w(sa & sb) / union if union else 0.0
