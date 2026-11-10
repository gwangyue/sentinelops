"""专家包清单 pack.yaml（03 §3.1）。

负责：清单的结构和 03 写明的注册约束（工具必须声明 output_budget_tokens）。
不负责：按 entry 加载函数、检查函数签名（sdk 和注册表的事，D2）。
"""

from pydantic import Field

from sentinel.contracts._base import Contract


class ConnectionDecl(Contract):
    kind: str  # "elasticsearch"
    read_credential: str  # 凭证名，凭证本身不写在清单里
    write_credential: str | None = None  # 只有执行器能取到


class DetectorDecl(Contract):
    id: str  # "es.unassigned_shards"
    entry: str  # "sentinel.packs.es.detectors.unassigned:detect"
    interval: str | None = None  # "15m"
    severity_rules: dict[str, str] | None = None


class ToolDecl(Contract):
    id: str
    entry: str
    mcp_export: bool = False
    # 必填：没有输出预算的工具不允许注册，强制"工具输出不能直接塞进上下文"
    output_budget_tokens: int = Field(gt=0)


class FactBuilderDecl(Contract):
    entry: str


class KnowledgeDecl(Contract):
    path: str


class RootCauseDecl(Contract):
    """专家包认识的一种根因（ADR-011）。诊断的 root_cause_label 只能从这里选，才能和场景的真实答案比对。"""

    label: str  # "es.disk_high_watermark"
    description: str  # 给模型看的一句话说明


class PackManifest(Contract):
    schema_version: int = 1
    pack: str  # 领域名，全局唯一
    version: str  # 专家包的语义化版本，进入 AgentVersion
    supports: str  # 支持的目标系统版本范围，例如 ">=9.0,<10"
    connection: ConnectionDecl
    detectors: list[DetectorDecl] = []
    tools: list[ToolDecl] = []
    actions: list[str] = []  # 动作 ID，规格见 action.py
    fact_builders: list[FactBuilderDecl] = []
    knowledge: list[KnowledgeDecl] = []
    scenarios: str = "scenarios/"
    root_causes: list[RootCauseDecl] = []  # 可选，默认为空：旧的清单照样能读（ADR-011）
