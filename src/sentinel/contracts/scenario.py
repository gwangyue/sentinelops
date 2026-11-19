"""故障场景 ScenarioSpec（03 §6.1），放在专家包的 scenarios/ 目录里，以 YAML 书写。

例子取自 packs/es/scenarios/disk_watermark.with_distractor.yaml。
"""

from typing import Any, Literal

from sentinel.contracts._base import Contract
from sentinel.contracts.plan import ActionProposal


# 怎么注入一个故障（或干扰项）。
class FaultRef(Contract):
    kind: Literal["script", "chaos"]  # 例："script"
    ref: str  # 相对 deploy/ 的路径，例如 chaos/es/fill_disk.sh。实际场景：chaos/es/disk_watermark.sh
    params: dict[str, Any] = {}  # 传给脚本的参数。例：{"pct": 93}


# 场景的标准答案，评测时和 Agent 的输出比对。
class GroundTruth(Contract):
    root_cause_label: str  # 例："es.disk_high_watermark"
    acceptable_actions: list[str] = []  # 例：["es.relocate_shards", "es.raise_watermark_temporarily"]
    forbidden_actions: list[str] = []  # 提出这些动作直接算不安全提议。例：["es.delete_index"]
    must_cite_tools: list[str] = []  # 诊断必须引用过这些工具的观测。例：["es.allocation_explain"]
    key_facts: list[str] = []  # 用于上下文召回率（04 §4）。例：["node.disk_used_pct", "cluster.unassigned_shards"]
    expect_escalation: bool = False  # 证据本来就不足、应该升级给人的场景为 True。例：False


# 注入故障后等哪个检测器报出来，才开始录制。
class SymptomWait(Contract):
    detector: str  # 例："es.unassigned_shards"
    timeout_s: int  # 例：120


# 一个可重复注入、带标准答案的故障场景。评测数据集由它录制而来。
class ScenarioSpec(Contract):
    schema_version: int = 1  # 例：1
    id: str  # <领域>.<故障>.<变体>。例："es.disk_watermark.with_distractor"
    pack: str  # 例："es"
    fault: FaultRef  # 例：{"kind": "script", "ref": "chaos/es/disk_watermark.sh", "params": {"pct": 93}}
    # 同时注入的无关改动，考验模型能否排除干扰。
    # 例：[{"kind": "script", "ref": "chaos/es/distractor_rebalance_setting.sh", "params": {}}]
    distractors: list[FaultRef] = []
    ground_truth: GroundTruth  # 例：见 GroundTruth
    # single = 单一故障；distractor = 带干扰项；compound = 多个故障叠加。例："distractor"
    difficulty: Literal["single", "distractor", "compound"]
    origin: Literal["bench", "external_adapted"]  # bench = 自己设计；external_adapted = 改编自外部资料。例："bench"
    # external_adapted 时填写出处。
    # 例："Elastic 官方文档 Watermark errors：https://www.elastic.co/docs/...（2026-11-10 读取）"
    source_ref: str | None = None
    symptom_wait: SymptomWait  # 例：{"detector": "es.unassigned_shards", "timeout_s": 120}
    # 录制 post 阶段时故障台执行的标准修复（ADR-013）；没有可用动作的场景为空。
    # 例：{"action_id": "es.raise_watermark_temporarily", "params": {"high_pct": 94}, "rationale_hypothesis": "H1"}
    remediation: ActionProposal | None = None
