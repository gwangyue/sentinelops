"""评测结果的本地存储：每次实验一个 JSONL 文件，每行一条 RunScore（03 §6.3）。

统计直接读这些文件。LangSmith 上也有同样的 feedback，用来看界面；从 LangSmith 拉回结果是 TODO(edge)（06 §9）。
"""

import json
from pathlib import Path
from typing import Any

from sentinel.contracts import RunScore

ROOT = Path(__file__).resolve().parents[3] / "var" / "eval"


def result_file(name: str, dataset_tag: str, split: str) -> Path:
    """name 是 "v5" 或 "baseline-rules" 这样的名字；同一个名字、标签、split 只保留最近一次。"""
    return ROOT / f"{name}__{dataset_tag}__{split}.jsonl"


def save(path: Path, scores: list[RunScore]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(s.model_dump_json() + "\n" for s in scores), encoding="utf-8")


def load(path: Path) -> list[RunScore]:
    return [RunScore.model_validate_json(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def runs_file(path: Path) -> Path:
    """评审模型要看诊断过程：每次运行的假设和渲染后的上下文另存一份（06 §6.1 第二步）。"""
    return path.with_suffix(".runs.jsonl")


def run_record(example_id: str, rep: int, out: dict[str, Any]) -> dict[str, Any]:
    d = out.get("diagnosis") or {}
    return {
        "example_id": example_id,
        "rep": rep,
        "hypotheses": [
            {k: h.get(k) for k in ("root_cause", "root_cause_label", "confidence", "claims")}
            for h in d.get("hypotheses", [])
        ],
        "rendered": (out.get("bundle") or {}).get("rendered", ""),
        "escalate_reason": out.get("escalate_reason"),
    }


def save_runs(path: Path, rows: list[dict[str, Any]]) -> None:
    runs_file(path).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def load_runs(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    f = runs_file(path)
    if not f.exists():
        return {}
    rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x]
    return {(r["example_id"], r["rep"]): r for r in rows}
