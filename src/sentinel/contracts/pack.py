"""专家包清单 pack.yaml（03 §3.1）。

负责：清单的结构和 03 写明的注册约束（工具必须声明 output_budget_tokens）。
不负责：按 entry 加载函数、检查函数签名（sdk 和注册表的事，D2）。

例子取自 packs/es/pack.yaml。
"""

from pydantic import Field

from sentinel.contracts._base import Contract


# 怎么连目标系统。只写凭证名，凭证本身在运行时从环境变量解析。
class ConnectionDecl(Contract):
    kind: str  # "elasticsearch"、"mysql"
    # 凭证名，凭证本身不写在清单里。例："es-readonly"（解析为 SENTINEL_CRED_ES_READONLY_{URL,USER,PASSWORD}）
    read_credential: str
    write_credential: str | None = None  # 只有执行器能取到。例："es-executor"


# 一个检测器：巡检时调用 entry，产出 Finding。
class DetectorDecl(Contract):
    id: str  # "es.unassigned_shards"
    entry: str  # "sentinel.packs.es.detectors.unassigned:detect"
    interval: str | None = None  # "15m"
    severity_rules: dict[str, str] | None = None  # 例：None（es 包的检测器都没配）


# 一个只读工具：诊断时按需调用，结果存成 Observation。
class ToolDecl(Contract):
    id: str  # 例："es.allocation_explain"
    entry: str  # 例："sentinel.packs.es.tools.allocation:allocation_explain"
    mcp_export: bool = False  # 是否通过 MCP 只读服务对外暴露。例：True；es.hot_threads 为 False（可能含敏感内容）
    # 必填：没有输出预算的工具不允许注册，强制"工具输出不能直接塞进上下文"。例：800
    output_budget_tokens: int = Field(gt=0)


# 把观测算成事实卡片的函数。
class FactBuilderDecl(Contract):
    entry: str  # 例："sentinel.packs.es.facts:cluster_facts"


# runbook 目录，作为上下文的 L4 层。
class KnowledgeDecl(Contract):
    path: str  # 相对专家包目录。例："knowledge/"


class RootCauseDecl(Contract):
    """专家包认识的一种根因（ADR-011）。诊断的 root_cause_label 只能从这里选，才能和场景的真实答案比对。"""

    label: str  # "es.disk_high_watermark"
    description: str  # 给模型看的一句话说明。例："数据节点磁盘使用率超过高水位，新分片或副本无法分配到该节点"


# 一个领域（专家包）的完整清单。新增一个领域 = 新增一份清单 + 它引用的代码。
class PackManifest(Contract):
    schema_version: int = 1  # 例：1
    pack: str  # 领域名，全局唯一。例："es"、"mysql"
    version: str  # 专家包的语义化版本，进入 AgentVersion。例："0.1.0"
    supports: str  # 支持的目标系统版本范围，例如 ">=9.0,<10"
    connection: ConnectionDecl  # 例：见 ConnectionDecl
    detectors: list[DetectorDecl] = []  # 例：es 包 4 个
    tools: list[ToolDecl] = []  # 例：es 包 13 个
    actions: list[str] = []  # 动作 ID，规格见 action.py。例：["es.enable_allocation", "es.relocate_shards", ...]
    fact_builders: list[FactBuilderDecl] = []  # 例：1 个
    knowledge: list[KnowledgeDecl] = []  # 例：[{"path": "knowledge/"}]
    scenarios: str = "scenarios/"  # 场景 YAML 所在目录
    root_causes: list[RootCauseDecl] = []  # 可选，默认为空：旧的清单照样能读（ADR-011）。例：es 包 6 个，含 es.unknown
