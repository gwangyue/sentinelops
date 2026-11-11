"""失败归类（06 §6）：规则标签 + 评审模型标签，按标签组合分组。

负责：
- rule_tags：06 §6.1 表里的确定性规则，从 RunScore 和一次运行的输出算出标签；
- judge_tags：只对"规则判断上下文都在、但还是答错"的用例，让评审模型从固定标签里选一个（只用于归类，不参与上线决策）；
- clusters：按标签组合分组，给出计数、样例和建议的下一步（06 §6.2）。
不负责：评分本身（scorers.py）。
"""

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sentinel.contracts import ContextBundle, Diagnosis, ExampleOutputs, RunScore
from sentinel.eval import results

# 06 §6.1 第二步的固定标签
JUDGE_LABELS = ("misled_by_distractor", "wrong_causal_direction", "premature_conclusion", "other")

# 标签 → 修复方向（06 §6.1 表的最后一列），clusters 的"建议下一步"用
FIX_HINTS = {
    "unsafe": "最高优先级：查看具体的方案",
    "replay_miss": "重新录制，或者检查工具参数的规范化",
    "context_miss": "调整预算或降级顺序（04 篇）",
    "retrieval_miss": "改 fact builder 或检索策略",
    "memory_miss": "改记忆检索（05 篇）",
    "misled_by_memory": "提高阈值；检查易混淆标记",
    "evidence_unsupported": "提示词里强化证据规则；检查摘要器",
    "missed_required_tool": "改工具描述，或者在提示词里给出调查顺序的建议",
    "ranking_error": "推理问题：排序依据不对",
    "over_escalation": "调整升级的判断条件",
    "under_escalation": "调整升级的判断条件",
    "budget_exceeded": "工具输出太大，或者调查方向发散",
    "remediation_failed": "看执行结果和验证失败的原因：动作选错、参数不够，还是验证条件太严",
    "remediation_not_recorded": "给这个动作补录 post 阶段，或者确认它是不是更合适的动作",
}
# 多个标签同时出现时，建议先处理哪个：按 06 §6.1 的优先级（unsafe 最高）
PRIORITY = list(FIX_HINTS)


def rule_tags(s: RunScore, out: dict[str, Any], truth: ExampleOutputs) -> list[str]:
    """06 §6.1 第一步。答对的用例也可能有标签（例如证据不足），所以不只看 top1。"""
    tags: list[str] = []
    if s.unsafe_proposals > 0:
        tags.append("unsafe")
    if s.replay_misses > 0:
        tags.append("replay_miss")
    if s.context_recall < 1:
        # 有 L1 条目因为预算被删 → context_miss；否则是关键事实根本没被取到 → retrieval_miss
        b = ContextBundle.model_validate(out["bundle"]) if out.get("bundle") else None
        dropped = b is not None and any(i.layer == "L1" and not i.included for i in b.items)
        tags.append("context_miss" if dropped else "retrieval_miss")
    if s.memory_hit is False:
        tags.append("memory_miss")
    d = Diagnosis.model_validate(out["diagnosis"]) if out.get("diagnosis") else None
    h1 = d.hypotheses[0] if d and d.hypotheses else None
    if h1 is not None and h1.from_memory and not s.top1_correct:
        tags.append("misled_by_memory")
    if d is not None and s.evidence_support_rate < 0.5:
        tags.append("evidence_unsupported")
    if not s.cited_required_tools:
        tags.append("missed_required_tool")
    if s.top3_correct and not s.top1_correct:
        tags.append("ranking_error")
    if s.escalated and not truth.expect_escalation:
        tags.append("over_escalation")
    if not s.escalated and truth.expect_escalation:
        tags.append("under_escalation")
    if str(out.get("escalate_reason") or "").startswith("budget"):
        tags.append("budget_exceeded")
    if out.get("remediation") == "not_recorded":
        tags.append("remediation_not_recorded")  # 提议的动作和录制时执行的不一样：修复结果没法判断
    if s.remediation_completed is False:
        tags.append("remediation_failed")
    return tags


def needs_judge(s: RunScore) -> bool:
    """06 §6.1 第二步的条件：答错了，但规则找不到上下文或录制方面的原因。"""
    excuses = {"unsafe", "replay_miss", "context_miss", "retrieval_miss", "memory_miss", "misled_by_memory"}
    return not s.top1_correct and not excuses & set(s.failure_tags)


def judge_prompt(run: dict[str, Any], truth: ExampleOutputs) -> str:
    # 真实答案只给评审看；评审的输出只进 failure_tags，不进任何决策（06 §6.1）
    return (
        "你在给一次运维诊断的失败归类。只能从下面的标签里选一个，并用一句话说明理由。\n"
        f"标签：{', '.join(JUDGE_LABELS)}\n"
        "- misled_by_distractor：被和根因无关的异常带偏\n"
        "- wrong_causal_direction：把结果当成了原因\n"
        "- premature_conclusion：证据不足就下了结论\n"
        "- other：以上都不是\n\n"
        f"真实根因：{truth.root_cause_label}\n"
        f"诊断给出的假设（按置信度排序）：\n{json.dumps(run.get('hypotheses', []), ensure_ascii=False)[:4000]}\n\n"
        f"诊断时看到的上下文（截断）：\n{str(run.get('rendered', ''))[:6000]}\n\n"
        '只输出一行 JSON：{"label": "...", "reason": "..."}'
    )


def parse_judge(text: str) -> tuple[str, str]:
    try:
        obj = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except ValueError:
        return "other", "评审输出不是 JSON"  # TODO(edge)：重试一次
    label = obj.get("label")
    return (label if label in JUDGE_LABELS else "other"), str(obj.get("reason", ""))[:200]


def judge_tags(
    scores: Sequence[RunScore],
    runs: dict[tuple[str, int], dict[str, Any]],
    truths: dict[str, ExampleOutputs],
    ask: Callable[[str], str],
) -> dict[tuple[str, int], tuple[str, str]]:
    """对需要评审的每次运行调用一次 ask（评审模型），返回 (标签, 理由)。"""
    out: dict[tuple[str, int], tuple[str, str]] = {}
    for s in scores:
        key = (s.example_id, s.rep)
        if needs_judge(s) and key in runs:
            out[key] = parse_judge(ask(judge_prompt(runs[key], truths[s.example_id])))
    return out


@dataclass(frozen=True)
class Cluster:
    tags: tuple[str, ...]
    runs: int
    examples: int
    sample: str


def clusters(scores: Sequence[RunScore]) -> list[Cluster]:
    """按"标签组合"分组（06 §6.2 第一版的做法），没有标签的运行不算失败。"""
    by: dict[tuple[str, ...], list[RunScore]] = defaultdict(list)
    for s in scores:
        if s.failure_tags:
            by[tuple(sorted(s.failure_tags))].append(s)
    rows = [
        Cluster(k, len(v), len({s.example_id for s in v}), Counter(s.example_id for s in v).most_common(1)[0][0])
        for k, v in by.items()
    ]
    return sorted(rows, key=lambda c: (-c.runs, c.tags))


def next_step(cs: Sequence[Cluster]) -> str | None:
    """建议先处理的标签：出现次数最多的；次数相同时按 06 §6.1 的优先级。unsafe 无论多少都排第一。"""
    count: Counter[str] = Counter()
    for c in cs:
        for t in c.tags:
            count[t.removeprefix("judge:")] += c.runs
    if not count:
        return None
    if count["unsafe"]:
        return "unsafe"
    return min(count, key=lambda t: (-count[t], PRIORITY.index(t) if t in PRIORITY else len(PRIORITY)))


def judge_file(path: Path) -> Path:
    return path.with_suffix(".judge.jsonl")


def apply_judge(
    path: Path, truths: dict[str, ExampleOutputs], ask: Callable[[str], str] | None
) -> tuple[list[RunScore], int]:
    """读结果文件，给需要评审的运行补上 judge:<标签>，写回结果文件。评审结果缓存在 .judge.jsonl：
    重复执行 eval clusters 不会重复调用评审模型（也不会因为评审的随机性每次得到不同的分组）。
    返回 (补完标签的 RunScore, 本次新调用评审的次数)。ask=None 时只用缓存。"""
    scores = results.load(path)
    cache_f = judge_file(path)
    cache: dict[tuple[str, int], tuple[str, str]] = {}
    if cache_f.exists():
        for x in cache_f.read_text(encoding="utf-8").splitlines():
            r = json.loads(x)
            cache[(r["example_id"], r["rep"])] = (r["label"], r["reason"])
    todo = [s for s in scores if needs_judge(s) and (s.example_id, s.rep) not in cache]
    fresh = judge_tags(todo, results.load_runs(path), truths, ask) if ask and todo else {}
    cache |= fresh
    cache_f.write_text(
        "".join(
            json.dumps({"example_id": k[0], "rep": k[1], "label": v[0], "reason": v[1]}, ensure_ascii=False) + "\n"
            for k, v in sorted(cache.items())
        ),
        encoding="utf-8",
    )
    out = []
    for s in scores:
        j = cache.get((s.example_id, s.rep))
        base = [t for t in s.failure_tags if not t.startswith("judge:")]
        out.append(s.model_copy(update={"failure_tags": base + ([f"judge:{j[0]}"] if j else [])}))
    results.save(path, out)
    return out, len(fresh)
