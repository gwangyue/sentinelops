"""上下文包 ContextBundle（03 §4.5 登记，定义在 04 篇 §3.9）。

被删掉的条目也记录下来（included=False + drop_reason），这就是"上下文报告"。
"""

from typing import Literal

from sentinel.contracts._base import Contract


class ContextItem(Contract):
    layer: Literal["L0", "L1", "L3", "L4", "L5"]  # L2 是诊断中按需拉取的，不在预组装的包里
    ref: str  # fnd_、obs_、mem_ 或知识文件路径
    tokens: int
    included: bool
    drop_reason: str | None  # "budget"、"below_threshold"、"duplicate" 等


class ContextBundle(Contract):
    schema_version: int = 1
    investigation_id: str
    policy_hash: str  # 组装策略配置的哈希，也计入 AgentVersion
    items: list[ContextItem]
    tokens_by_layer: dict[str, int]
    rendered: str  # 最终交给模型的文本
