"""契约测试（08 §5）：往返序列化、03 篇的 YAML 示例原样可解析、v1 旧样例仍能读取。"""

import json
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sentinel.contracts import Claim, Diagnosis, Hypothesis, PackManifest, ScenarioSpec, ToolDecl
from tests.contracts.factories import all_samples

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs/design/03-contracts.md"
SAMPLES = Path(__file__).parent / "samples/v1"

SAMPLE_MODELS = {name: type(obj) for name, obj in all_samples().items()}


def test_covers_all_15_contracts() -> None:
    # 03 §本篇结论：15 个契约；审批与执行算一个契约但有 3 个模型，所以是 17 个顶层模型；ADR-013 新增 ActionProposal
    assert len(SAMPLE_MODELS) == 18


@pytest.mark.parametrize("name", sorted(SAMPLE_MODELS))
def test_roundtrip(name: str) -> None:
    obj = all_samples()[name]
    data = json.loads(obj.model_dump_json())
    assert type(obj).model_validate(data) == obj


@pytest.mark.parametrize("name", sorted(SAMPLE_MODELS))
def test_v1_samples_still_load(name: str) -> None:
    # 基线数据一旦提交就不再改；契约升级后这里必须仍然通过（03 §7"兼容旧数据"）
    data = json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    SAMPLE_MODELS[name].model_validate(data)


def _yaml_blocks() -> list[dict]:  # type: ignore[type-arg]
    return [yaml.safe_load(b) for b in re.findall(r"```yaml\n(.*?)```", DOC.read_text(encoding="utf-8"), re.S)]


def test_doc_pack_example_parses() -> None:
    m = PackManifest.model_validate(_yaml_blocks()[0])
    assert m.connection.write_credential == "es-executor"
    assert all(t.output_budget_tokens > 0 for t in m.tools)


def test_tool_without_output_budget_rejected() -> None:
    # 03 §3.1 约束：没有 output_budget_tokens 的工具不允许注册
    with pytest.raises(ValidationError, match="output_budget_tokens"):
        ToolDecl.model_validate({"id": "t", "entry": "m:f"})


def test_doc_scenario_examples_parse() -> None:
    blocks = _yaml_blocks()
    full = ScenarioSpec.model_validate(blocks[1])
    assert full.symptom_wait.timeout_s == 120
    assert full.distractors[0].ref == "chaos/es/net_delay.yaml"
    # 第二个示例写着"其余字段同上"：用第一个示例补齐缺的字段
    short = blocks[2]
    merged = {**blocks[1], **short, "ground_truth": {**blocks[1]["ground_truth"], **short["ground_truth"]}}
    merged["distractors"] = []
    assert ScenarioSpec.model_validate(merged).ground_truth.root_cause_label == "es.node_unavailable"


def test_claim_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        Claim(id="C1", text="x", evidence=[])


def test_at_most_three_hypotheses() -> None:
    h = Hypothesis(
        id="H",
        root_cause="x",
        root_cause_label=None,
        confidence=0.1,
        claims=[Claim(id="C", text="x", evidence=["obs_1"])],
    )
    with pytest.raises(ValidationError):
        Diagnosis(
            investigation_id="i",
            hypotheses=[h] * 4,
            escalate=False,
            escalate_reason=None,
            steps=0,
            tokens_in=0,
            tokens_out=0,
        )


def test_agent_version_note_required() -> None:
    sample = all_samples()["agent_version"]
    with pytest.raises(ValidationError):
        type(sample).model_validate({**sample.model_dump(), "note": ""})


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolDecl.model_validate({"id": "t", "entry": "m:f", "output_budget_tokens": 1, "typo": 1})
