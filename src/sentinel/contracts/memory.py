"""事故记忆 MemoryRecord（03 §6.4）：只在人确认根因之后写入。embedding 只存在 es-memory 的索引里。"""

from datetime import datetime
from typing import Any, Literal

from sentinel.contracts._base import Contract


class MemoryRecord(Contract):
    schema_version: int = 1
    id: str  # mem_...
    namespace: str  # "es/es-target"
    symptom_fingerprint: dict[str, Any]  # {detectors: [...], abnormal_facts: [...]}
    symptom_text: str  # 由英文模板生成（05 §3.1）
    root_cause_label: str
    root_cause_text: str
    action_taken: str | None
    outcome: Literal["resolved", "mitigated", "no_effect"]
    source_investigations: list[str]
    occurrences: int = 1
    confused_with: list[str] = []
    confirmed_by: str
    corrected: bool
    target_version: str
    created_at: datetime
    expires_at: datetime | None
    invalidated: bool = False
    hit_count: int = 0
    helpful_count: int = 0
