"""按场景聚类的配对 bootstrap（06 §4.2），以及两个稳定性指标（06 §4.4）。

负责：比较两组 RunScore 的主指标 top1：
  1. 每个用例的得分 = 它多次运行的均值；两边都有的用例才参与比较（配对）；
  2. 差值 d = B − A，按场景平均得到 d_s；
  3. 重复 n 次：有放回地抽 S 个场景，算 d_s 的均值；取 2.5% 和 97.5% 分位数。
  replay_misses > 0 的用例从主指标里剔除，单独列出（06 §3.1）。
不负责：多重比较校正——06 §3.3 规定只有一个主指标，所以不需要。
"""

import random
import statistics
from collections import defaultdict
from dataclasses import dataclass

from sentinel.contracts import RunScore


def scenario_of(example_id: str) -> str:
    return example_id.rsplit("#r", 1)[0]


def per_example(scores: list[RunScore]) -> dict[str, float]:
    runs: dict[str, list[float]] = defaultdict(list)
    for s in scores:
        runs[s.example_id].append(float(s.top1_correct))
    return {k: statistics.fmean(v) for k, v in runs.items()}


@dataclass(frozen=True)
class Comparison:
    point: float  # 点估计：所有用例差值的均值（06 §3.2）
    low: float
    high: float
    verdict: str
    n_scenarios: int
    n_examples: int
    excluded_replay_miss: list[str]
    a_score: float
    b_score: float


def compare(a: list[RunScore], b: list[RunScore], *, n_boot: int = 2000, seed: int = 0) -> Comparison:
    excluded = sorted({s.example_id for s in a + b if s.replay_misses > 0})
    ea, eb = (
        per_example([s for s in a if s.example_id not in excluded]),
        per_example([s for s in b if s.example_id not in excluded]),
    )
    common = sorted(set(ea) & set(eb))
    if not common:
        raise ValueError("两组结果没有共同的用例：确认是同一个数据集标签和 split")
    diffs = {e: eb[e] - ea[e] for e in common}
    by_scn: dict[str, list[float]] = defaultdict(list)
    for e, d in diffs.items():
        by_scn[scenario_of(e)].append(d)
    d_s = [statistics.fmean(v) for v in by_scn.values()]
    rng = random.Random(seed)  # 固定种子：同样的输入，区间完全一样
    boots = sorted(statistics.fmean(rng.choices(d_s, k=len(d_s))) for _ in range(n_boot))
    low, high = boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1]
    verdict = "提升" if low > 0 else "退步" if high < 0 else "无法判断"
    return Comparison(
        point=statistics.fmean(diffs.values()),
        low=low,
        high=high,
        verdict=verdict,
        n_scenarios=len(d_s),
        n_examples=len(common),
        excluded_replay_miss=excluded,
        a_score=statistics.fmean(ea[e] for e in common),
        b_score=statistics.fmean(eb[e] for e in common),
    )


@dataclass(frozen=True)
class Stability:
    unstable_rate: float  # 多次运行 top1 不完全一致的用例比例
    mean_within_var: float  # 用例内方差的均值


def stability(scores: list[RunScore]) -> Stability:
    runs: dict[str, list[float]] = defaultdict(list)
    for s in scores:
        runs[s.example_id].append(float(s.top1_correct))
    multi = [v for v in runs.values() if len(v) > 1]
    if not multi:
        return Stability(0.0, 0.0)
    return Stability(
        unstable_rate=sum(1 for v in multi if len(set(v)) > 1) / len(multi),
        mean_within_var=statistics.fmean(statistics.pvariance(v) for v in multi),
    )
