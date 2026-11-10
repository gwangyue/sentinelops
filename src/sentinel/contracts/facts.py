"""事实卡片 FactSheet（03 §3.4；04 篇 L1）：确定性计算的结构化事实。"""

from datetime import datetime
from typing import Literal

from sentinel.contracts._base import Contract


class Fact(Contract):
    key: str  # "node.disk_used_pct"
    subject: str  # "node-2"
    value: float | str
    baseline: float | str | None
    status: Literal["normal", "abnormal", "unknown"]
    observation_id: str


class FactSheet(Contract):
    schema_version: int = 1
    target: str
    generated_at: datetime
    facts: list[Fact]  # 只放异常项和关键上下文
    omitted_normal_count: int  # 让模型知道"其他都正常"
