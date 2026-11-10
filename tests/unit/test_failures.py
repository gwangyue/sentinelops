"""失败归类（06 §6）：每条规则一个用例；评审只处理"上下文都在但答错"；按标签组合分组；建议的下一步。"""

from typing import Any

import pytest

from sentinel.contracts import ExampleOutputs, RunScore
from sentinel.eval.failures import clusters, judge_tags, needs_judge, next_step, parse_judge, rule_tags
from tests.contracts.factories import all_samples

SAMPLES = all_samples()


def base() -> RunScore:
    s = SAMPLES["run_score"]
    assert isinstance(s, RunScore)
    return s


def truth(**kw: Any) -> ExampleOutputs:
    empty: dict[str, Any] = {"acceptable_actions": [], "forbidden_actions": [], "must_cite_tools": [], "key_facts": []}
    return ExampleOutputs(root_cause_label="es.disk_high_watermark", **(empty | kw))


def out(*, dropped: bool = False, from_memory: list[str] | None = None, reason: str | None = None) -> dict[str, Any]:
    d = SAMPLES["diagnosis"].model_dump(mode="json")
    d["hypotheses"][0]["from_memory"] = from_memory or []
    item = {
        "layer": "L1",
        "ref": "obs_1",
        "tokens": 10,
        "included": not dropped,
        "drop_reason": "budget" if dropped else None,
    }
    bundle = SAMPLES["context_bundle"].model_dump(mode="json") | {"items": [item]}
    return {"diagnosis": d, "bundle": bundle, "escalate_reason": reason}


@pytest.mark.parametrize(
    ("update", "o", "t", "tag"),
    [
        ({"unsafe_proposals": 1}, out(), truth(), "unsafe"),
        ({"replay_misses": 2}, out(), truth(), "replay_miss"),
        ({"context_recall": 0.5}, out(dropped=True), truth(), "context_miss"),
        ({"context_recall": 0.5}, out(), truth(), "retrieval_miss"),
        ({"memory_hit": False}, out(), truth(), "memory_miss"),
        ({"top1_correct": False}, out(from_memory=["mem_1"]), truth(), "misled_by_memory"),
        ({"evidence_support_rate": 0.4}, out(), truth(), "evidence_unsupported"),
        ({"cited_required_tools": False}, out(), truth(), "missed_required_tool"),
        ({"top1_correct": False, "top3_correct": True}, out(), truth(), "ranking_error"),
        ({"escalated": True}, out(), truth(), "over_escalation"),
        ({}, out(), truth(expect_escalation=True), "under_escalation"),
        ({"escalated": True}, out(reason="budget: 超出"), truth(), "budget_exceeded"),
    ],
)
def test_each_rule(update: dict[str, Any], o: dict[str, Any], t: ExampleOutputs, tag: str) -> None:
    assert tag in rule_tags(base().model_copy(update=update), o, t)


def test_clean_run_has_no_tags() -> None:
    assert rule_tags(base(), out(), truth()) == []


def test_judge_only_when_context_present_but_wrong() -> None:
    wrong = base().model_copy(update={"top1_correct": False, "failure_tags": ["ranking_error"]})
    excused = wrong.model_copy(update={"failure_tags": ["context_miss"]})
    assert needs_judge(wrong) and not needs_judge(excused) and not needs_judge(base())
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return '好的 {"label": "misled_by_distractor", "reason": "被 X2 带偏"}'

    got = judge_tags(
        [wrong, excused],
        {(wrong.example_id, 0): {"hypotheses": [], "rendered": "ctx"}},
        {wrong.example_id: truth()},
        ask,
    )
    assert got == {(wrong.example_id, 0): ("misled_by_distractor", "被 X2 带偏")} and len(asked) == 1
    assert "es.disk_high_watermark" in asked[0]


def test_parse_judge_rejects_unknown_labels() -> None:
    assert parse_judge('{"label": "made_up", "reason": "x"}')[0] == "other"
    assert parse_judge("不是 JSON")[0] == "other"


def test_clusters_group_by_tag_combination_and_suggest_next() -> None:
    a = base().model_copy(update={"example_id": "x#r1", "failure_tags": ["ranking_error"]})
    b = base().model_copy(update={"example_id": "y#r1", "failure_tags": ["context_miss", "ranking_error"]})
    cs = clusters([a, a, b, base()])
    assert [(c.tags, c.runs) for c in cs] == [(("ranking_error",), 2), (("context_miss", "ranking_error"), 1)]
    assert next_step(cs) == "ranking_error"  # 出现 3 次，最多
    assert next_step(clusters([a, base().model_copy(update={"failure_tags": ["unsafe"]})])) == "unsafe"
    assert next_step([]) is None


def test_apply_judge_caches_and_writes_back(tmp_path: Any) -> None:
    from sentinel.eval import results
    from sentinel.eval.failures import apply_judge

    wrong = base().model_copy(update={"top1_correct": False, "failure_tags": ["ranking_error"]})
    path = tmp_path / "v1__t__dev.jsonl"
    results.save(path, [wrong, base()])
    results.save_runs(path, [{"example_id": wrong.example_id, "rep": 0, "hypotheses": [], "rendered": ""}])
    calls: list[str] = []

    def ask(p: str) -> str:
        calls.append(p)
        return '{"label": "premature_conclusion", "reason": "r"}'

    scores, n = apply_judge(path, {wrong.example_id: truth()}, ask)
    assert n == 1 and scores[0].failure_tags == ["ranking_error", "judge:premature_conclusion"]
    again, n2 = apply_judge(path, {wrong.example_id: truth()}, ask)  # 第二次只用缓存，标签不重复
    assert n2 == 0 and len(calls) == 1 and again[0].failure_tags == scores[0].failure_tags
    assert results.load(path)[0].failure_tags == scores[0].failure_tags
