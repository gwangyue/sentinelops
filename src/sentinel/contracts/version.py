"""Agent 版本 AgentVersion（03 §5.2）：所有指标都挂在它上面。

负责：结构和 note 必填。
不负责：哈希计算和冻结（core/versioning，D3）。

例子取自本机 Postgres agent_versions 表 seq=41（v41）。
"""

from datetime import datetime
from typing import Any

from pydantic import Field

from sentinel.contracts._base import Contract


# 决定 Agent 行为的全部组成部分。任何一项变了，哈希就变，就是一个新版本。
class VersionComponents(Contract):
    # 每段提示词的内容哈希。例：{"diagnose.md": "51b1e2e2...b751b7f", "plan.md": "637a00f9...bd8a812"}
    prompts: dict[str, str]
    # 用途 → {provider, model, temperature, thinking_budget, ...}；model 必须是固定版本（G2），
    # 内容来自 config/models.yaml，代码里不写型号（ADR-008）。例：
    # {"diagnosis": {"provider": "google", "model": "gemini-3.7-flash", "temperature": 1.0, ...},
    #  "embedding": {"provider": "google", "model": "gemini-embedding-001", "output_dimensionality": 768}}
    models: dict[str, dict[str, Any]]
    tools: dict[str, str]  # 工具 ID → 工具描述的哈希。例：{"es.cat_shards": "de4d917c...6d8", ...}
    context_policy: dict[str, Any]  # config/context_policy.yaml 的内容。例：{"L1": {"format": "facts"}, ...}
    memory_policy: dict[str, Any]  # config/memory_policy.yaml 的内容。例：{"merge": {"jaccard": 0.8}, ...}
    packs: dict[str, str]  # 专家包 → 版本号。例：{"es": "0.1.0", "mysql": "0.1.0"}
    code_sha: str  # 例："sha256:4586ff9d3408a4eed8288c2314b64d7e2a1284bb197c2404127c01598687d7a4"


# 一个冻结的 Agent 版本。评测结果和审计事件都用它的 hash 标识。
class AgentVersion(Contract):
    schema_version: int = 1  # 例：1
    seq: int  # 14 → "v14"。例：41
    hash: str  # components 的 sha256。例："sha256:fbac6a69be98888c2565ab7a07005ffd3cb451b360c7c8e15a87b495788bd73b"
    # 上一版的 hash；第一版为 None。例："sha256:afd6049aa8030cd6adf834584bb8c5629cbca47519570e5dc4e128d2151b8f5f"
    parent: str | None
    created_at: datetime  # 例：2026-11-11T13:42:15.420212Z
    # 必填：这一版改了什么。例："v41：同 v40；执行器拒绝执行时也写执行结果（M3 独立审阅发现 1：...）；CLI 修复（...）"
    note: str = Field(min_length=1)
    components: VersionComponents
