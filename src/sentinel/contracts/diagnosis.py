"""诊断 Diagnosis（03 §4.1）。

负责：结构约束——每个论断至少引用 1 条观测记录，最多 3 个假设。
不负责：证据是否真实存在、quote 是否是摘要的子串。那要查观测存储，属于评分器（06 篇），不放在契约里。

例子取自本机 Postgres investigations.diagnosis（调查 inv_01M74E2QD8GN52NJHBDXX1JMYR，磁盘水位场景）。
id 类字段由模型生成，契约不限制格式：实际数据里见过 "hyp_1"、"H1"、"h1"，"clm_1"、"claim_1"。
"""

from pydantic import Field

from sentinel.contracts._base import Contract


# 一条可核对的论断：说了什么 + 依据哪条观测 + 原文摘录。
class Claim(Contract):
    id: str  # 模型生成，同一诊断内唯一。例："C1"、"clm_1"
    text: str  # 例："节点 es-target-es-data-2 磁盘使用率达到 93%，超过低水位设置，无法分配新的副本分片。"
    evidence: list[str] = Field(min_length=1)  # 观测记录 ID，至少 1 个。例：["obs_01M74E2WKK46VRAZ18ENDH8YHJ"]
    # 从观测摘要里逐字摘出的片段，评分器据此核对证据。
    # 例："es-target-es-data-2: no [disk_threshold] the node is above the low watermark cluster setting ..."
    quote: str | None = None


# 一个根因假设及支撑它的论断。
class Hypothesis(Contract):
    id: str  # 模型生成。例："H1"、"hyp_1"
    # 自由文本的根因描述。
    # 例："数据节点 es-target-es-data-2 磁盘使用率达 93%，超过磁盘分配水位线，导致 ... 集群状态变为 yellow。"
    root_cause: str
    # 归一化标签，和真实答案比对；只能取专家包 root_causes 里的值（ADR-011）。例："es.disk_high_watermark"
    root_cause_label: str | None
    confidence: float = Field(ge=0, le=1)  # 模型自报，只用于排序。例：0.95
    claims: list[Claim]  # 例：3 条，见 Claim
    from_memory: list[str] = []  # 参考了哪些记忆，用于消融分析。例：["mem_01M74E05SVWD7FTG27A6NMEPN9"]


# 诊断节点的输出：最多 3 个排好序的假设，或者升级给人。
class Diagnosis(Contract):
    schema_version: int = 1  # 例：1
    investigation_id: str  # 例："inv_01M74E2QD8GN52NJHBDXX1JMYR"
    hypotheses: list[Hypothesis] = Field(max_length=3)  # 已按 confidence 排序。例：1 个，标签 es.disk_high_watermark
    escalate: bool  # True = 模型认为证据不足，交给人。例：False
    escalate_reason: str | None  # escalate 为 False 时为 None。例：None
    steps: int  # 诊断循环用了几步（含 L2 按需取数）。例：2
    tokens_in: int  # 例：10717
    tokens_out: int  # 例：555
