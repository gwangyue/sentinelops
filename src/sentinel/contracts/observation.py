"""观测记录 Observation（03 §3.3）：证据链的根，每次工具调用一条。"""

from datetime import datetime
from typing import Any, Literal

from sentinel.contracts._base import Contract


class Observation(Contract):
    schema_version: int = 1
    id: str  # obs_...
    investigation_id: str
    tool: str
    args: dict[str, Any]  # 规范化后的参数，也是回放的查找键
    backend: Literal["live", "record", "replay"]
    status: Literal["ok", "error", "replay_miss"]
    raw_ref: str  # 原始输出的位置（文件路径或 S3 key）
    raw_sha256: str
    digest: str  # 进入上下文的摘要，不超过工具的 output_budget_tokens
    untrusted: bool = True  # 可能含用户写入的内容，一律视为不可信
    created_at: datetime
