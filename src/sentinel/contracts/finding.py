"""问题事件 Finding（03 §3.2）：检测器的确定性输出，不经过 LLM。

例子取自本机 Postgres investigations.findings（调查 inv_01M74E2QD8GN52NJHBDXX1JMYR，磁盘水位场景）。
"""

from datetime import datetime
from typing import Literal

from sentinel.contracts._base import Contract


# 一个检测器在一次巡检里发现的一个问题；一次巡检可能产生多条，作为调查的 L0 输入。
class Finding(Contract):
    schema_version: int = 1  # 契约版本，升级时旧数据靠它识别。例：1
    id: str  # fnd_ + ULID，全局唯一。例："fnd_01M74E2R2YJ8SGBJC5KSZ3YB4D"
    # hash(集群, 检测器, 对象, 严重程度)，同一个问题连续巡检时用它去重（finding.deduplicated）。
    # 例："fcf00ea6be721495bdbaae2ba097ec6d"
    fingerprint: str
    pack: str  # 产生它的专家包。例："es"、"mysql"
    detector: str  # 检测器 ID，和 pack.yaml 的 detectors[].id 一致。例："es.disk_watermark"
    target: str  # 集群标识，例如 "es-target"
    object_ref: str | None  # 出问题的具体对象；集群级问题为 None。例："es-target-es-data-2"
    severity: Literal["info", "warn", "critical"]  # 例："warn"（磁盘水位）、"critical"（MySQL 只读）
    summary: str  # 给人和模型看的一句话。例："es-target-es-data-2 磁盘使用率 93%，超过高水位 90%"
    # 检测时的关键数值，只放数字。例：{"disk_used_pct": 93.0, "watermark_high_pct": 90.0, "flood_stage_pct": 95.0}
    metrics: dict[str, float]
    observed_at: datetime  # 检测时间（UTC）。例：2026-11-11T04:32:55.902383Z
    # 判断依据的观测记录 ID（证据链）。例：["obs_01M74E2QRGGX05JT3SM61SVGKR", "obs_01M74E2QWD63X6PZ83CG5EE5Z8"]
    observation_ids: list[str]
