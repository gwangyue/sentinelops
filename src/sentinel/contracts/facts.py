"""事实卡片 FactSheet（03 §3.4；04 篇 L1）：确定性计算的结构化事实。

例子取自本机 Postgres investigations.fact_sheets（调查 inv_01M74E2QD8GN52NJHBDXX1JMYR，磁盘水位场景）。
"""

from datetime import datetime
from typing import Literal

from sentinel.contracts._base import Contract


# 一条事实：某个对象的某个指标是多少、正常与否、出自哪条观测。
class Fact(Contract):
    key: str  # 指标名，<对象类型>.<指标>。例："node.disk_used_pct"、"cluster.status"
    subject: str  # 指标属于哪个对象。例："es-target-es-data-2"、"cluster"
    value: float | str  # 例：93.0、"yellow"
    baseline: float | str | None  # 正常基线；第一版大多没有，为 None。例：None
    # abnormal = 超出阈值；unknown = 没有阈值可判断（只作背景）。例："abnormal"
    status: Literal["normal", "abnormal", "unknown"]
    observation_id: str  # 这个数从哪条观测算出来。例："obs_01M74E2QRGGX05JT3SM61SVGKR"


# 一个集群在一次调查里的事实卡片，渲染成上下文的 L1 层。
class FactSheet(Contract):
    schema_version: int = 1  # 例：1
    target: str  # 例："es-target"
    generated_at: datetime  # 例：2026-11-11T04:32:56.234040Z
    # 只放异常项和关键上下文。例：上面的 Fact 共 8 条（cluster.status、node.disk_used_pct、node.heap_used_pct ...）
    facts: list[Fact]
    omitted_normal_count: int  # 让模型知道"其他都正常"。例：2
