"""评测：评分器、bootstrap、基线、数据集同步。不访问网络、不调用 LLM。"""

import statistics
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sentinel.contracts import EvalExample, ExampleOutputs, RunScore
from sentinel.eval import dataset as ds
from sentinel.eval.baselines import majority, rules_label
from sentinel.eval.scorers import cost_usd, load_pricing, score
from sentinel.eval.stats.bootstrap import compare, stability
from tests.flows.scripts import diagnose_script

TRUTH = ExampleOutputs(
    root_cause_label="es.disk_high_watermark",
    acceptable_actions=[],
    forbidden_actions=[],
    must_cite_tools=["es.allocation_explain"],
    key_facts=["node.disk_used_pct"],
)


def rs(eid: str, top1: bool, rep: int = 0, misses: int = 0) -> RunScore:
    return RunScore(
        example_id=eid,
        rep=rep,
        agent_version="v",
        dataset_version="t",
        top1_correct=top1,
        top3_correct=top1,
        escalated=False,
        escalation_appropriate=True,
        context_recall=1.0,
        memory_hit=None,
        evidence_support_rate=1.0,
        cited_required_tools=True,
        unsafe_proposals=0,
        remediation_completed=None,
        steps=1,
        tokens=1,
        cost_usd=0.0,
        replay_misses=misses,
        failure_tags=[],
    )


# ---------- bootstrap ----------


def test_identical_versions_interval_contains_zero() -> None:
    a = [rs(f"s{i}#r1", i % 2 == 0) for i in range(6)]
    c = compare(a, a)
    assert c.point == 0 and c.low <= 0 <= c.high and c.verdict == "无法判断"


def test_uniform_improvement_is_detected() -> None:
    a = [rs(f"s{i}#r{k}", False) for i in range(8) for k in (1, 2)]
    b = [rs(f"s{i}#r{k}", True) for i in range(8) for k in (1, 2)]
    c = compare(a, b)
    assert c.point == 1.0 and c.low > 0 and c.verdict == "提升" and c.n_scenarios == 8


def test_bootstrap_is_reproducible_and_clustered_by_scenario() -> None:
    # 场景 s0 有 2 条录制都提升，s1 有 1 条没变：按场景平均后 d_s = [1, 0]，不是按用例的 [1, 1, 0]
    a = [rs("s0#r1", False), rs("s0#r2", False), rs("s1#r1", True)]
    b = [rs("s0#r1", True), rs("s0#r2", True), rs("s1#r1", True)]
    c1, c2 = compare(a, b), compare(a, b)
    assert c1 == c2
    assert c1.n_scenarios == 2 and c1.low == 0.0 and c1.high == 1.0
    assert c1.point == pytest.approx(statistics.fmean([1, 1, 0]))  # 点估计按用例（06 §3.2）


def test_replay_miss_examples_are_excluded() -> None:
    a = [rs("s0#r1", True), rs("s1#r1", True)]
    b = [rs("s0#r1", False, misses=2), rs("s1#r1", True)]
    c = compare(a, b)
    assert c.excluded_replay_miss == ["s0#r1"] and c.n_examples == 1


def test_stability() -> None:
    st = stability([rs("a#r1", True, 0), rs("a#r1", False, 1), rs("b#r1", True, 0), rs("b#r1", True, 1)])
    assert st.unstable_rate == 0.5 and st.mean_within_var == pytest.approx(0.125)


# ---------- 评分器 ----------


def _out_from_flow(harness: Any, script: Any) -> dict[str, Any]:
    h = harness("disk_watermark", script)
    s = h.run()
    return {
        "status": s["status"],
        "diagnosis": s["diagnosis"],
        "bundle": s["bundle"],
        "observations": [o.model_dump(mode="json") for o in h.obs.list(s["inv_id"])],
    }


def _score(out: dict[str, Any], truth: ExampleOutputs = TRUTH) -> RunScore:
    return score(
        out=out,
        truth=truth,
        example_id="es.disk_watermark.single#r1",
        rep=0,
        agent_version="v",
        dataset_tag="t",
        model="gemini-3.7-flash",
        pricing=load_pricing(),
    )


def test_scorer_on_correct_diagnosis(harness: Any) -> None:
    s = _score(_out_from_flow(harness, diagnose_script()))
    assert s.top1_correct and s.top3_correct and not s.escalated and s.escalation_appropriate
    assert s.evidence_support_rate == 1.0 and s.cited_required_tools and s.context_recall == 1.0
    assert s.replay_misses == 0 and s.tokens == 2200 and s.cost_usd > 0


def test_scorer_wrong_label_and_uncited_tool(harness: Any) -> None:
    out = _out_from_flow(harness, diagnose_script(label="es.shard_limit_reached"))
    s = _score(out, TRUTH.model_copy(update={"must_cite_tools": ["es.cluster_settings"]}))
    assert not s.top1_correct and not s.cited_required_tools


def test_scorer_escalation_counts_as_wrong_unless_expected(harness: Any) -> None:
    out = _out_from_flow(harness, diagnose_script(confidence=0.1))
    assert out["status"] == "escalated"
    assert not _score(out).top1_correct
    expect = _score(out, TRUTH.model_copy(update={"expect_escalation": True}))
    assert expect.top1_correct and expect.escalation_appropriate


def test_scorer_context_recall_partial(harness: Any) -> None:
    out = _out_from_flow(harness, diagnose_script())
    s = _score(out, TRUTH.model_copy(update={"key_facts": ["node.disk_used_pct", "threadpool.search.rejected"]}))
    assert s.context_recall == 0.5


def test_cost_requires_known_price() -> None:
    assert cost_usd("gemini-3.7-flash", 1_000_000, 0, load_pricing()) == 0.75
    with pytest.raises(KeyError):
        cost_usd("unknown-model-1", 1, 1, load_pricing())


# ---------- 基线 ----------

RULES = {
    "rules": [
        {"when_all": ["es.node_count"], "label": "es.node_unavailable"},
        {"when_all": ["es.disk_watermark"], "label": "es.disk_high_watermark"},
        {"when_all": ["es.unassigned_shards"], "label": "es.allocation_disabled"},
    ],
    "default": "es.unknown",
}


def test_rules_order_matters() -> None:
    assert rules_label({"es.unassigned_shards", "es.node_count"}, RULES) == "es.node_unavailable"
    assert rules_label({"es.unassigned_shards"}, RULES) == "es.allocation_disabled"
    assert rules_label(set(), RULES) == "es.unknown"


def _ex(eid: str, label: str) -> EvalExample:
    return EvalExample.model_validate(
        {
            "id": eid,
            "scenario_id": eid.split("#")[0],
            "split": "dev",
            "inputs": {"trigger": {"target": "es-target"}, "fixture_ref": "x", "memory_snapshot": None},
            "outputs": {
                "root_cause_label": label,
                "acceptable_actions": [],
                "forbidden_actions": [],
                "must_cite_tools": [],
                "key_facts": [],
            },
            "metadata": {
                "pack": "es",
                "difficulty": "single",
                "target_version": "9.5.3",
                "recorded_at": datetime(2026, 11, 10, tzinfo=UTC),
                "stream_id": None,
                "stream_pos": None,
                "origin": "bench",
            },
        }
    )


def test_majority_baseline() -> None:
    exs = [_ex("a#r1", "x"), _ex("a#r2", "x"), _ex("b#r1", "y")]
    assert [s.top1_correct for s in majority(exs, "t")] == [True, True, False]


# ---------- 数据集 ----------


def test_split_is_deterministic_per_scenario() -> None:
    assert ds.split_for("es.disk_watermark.single") == "dev"
    assert ds.split_for("es.network_partition.single") == "heldout"
    # G1：外部改编的场景一律进 heldout，即使 hash 会把它分到 dev
    assert ds.split_for("es.disk_watermark.single", "external_adapted") == "heldout"
    assert ds.example_uuid("a#r1") == ds.example_uuid("a#r1") and uuid.UUID(ds.example_uuid("a#r1"))


class FakeLS:
    def __init__(self) -> None:
        self.examples: dict[str, dict[str, Any]] = {}
        self.tags: list[str] = []

    def has_dataset(self, *, dataset_name: str) -> bool:
        return True

    def read_dataset(self, *, dataset_name: str) -> Any:
        return SimpleNamespace(id="ds1")

    def create_dataset(self, dataset_name: str, *, description: str) -> Any:
        return SimpleNamespace(id="ds1")

    def list_examples(self, *, dataset_id: Any) -> Any:
        return [SimpleNamespace(id=k) for k in self.examples]

    def create_examples(self, *, dataset_id: Any, examples: list[dict[str, Any]]) -> None:
        for e in examples:
            assert e["id"] not in self.examples
            self.examples[e["id"]] = e

    def update_examples(self, *, dataset_id: Any, updates: list[dict[str, Any]]) -> None:
        for e in updates:
            self.examples[e["id"]] = e

    def list_dataset_versions(self, *, dataset_id: Any) -> Any:
        return [SimpleNamespace(as_of=datetime(2026, 11, 10, tzinfo=UTC))]

    def update_dataset_tag(self, *, dataset_id: Any, as_of: datetime, tag: str) -> None:
        self.tags.append(tag)


def test_sync_is_idempotent_and_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "DATASETS", tmp_path)
    monkeypatch.setattr(ds.time, "sleep", lambda s: None)
    (tmp_path / "CHANGELOG.md").write_text("| 标签 |\n")
    for e in (_ex("a#r1", "x"), _ex("b#r1", "y")):
        ds.append_example("es", e)
    ls = FakeLS()
    tag1, counts = ds.sync("es", "first", ls)
    tag2, _ = ds.sync("es", "again", ls)
    assert len(ls.examples) == 2 and counts == {"dev": 2, "heldout": 0}
    assert tag1.endswith("-1") and tag2.endswith("-2") and ls.tags == [tag1, tag2]
    with pytest.raises(ValueError):
        ds.sync("es", " ", ls)
