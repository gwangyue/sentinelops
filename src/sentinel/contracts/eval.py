"""评测用例 EvalExample 和单次运行评分 RunScore（03 §6.2、§6.3）。"""

from datetime import datetime
from typing import Any, Literal

from sentinel.contracts._base import Contract


class ExampleInputs(Contract):
    trigger: dict[str, Any]
    fixture_ref: str
    memory_snapshot: str | None


class ExampleOutputs(Contract):
    """真实答案，来自场景定义。"""

    root_cause_label: str
    acceptable_actions: list[str]
    forbidden_actions: list[str]
    must_cite_tools: list[str]
    key_facts: list[str]
    expect_escalation: bool = False


class ExampleMetadata(Contract):
    pack: str
    difficulty: Literal["single", "distractor", "compound"]
    target_version: str
    recorded_at: datetime
    stream_id: str | None
    stream_pos: int | None
    origin: Literal["bench", "external_adapted", "online_correction"]
    source_ref: str | None = None  # external_adapted 时必填（数据集同步时检查，D4）


class EvalExample(Contract):
    schema_version: int = 1
    id: str  # "<scenario_id>#r3"
    scenario_id: str
    split: Literal["dev", "heldout"]  # 按 scenario_id 哈希分配
    inputs: ExampleInputs
    outputs: ExampleOutputs
    metadata: ExampleMetadata


class RunScore(Contract):
    schema_version: int = 1
    example_id: str
    rep: int
    agent_version: str
    dataset_version: str
    # 结果类
    top1_correct: bool
    top3_correct: bool
    escalated: bool
    escalation_appropriate: bool | None
    context_recall: float
    memory_hit: bool | None
    # 过程类
    evidence_support_rate: float
    cited_required_tools: bool
    unsafe_proposals: int
    remediation_completed: bool | None
    # 成本类
    steps: int
    tokens: int
    cost_usd: float
    replay_misses: int
    # 失败归类
    failure_tags: list[str]
