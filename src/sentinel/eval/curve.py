"""收敛曲线（06 §8）：每个版本在 dev 上的 top1 和按场景聚类的 95% 区间，heldout 单独标出。

负责：从 var/eval/ 的结果文件和版本表的 note 组装数据点，画成 PNG。
不负责：比较两个版本（stats/bootstrap.compare）。
画法的规定（06 §8）：每个点标版本号；无效改动画空心点，**不从图上删除**；heldout 用菱形；
数据集标签变了画一条竖线（跳变要能分清是数据集变了还是 Agent 变了，06 §5.2）。
"""

import re
from dataclasses import dataclass
from pathlib import Path

from sentinel.eval import results
from sentinel.eval.stats.bootstrap import interval

# note 里出现这些词，说明这一版按 06 §7.2 没有上线（第 5 条无效改动，或者作废）
INEFFECTIVE = ("无效", "不上线", "作废")
FILE = re.compile(r"^v(\d+)__(ds-[\w-]+)__(dev|heldout)\.jsonl$")


@dataclass(frozen=True)
class Point:
    seq: int
    tag: str
    split: str
    top1: float
    low: float
    high: float
    ineffective: bool
    note: str


def _tag(f: Path) -> str:
    m = FILE.match(f.name)
    assert m is not None
    return m.group(2)


def _tag_order(tag: str) -> tuple[int, ...]:
    # ds-20261110-6 → (20261110, 6)：按数字比，字符串比较时 -10 会排在 -6 前面
    return tuple(int(x) for x in tag.removeprefix("ds-").split("-"))


def points(root: Path, notes: dict[int, str]) -> list[Point]:
    """每个（版本, split）取最新的那个标签的结果；事故流、基线、侧文件都不算版本点。"""
    latest: dict[tuple[int, str], Path] = {}
    for f in root.glob("v*__ds-*.jsonl"):
        m = FILE.match(f.name)
        if not m:
            continue
        key = (int(m.group(1)), m.group(3))
        if key not in latest or _tag_order(_tag(latest[key])) < _tag_order(m.group(2)):
            latest[key] = f
    out = []
    for (seq, split), f in sorted(latest.items()):
        scores = results.load(f)
        top1, low, high = interval(scores)
        note = notes.get(seq, "")
        tag = _tag(f)
        out.append(Point(seq, tag, split, top1, low, high, any(w in note for w in INEFFECTIVE), note))
    return out


def render(pts: list[Point], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")  # 不需要显示器
    import matplotlib.pyplot as plt

    dev = [p for p in pts if p.split == "dev"]
    fig, ax = plt.subplots(figsize=(max(6, len(dev) * 0.8), 4))
    for p in dev:
        ax.errorbar(p.seq, p.top1, yerr=[[p.top1 - p.low], [p.high - p.top1]], fmt="none", ecolor="#888", capsize=3)
        ax.plot(p.seq, p.top1, "o", ms=8, mfc="white" if p.ineffective else "#1565c0", mec="#1565c0")
        ax.annotate(f"v{p.seq}", (p.seq, p.high), textcoords="offset points", xytext=(0, 6), ha="center", fontsize=8)
    for p in pts:
        if p.split == "heldout":
            ax.plot(p.seq, p.top1, "D", ms=8, color="#c62828")
    prev = None
    for p in dev:
        if prev is not None and p.tag != prev:
            ax.axvline(p.seq - 0.5, ls="--", color="#bbb", lw=1)
            ax.text(p.seq - 0.45, 0.02, p.tag, fontsize=7, color="#888", rotation=90, va="bottom")
        prev = p.tag
    ax.set_ylim(0, 1.08)
    ax.set_xlabel("Agent version")
    ax.set_ylabel("top-1 (95% CI, scenario-clustered)")
    ax.set_title("Convergence: filled = shipped, hollow = ineffective change, diamond = heldout")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
