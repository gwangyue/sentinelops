"""评测用例 EvalExample 和单次运行评分 RunScore（03 §6.2、§6.3）。

EvalExample 的例子取自 datasets/es.jsonl 的 es.disk_watermark.ext_flood_stage#r5；
RunScore 的例子取自 var/eval/v38__remediate__ds-20261111-2__heldout.jsonl 等本机评测结果。
"""

from datetime import datetime
from typing import Any, Literal

from sentinel.contracts._base import Contract


# 评测时喂给 Agent 的输入：触发方式 + 回放夹具。
class ExampleInputs(Contract):
    trigger: dict[str, Any]  # 例：{"kind": "patrol", "target": "es-target"}
    fixture_ref: str  # 录制好的工具输出，回放用。例："fixtures/es.disk_watermark.ext_flood_stage/r5.jsonl"
    memory_snapshot: str | None  # 预置的记忆快照；本机数据集全部为 None（记忆实验走事故流，见 eval/stream.py）


class ExampleOutputs(Contract):
    """真实答案，来自场景定义。"""

    root_cause_label: str  # 例："es.disk_high_watermark"
    acceptable_actions: list[str]  # 例：["es.relocate_shards", "es.raise_watermark_temporarily"]
    forbidden_actions: list[str]  # 例：["es.delete_index"]
    must_cite_tools: list[str]  # 例：["es.cat_allocation"]
    key_facts: list[str]  # 例：["node.disk_used_pct"]
    expect_escalation: bool = False  # 例：False（本机 40 条全部为 False）


# 用例的来历：哪个包、多难、录制时的目标版本和时间。
class ExampleMetadata(Contract):
    pack: str  # 例："es"
    difficulty: Literal["single", "distractor", "compound"]  # 例："single"
    target_version: str  # 录制时目标系统的版本。例："9.5.3"
    recorded_at: datetime  # 例：2026-11-10T13:42:44.941093Z
    stream_id: str | None  # 所属事故流；本机数据集全部为 None（事故流在 datasets/streams/ 里单独编排）
    stream_pos: int | None  # 在事故流中的位置；同上，例：None
    # online_correction = 线上调查被人纠正后回流的用例。
    # 例："external_adapted"（本机 34 条 bench、6 条 external_adapted）
    origin: Literal["bench", "external_adapted", "online_correction"]
    # external_adapted 时必填（数据集同步时检查，D4）。
    # 例："Elastic 官方文档 Watermark errors：https://www.elastic.co/docs/...（2026-11-10 读取）"
    source_ref: str | None = None


# 数据集里的一条评测用例：一个场景的一次录制。
class EvalExample(Contract):
    schema_version: int = 1  # 例：1
    id: str  # "<scenario_id>#r3"。例："es.disk_watermark.ext_flood_stage#r5"
    scenario_id: str  # 例："es.disk_watermark.ext_flood_stage"
    split: Literal["dev", "heldout"]  # 按 scenario_id 哈希分配。例："heldout"
    inputs: ExampleInputs
    outputs: ExampleOutputs
    metadata: ExampleMetadata


# 某个 Agent 版本在某条用例上跑一次的评分。所有指标都由它聚合。
class RunScore(Contract):
    schema_version: int = 1  # 例：1
    example_id: str  # 例："mysql.read_only.single#r1"
    rep: int  # 同一用例的第几次重复（从 0 开始）。例：0
    agent_version: str  # 例："sha256:341924fe1df83755bd72bb9113d1540af5f1318711d6e74a6fa5081ad3897079"
    dataset_version: str  # 数据集版本标签。例："ds-20261111-2"
    # 结果类
    top1_correct: bool  # 第一个假设的标签等于真实答案。例：True
    top3_correct: bool  # 例：True
    escalated: bool  # 例：False
    escalation_appropriate: bool | None  # 升级与否和 expect_escalation 一致吗。例：True
    context_recall: float  # key_facts 有多少进了上下文（04 §4）。例：1.0
    # 事故流里再次遇到同一根因时，相关记忆是否进了 L5；非事故流运行或首次遇到为 None（06 §3.1）。例：None、True
    memory_hit: bool | None
    # 过程类
    evidence_support_rate: float  # 论断的证据和 quote 能被观测核实的比例。例：1.0
    cited_required_tools: bool  # 例：True
    unsafe_proposals: int  # plan 节点拒掉的不安全提议 + 提出 forbidden_actions 的次数。例：0
    remediation_completed: bool | None  # 修复后问题是否解决；没走修复或没录到为 None。例：None、True
    # 成本类
    steps: int  # 例：1
    tokens: int  # 例：8311
    cost_usd: float  # 例：0.007025
    replay_misses: int  # 回放时夹具里没找到的工具调用数。例：0
    # 失败归类：规则标签 + 评审标签。
    # 例：[]；本机常见 "retrieval_miss"、"missed_required_tool"、"evidence_unsupported"
    failure_tags: list[str]
