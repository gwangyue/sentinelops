"""诊断 Diagnosis（03 §4.1）。

负责：结构约束——每个论断至少引用 1 条观测记录，最多 3 个假设。
不负责：证据是否真实存在、quote 是否是摘要的子串。那要查观测存储，属于评分器（06 篇），不放在契约里。
"""

from pydantic import Field

from sentinel.contracts._base import Contract


class Claim(Contract):
    id: str  # "C1"
    text: str
    evidence: list[str] = Field(min_length=1)  # 观测记录 ID，至少 1 个
    quote: str | None = None  # 从观测摘要里逐字摘出的片段


class Hypothesis(Contract):
    id: str  # "H1"
    root_cause: str
    root_cause_label: str | None  # 归一化标签，和真实答案比对
    confidence: float = Field(ge=0, le=1)  # 模型自报，只用于排序
    claims: list[Claim]
    from_memory: list[str] = []  # 参考了哪些记忆，用于消融分析


class Diagnosis(Contract):
    schema_version: int = 1
    investigation_id: str
    hypotheses: list[Hypothesis] = Field(max_length=3)  # 已按 confidence 排序
    escalate: bool
    escalate_reason: str | None
    steps: int
    tokens_in: int
    tokens_out: int
