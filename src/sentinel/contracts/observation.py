"""观测记录 Observation（03 §3.3）：证据链的根，每次工具调用一条。

例子取自本机 Postgres observations（一次 MCP 只读调用 es.cluster_health）。
"""

from datetime import datetime
from typing import Any, Literal

from sentinel.contracts._base import Contract


# 一次工具调用的结果。原始输出落盘，只有不超过预算的摘要进入上下文；诊断里的论断都引用它的 id。
class Observation(Contract):
    schema_version: int = 1  # 例：1
    id: str  # obs_ + ULID。例："obs_01M74M28F09HJT0FR7Z0778GDA"
    # 所属调查；巡检、MCP 调用也各有一个调查 ID。
    # 例："inv_01M74E2QD8GN52NJHBDXX1JMYR"、"inv_mcp_01M74M280EGTD3T3BF410WEW0X"
    investigation_id: str
    tool: str  # 工具 ID，和 pack.yaml 的 tools[].id 一致。例："es.cluster_health"
    args: dict[str, Any]  # 规范化后的参数，也是回放的查找键。例：{}、{"index": "bench-new"}
    # live = 直连目标系统；record = 直连并录制成夹具；replay = 从夹具回放（评测用）。例："live"
    backend: Literal["live", "record", "replay"]
    status: Literal["ok", "error", "replay_miss"]  # replay_miss = 回放时夹具里找不到这组参数。例："ok"
    # 原始输出的位置（文件路径或 S3 key）。
    # 例："var/observations/inv_mcp_01M74M280EGTD3T3BF410WEW0X/obs_01M74M28F09HJT0FR7Z0778GDA.json"
    raw_ref: str
    raw_sha256: str  # 原始输出的哈希，防篡改。例："4214291693c3e756...3f6509"（64 位十六进制）
    # 进入上下文的摘要，不超过工具的 output_budget_tokens。
    # 例："status=green number_of_nodes=3 number_of_data_nodes=3 active_primary_shards=7 ... unassigned_shards=0"
    digest: str
    untrusted: bool = True  # 可能含用户写入的内容，一律视为不可信。例：True
    created_at: datetime  # 例：2026-11-11T06:17:31.362641Z
