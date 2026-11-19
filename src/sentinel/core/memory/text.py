"""记忆的文本：由系统模板生成，不用模型的原话（05 §3.1、§3.6 防投毒第 2 道防线）。

模板只由检测器 ID、事实键、档位、设置路径拼成，用英文，ES 的 standard 分词就够用（05 §3.1）。
被管系统的原文（节点名、索引名、设置值）不进入记忆文本。
"""

from collections.abc import Mapping, Sequence

# 症状指纹的示例 input example
# {
#  "detectors": ["es.disk_watermark", "es.unassigned_shards"],
#  "abnormal_facts": [
#    "cluster.status:yellow",
#    "cluster.unassigned_shards:high",
#    "node.disk_used_pct:high",
#  ],
#  "objects": ["cluster", "node"],
#  "changes": [],
# }
#
# 输出示例：
#   detectors: es.disk_watermark, es.unassigned_shards
#   | abnormal: cluster.status yellow, cluster.unassigned_shards high, node.disk_used_pct high
#   | objects: cluster, node
# （实际是一行，这里为了排版折开）


def symptom_text(fp: Mapping[str, Sequence[str]]) -> str:
    parts = [f"detectors: {', '.join(fp.get('detectors', [])) or 'none'}"]
    if fp.get("abnormal_facts"):
        parts.append("abnormal: " + ", ".join(x.replace(":", " ") for x in fp["abnormal_facts"]))
    if fp.get("objects"):
        parts.append("objects: " + ", ".join(fp["objects"]))
    if fp.get("changes"):
        parts.append("recent changes: " + ", ".join(fp["changes"]))
    return " | ".join(parts)


def root_cause_text(label: str, description: str) -> str:
    # 专家包根因表里的说明（ADR-011），不是模型写的根因描述
    return f"{label}: {description}"
