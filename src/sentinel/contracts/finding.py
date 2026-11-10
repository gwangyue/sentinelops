"""问题事件 Finding（03 §3.2）：检测器的确定性输出，不经过 LLM。"""

from datetime import datetime
from typing import Literal

from sentinel.contracts._base import Contract


class Finding(Contract):
    schema_version: int = 1
    id: str  # fnd_...
    fingerprint: str  # hash(集群, 检测器, 对象, 严重程度)，巡检去重用
    pack: str
    detector: str
    target: str  # 集群标识，例如 "es-target"
    object_ref: str | None
    severity: Literal["info", "warn", "critical"]
    summary: str
    metrics: dict[str, float]
    observed_at: datetime
    observation_ids: list[str]
