"""事故流运行器（05 §4.2）：顺序、带状态；第 2 次遇到同一根因时记忆命中；伤害记录；临时索引用完删掉。

离线：假的 run（代替 run_one，不调用模型），记忆存储用 LocalMemoryStore，向量用 test_memory 的假向量。
run 里真的调用 provider.similar，所以检索、写入、反馈走的都是真实代码。
"""

import json
from pathlib import Path
from typing import Any, ClassVar

from sentinel.contracts import Diagnosis, EvalExample, RunScore
from sentinel.core.memory.provider import StoreMemoryProvider
from sentinel.core.memory.store import LocalMemoryStore
from sentinel.eval.stream import StreamDeps, make_streams, memory_summary, occurrences, run_stream_set
from tests.contracts.factories import all_samples
from tests.unit.test_memory import POLICY, fake_embed, finding, sheet

SAMPLES = all_samples()
DISK, NODE = "es.disk_high_watermark", "es.node_left"
FINDINGS = {
    DISK: [finding("es.unassigned_shards"), finding("es.disk_watermark", "es-target-es-data-2")],
    NODE: [finding("es.unassigned_shards"), finding("es.node_left", "es-target-es-data-1")],
}


def example(scenario: str, k: int, label: str) -> EvalExample:
    ex = SAMPLES["eval_example"]
    assert isinstance(ex, EvalExample)
    return ex.model_copy(
        update={
            "id": f"{scenario}#r{k}",
            "scenario_id": scenario,
            "outputs": ex.outputs.model_copy(update={"root_cause_label": label}),
        }
    )


class TrackedStore(LocalMemoryStore):
    made: ClassVar[list["TrackedStore"]] = []

    def __init__(self) -> None:
        super().__init__()
        self.dropped = False
        self.seen: int = 0
        self.harms: dict[str, int] = {}
        TrackedStore.made.append(self)

    def drop(self) -> None:
        self.seen = len(self.items)
        self.harms = {m.record.root_cause_label: m.harm_count for m in self.items.values()}
        self.dropped = True
        super().drop()


def fake_run(wrong: set[str]) -> Any:
    """按真实根因给出问题事件；有记忆就放进 L5。wrong 里的用例：模型照抄 L5 的第一条记忆（用来制造伤害）。"""

    def run(ex: EvalExample, provider: StoreMemoryProvider | None) -> dict[str, Any]:
        label = ex.outputs.root_cause_label
        fs, sh = FINDINGS[label], [sheet("es-target-es-data-2", 92.0)]
        hits = provider.similar(fs, sh, [], 3) if provider else []
        h1 = (SAMPLES["diagnosis"]).model_dump(mode="json")["hypotheses"][0]
        if ex.id in wrong and hits:
            h1 |= {"root_cause_label": hits[0].root_cause_label, "from_memory": [hits[0].memory_id]}
        else:
            h1 |= {"root_cause_label": label, "from_memory": []}
        d = SAMPLES["diagnosis"].model_dump(mode="json") | {"hypotheses": [h1]}
        items = [{"layer": "L5", "ref": h.memory_id, "tokens": 10, "included": True, "drop_reason": None} for h in hits]
        return {
            "diagnosis": d,
            "bundle": {"items": items},
            "findings": [f.model_dump(mode="json") for f in fs],
            "fact_sheets": [s.model_dump(mode="json") for s in sh],
            "changes": [],
            "inv_id": f"inv_{ex.id}",
        }

    return run


def fake_score(out: dict[str, Any], ex: EvalExample, rep: int) -> RunScore:
    d = Diagnosis.model_validate(out["diagnosis"])
    s = SAMPLES["run_score"]
    assert isinstance(s, RunScore)
    return s.model_copy(
        update={
            "example_id": ex.id,
            "rep": rep,
            "top1_correct": d.hypotheses[0].root_cause_label == ex.outputs.root_cause_label,
        }
    )


def deps(wrong: set[str], memory: bool = True) -> StreamDeps:
    return StreamDeps(
        run=fake_run(wrong),
        score=fake_score,
        make_store=TrackedStore if memory else None,
        embed=fake_embed,
        policy=POLICY,
        descriptions={DISK: "disk over high watermark", NODE: "node left the cluster"},
    )


EXAMPLES = {
    e.id: e
    for e in [
        example("es.disk.a", 0, DISK),
        example("es.node.a", 0, NODE),
        example("es.disk.b", 0, DISK),
        example("es.node.b", 0, NODE),
    ]
}
STREAM = {"S1": ["es.disk.a#r0", "es.node.a#r0", "es.disk.b#r0", "es.node.b#r0"]}


def test_make_streams_one_recording_per_scenario_fixed_order() -> None:
    exs = [example(f"es.s{i}", k, DISK) for i in range(5) for k in (0, 1)]
    s = make_streams(exs, seed=0)
    assert sorted(s) == ["S1", "S2"]
    assert all(e.endswith("#r0") for e in s["S1"]) and all(e.endswith("#r1") for e in s["S2"])
    assert len(s["S1"]) == 5 and make_streams(exs, seed=0) == s  # 同一个种子，顺序完全一样


def test_occurrence_counts_per_stream() -> None:
    occ = occurrences(STREAM, {k: v.outputs.root_cause_label for k, v in EXAMPLES.items()})
    assert [occ[e] for e in STREAM["S1"]] == [1, 1, 2, 2]


def test_second_occurrence_hits_memory_and_store_is_dropped() -> None:
    TrackedStore.made.clear()
    scores, rows = run_stream_set(STREAM, EXAMPLES, 2, deps(set()), log=lambda _: None)
    hit = {(s.rep, s.example_id): s.memory_hit for s in scores}
    assert hit[(0, "es.disk.a#r0")] is None and hit[(0, "es.node.a#r0")] is None  # 第一次遇到：没有相关记忆
    assert hit[(0, "es.disk.b#r0")] is True and hit[(0, "es.node.b#r0")] is True
    # 每次重复都从空的记忆开始：rep1 的第一个用例 L5 也是空的
    assert next(r for r in rows if r.rep == 1 and r.example_id == "es.disk.a#r0").l5 == []
    assert len(TrackedStore.made) == 2 and all(st.dropped and st.seen == 2 for st in TrackedStore.made)
    assert not any(r.harm for r in rows)


def test_harm_when_top1_copies_wrong_memory(tmp_path: Path) -> None:
    # 反例（05 §4.1）：节点离开和磁盘水位的症状很像，磁盘的记忆进了 L5；模型照抄它，top-1 错了 → 记一次伤害
    TrackedStore.made.clear()
    stream = {"S1": ["es.disk.a#r0", "es.node.b#r0"]}
    scores, rows = run_stream_set(stream, EXAMPLES, 1, deps({"es.node.b#r0"}), log=lambda _: None)
    r = rows[1]
    assert r.l5 == [DISK] and r.harm and not scores[1].top1_correct
    assert TrackedStore.made[0].harms[DISK] == 1  # 反馈写回了那条记忆（05 §3.7）
    side = tmp_path / "x.memory.jsonl"
    side.write_text("".join(json.dumps(x.__dict__) + "\n" for x in rows), encoding="utf-8")
    assert "精度 0.00（1 条）" in memory_summary(side, scores) and "伤害率 0.50（1/2）" in memory_summary(side, scores)


def test_control_without_memory_has_no_l5() -> None:
    scores, rows = run_stream_set(STREAM, EXAMPLES, 1, deps(set(), memory=False), log=lambda _: None)
    assert all(s.memory_hit is None for s in scores) and all(r.l5 == [] for r in rows)


def test_parallel_jobs_have_own_stores_and_fixed_order() -> None:
    # 6 个（重复, 流）任务并行：每个任务一个独立的记忆索引；合并后的顺序和串行时一样
    TrackedStore.made.clear()
    two = {"S1": STREAM["S1"], "S2": list(reversed(STREAM["S1"]))}
    scores, rows = run_stream_set(two, EXAMPLES, 3, deps(set()), log=lambda _: None)
    assert len(TrackedStore.made) == 6 and all(st.dropped and st.seen == 2 for st in TrackedStore.made)
    assert [(r.rep, r.stream, r.example_id) for r in rows] == [
        (rep, name, eid) for rep in range(3) for name, ids in two.items() for eid in ids
    ]
    serial, _ = run_stream_set(two, EXAMPLES, 3, deps(set()), log=lambda _: None, workers=1)
    assert [s.memory_hit for s in scores] == [s.memory_hit for s in serial]
