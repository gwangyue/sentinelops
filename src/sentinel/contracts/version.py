"""Agent 版本 AgentVersion（03 §5.2）：所有指标都挂在它上面。

负责：结构和 note 必填。
不负责：哈希计算和冻结（core/versioning，D3）。
"""

from datetime import datetime
from typing import Any

from pydantic import Field

from sentinel.contracts._base import Contract


class VersionComponents(Contract):
    prompts: dict[str, str]  # 每段提示词的内容哈希
    # 用途 → {provider, model, temperature, thinking_budget, ...}；model 必须是固定版本（G2），
    # 内容来自 config/models.yaml，代码里不写型号（ADR-008）
    models: dict[str, dict[str, Any]]
    tools: dict[str, str]  # 工具 ID → 工具描述的哈希
    context_policy: dict[str, Any]
    memory_policy: dict[str, Any]
    packs: dict[str, str]  # 专家包 → 版本号
    code_sha: str


class AgentVersion(Contract):
    schema_version: int = 1
    seq: int  # 14 → "v14"
    hash: str  # components 的 sha256
    parent: str | None
    created_at: datetime
    note: str = Field(min_length=1)  # 必填：这一版改了什么
    components: VersionComponents
