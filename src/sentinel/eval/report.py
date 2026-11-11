"""治理周报的第 4 项"评测状态"（07 §8），以及"需要你决定的事"里来自评测和数据集的条目。

负责：当前上线版本（datasets/version_decisions.yaml 里最新的 shipped）在每个专家包 dev 上的 top1 和区间；
      保留测试集用过几次、距离上次运行几天；线上修正补进数据集的条数；dev 里样本太少的难度组。
不负责：来自审计的数字（governance/report.py）。
保留测试集的使用次数和最近一次的日期读 datasets/HELDOUT_LOG.md（06 §5.2；中断的运行也算一次，结果文件里没有它）。
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from sentinel.eval import results
from sentinel.eval.dataset import DATASETS, load_examples
from sentinel.eval.stats.bootstrap import interval

DECISIONS = DATASETS / "version_decisions.yaml"
CHANGELOG = DATASETS / "CHANGELOG.md"
HELDOUT_LOG = DATASETS / "HELDOUT_LOG.md"
FILE = re.compile(r"^v(\d+)(__remediate)?__(ds-[\w-]+)__(dev|heldout)\.jsonl$")
MIN_SCENARIOS = 3  # dev 里某个难度组的场景少于这个数，区间没有意义（06 §4.3）
HELDOUT_STALE_DAYS = 14


@dataclass
class PackEval:
    pack: str
    version: int | None  # 有 dev 结果的版本（优先当前上线版本）
    tag: str | None
    top1: float | None
    low: float | None
    high: float | None
    heldout_runs: int
    heldout_days_ago: int | None
    online_corrections: int
    small_groups: dict[str, int] = field(default_factory=dict)


def shipped_version(path: Path = DECISIONS) -> int | None:
    rows = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("versions") or {}
    shipped = [int(k) for k, v in rows.items() if v.get("decision") == "shipped"]
    return max(shipped) if shipped else None


def tags_by_pack(path: Path = CHANGELOG) -> dict[str, list[str]]:
    """CHANGELOG 的标签表：| 标签 | 领域 | …。按出现顺序（越往后越新）。"""
    out: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) > 2 and cells[0].startswith("ds-"):
            out.setdefault(cells[1], []).append(cells[0])
    return out


def heldout_uses(pack: str, path: Path = HELDOUT_LOG) -> list[datetime]:
    """HELDOUT_LOG.md 表格里这个领域的每一行（| 日期 | 里程碑 | 领域 | …）就是一次使用。"""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) > 2 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]) and cells[2] == pack:
            out.append(datetime.fromisoformat(cells[0]).replace(tzinfo=UTC))
    return out


def pack_eval(pack: str, shipped: int | None, now: datetime, root: Path | None = None) -> PackEval:
    root = root or results.ROOT
    tags = tags_by_pack().get(pack, [])
    files = [(m, f) for f in root.glob("v*.jsonl") if (m := FILE.match(f.name)) and m.group(3) in tags]
    dev = [(int(m.group(1)), tags.index(m.group(3)), m.group(2) is None, f) for m, f in files if m.group(4) == "dev"]
    # 当前上线版本、最新标签、报告模式优先；当前版本没有结果时退回到最近一个有结果的版本（周报里会写明是哪一版）
    dev.sort(key=lambda x: (x[0] == shipped, x[1], x[0], x[2]))
    top1 = low = high = None
    version = tag = None
    if dev:
        version, _, _, f = dev[-1]
        tag = FILE.match(f.name).group(3)  # type: ignore[union-attr]
        top1, low, high = interval(results.load(f))
    uses = heldout_uses(pack)
    examples = load_examples(pack)
    dev_scn = {e.scenario_id: e.metadata.difficulty for e in examples if e.split == "dev"}
    groups = Counter(dev_scn.values())
    return PackEval(
        pack=pack,
        version=version,
        tag=tag,
        top1=top1,
        low=low,
        high=high,
        heldout_runs=len(uses),
        heldout_days_ago=(now - max(uses)).days if uses else None,
        online_corrections=sum(1 for e in examples if e.metadata.origin == "online_correction"),
        small_groups={d: n for d, n in groups.items() if n < MIN_SCENARIOS},
    )
