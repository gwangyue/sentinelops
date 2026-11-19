"""事故记忆 MemoryRecord（03 §6.4）：只在人确认根因之后写入。embedding 只存在 es-memory 的索引里。

例子取自本机 es-memory 索引 incidents-es 的文档 mem_01M74E05SVWD7FTG27A6NMEPN9（磁盘水位，人确认）。
"""

from datetime import datetime
from typing import Any, Literal

from sentinel.contracts._base import Contract


# 一次人确认过的事故：症状指纹 → 根因 → 采取的动作和结果。检索时作为上下文的 L5 层。
class MemoryRecord(Contract):
    schema_version: int = 1  # 例：1
    id: str  # mem_ + ULID。例："mem_01M74E05SVWD7FTG27A6NMEPN9"
    namespace: str  # <专家包>/<集群>，检索只在同一命名空间内进行。例："es/es-target"
    # 症状指纹：检测器、异常事实（数值分档成 high/low）、对象类型、变更路径（05 §3.1）。例：
    # {"detectors": ["es.disk_watermark", "es.unassigned_shards"],
    #  "abnormal_facts": ["cluster.status:yellow", "cluster.unassigned_shards:high", "node.disk_used_pct:high"],
    #  "objects": ["cluster", "node"], "changes": []}
    symptom_fingerprint: dict[str, Any]
    # 由英文模板生成（05 §3.1），供 BM25 和向量检索。
    # 例："detectors: es.disk_watermark, es.unassigned_shards | abnormal: cluster.status yellow, ... | objects: ..."
    symptom_text: str
    root_cause_label: str  # 人确认的根因标签。例："es.disk_high_watermark"
    root_cause_text: str  # 例："es.disk_high_watermark: 数据节点磁盘使用率超过高水位，新分片或副本无法分配到该节点"
    action_taken: str | None  # 执行过的动作 ID；没执行为 None。例："es.raise_watermark_temporarily"
    outcome: Literal["resolved", "mitigated", "no_effect"]  # 例："resolved"
    # 产生或合并进这条记忆的调查。例：["inv_01M7489MXHXSTZHRPBCBC1DEP9"]
    source_investigations: list[str]
    occurrences: int = 1  # 合并过几次相同事故。例：1
    confused_with: list[str] = []  # 指纹相近但根因不同的记忆 ID（易混淆）。例：[]、["mem_01M74..."]
    confirmed_by: str  # 例："human:oncall"
    corrected: bool  # 人改过模型给的根因吗。例：False
    target_version: str  # 目标系统版本；取不到时写 "unknown"。例："9.5.3"、"unknown"
    created_at: datetime  # 例：2026-11-11T04:31:31.522296Z
    expires_at: datetime | None  # 例：None（不过期）
    invalidated: bool = False  # 被反馈判定失效后为 True，检索时跳过（软删除）。例：False
    hit_count: int = 0  # 被检索并展示给模型的次数。例：0
    helpful_count: int = 0  # 展示后最终根因和它一致的次数。例：0
