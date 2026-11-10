"""故障场景 ScenarioSpec（03 §6.1），放在专家包的 scenarios/ 目录里，以 YAML 书写。"""

from typing import Any, Literal

from sentinel.contracts._base import Contract


class FaultRef(Contract):
    kind: Literal["script", "chaos"]
    ref: str  # 相对 deploy/ 的路径，例如 chaos/es/fill_disk.sh
    params: dict[str, Any] = {}


class GroundTruth(Contract):
    root_cause_label: str
    acceptable_actions: list[str] = []
    forbidden_actions: list[str] = []  # 提出这些动作直接算不安全提议
    must_cite_tools: list[str] = []
    key_facts: list[str] = []  # 用于上下文召回率（04 §4）
    expect_escalation: bool = False


class SymptomWait(Contract):
    detector: str
    timeout_s: int


class ScenarioSpec(Contract):
    schema_version: int = 1
    id: str  # <领域>.<故障>.<变体>
    pack: str
    fault: FaultRef
    distractors: list[FaultRef] = []
    ground_truth: GroundTruth
    difficulty: Literal["single", "distractor", "compound"]
    origin: Literal["bench", "external_adapted"]
    source_ref: str | None = None  # external_adapted 时填写出处
    symptom_wait: SymptomWait
