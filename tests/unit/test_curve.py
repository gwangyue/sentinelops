"""收敛曲线（06 §8）：每个（版本, split）取最新标签；无效改动是空心点；区间按场景聚类；能画出 PNG。"""

from pathlib import Path

from sentinel.contracts import RunScore
from sentinel.eval import results
from sentinel.eval.curve import points, render
from sentinel.eval.stats.bootstrap import interval
from tests.contracts.factories import all_samples


def score(eid: str, top1: bool) -> RunScore:
    s = all_samples()["run_score"]
    assert isinstance(s, RunScore)
    return s.model_copy(update={"example_id": eid, "top1_correct": top1})


def test_points_pick_latest_tag_and_mark_ineffective(tmp_path: Path) -> None:
    good = [score("a.x#r1", True), score("b.y#r1", True)]
    half = [score("a.x#r1", True), score("b.y#r1", False)]
    results.save(tmp_path / "v3__ds-20261110-6__dev.jsonl", half)
    results.save(tmp_path / "v3__ds-20261110-10__dev.jsonl", good)  # 按数字比：-10 比 -6 新
    results.save(tmp_path / "v4__ds-20261110-10__dev.jsonl", half)
    results.save(tmp_path / "v4__ds-20261110-10__heldout.jsonl", good)
    results.save(tmp_path / "v4__stream__ds-20261110-10__dev.jsonl", good)  # 事故流不算版本点
    pts = points(tmp_path, {3: "加入 L3", 4: "L4"}, {4: "ineffective"})
    assert [(p.seq, p.split, p.tag) for p in pts] == [
        (3, "dev", "ds-20261110-10"),
        (4, "dev", "ds-20261110-10"),
        (4, "heldout", "ds-20261110-10"),
    ]
    assert pts[0].top1 == 1.0 and not pts[0].ineffective and pts[1].ineffective
    render(pts, tmp_path / "c.png")
    assert (tmp_path / "c.png").read_bytes()[:4] == b"\x89PNG"


def test_interval_resamples_scenarios() -> None:
    top1, low, high = interval([score("a.x#r1", True), score("b.y#r1", False), score("c.z#r1", True)])
    assert abs(top1 - 2 / 3) < 1e-9 and 0.0 <= low <= top1 <= high <= 1.0 and low < high
