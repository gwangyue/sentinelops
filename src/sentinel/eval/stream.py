"""事故流评测（05 §4.2；M-E1 记忆增益）。

负责：
- 生成事故流：每个 dev 场景的 2 条录制分到两条流，每条流内用固定种子随机排序（在看结果之前生成并提交）；
- 按顺序运行：每条流一个临时记忆索引；每个用例结束后，用真实答案模拟"人确认根因"，写记忆、更新反馈；
- 评分：RunScore（含 memory_hit），另存记忆侧的指标（命中、精度、伤害）。
不负责：线上的确认流程（调查图的 confirm 节点在 M3）。
已知偏差（05 §4.2，必须如实说明）：模拟确认用的是真实答案，相当于"人每次都确认对"，比现实乐观。
事故流必须按顺序、带状态地跑，所以不走 LangSmith 的 evaluate()；每次调查的 trace 仍然上传（06 §9）。
"""

import json
import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from ulid import ULID

from sentinel.contracts import AgentVersion, Diagnosis, EvalExample, FactSheet, Finding, RunScore
from sentinel.core.api import (
    Change,
    EsMemoryStore,
    MemoryIndex,
    StoreMemoryProvider,
    build_fingerprint,
    embed,
    load_memory_policy,
    load_models_config,
    load_pack,
    memory_feedback,
    write_confirmed,
)
from sentinel.eval import results
from sentinel.eval.baselines import fetch_examples
from sentinel.eval.dataset import DATASETS
from sentinel.eval.guards import check_no_cache, require_dataset_tag
from sentinel.eval.runner import checked_version, run_one
from sentinel.eval.scorers import load_pricing, score

NAMESPACE = "es/es-target"


def streams_file(pack: str) -> Path:
    return DATASETS / "streams" / f"{pack}.yaml"


def make_streams(examples: Sequence[EvalExample], seed: int) -> dict[str, list[str]]:
    """两条流，每条包含每个 dev 场景的一条录制；流内顺序用固定种子打乱（避免"按结果挑顺序"）。"""
    by_scn: dict[str, list[str]] = defaultdict(list)
    for e in sorted(examples, key=lambda e: e.id):
        if e.split == "dev":
            by_scn[e.scenario_id].append(e.id)
    out: dict[str, list[str]] = {}
    for i, name in enumerate(("S1", "S2")):
        ids = [v[i] for _, v in sorted(by_scn.items()) if len(v) > i]
        random.Random(seed + i).shuffle(ids)
        out[name] = ids
    return out


def load_streams(pack: str) -> dict[str, list[str]]:
    return dict(yaml.safe_load(streams_file(pack).read_text(encoding="utf-8"))["streams"])


def occurrences(streams: dict[str, list[str]], labels: dict[str, str]) -> dict[str, int]:
    """每个用例是它所在的流里第几次遇到这个根因（1 = 第一次，没有相关记忆）。"""
    out: dict[str, int] = {}
    for ids in streams.values():
        seen: dict[str, int] = defaultdict(int)
        for eid in ids:
            seen[labels[eid]] += 1
            out[eid] = seen[labels[eid]]
    return out


@dataclass(frozen=True)
class MemoryRow:
    """记忆侧的指标（RunScore 之外）：L5 里出现了哪些记忆、精度、是否造成伤害。"""

    example_id: str
    rep: int
    stream: str
    occurrence: int
    truth: str
    l5: list[str]  # L5 里记忆的根因标签
    harm: bool  # top-1 来自记忆、并且是错的（05 §4.1；依赖模型自报的 from_memory）


def _l5_ids(bundle: dict[str, Any] | None) -> list[str]:
    return [i["ref"] for i in (bundle or {}).get("items", []) if i["layer"] == "L5" and i["included"]]


# 评分函数：(run_one 的输出, 用例, 第几次重复) → RunScore；外层把型号、单价、标签绑进去
ScoreFn = Callable[[dict[str, Any], EvalExample, int], RunScore]
RunFn = Callable[[EvalExample, StoreMemoryProvider | None], dict[str, Any]]


@dataclass
class StreamDeps:
    """事故流内层循环的依赖。线上用 ES 记忆索引和真实的 run_one，测试换成 LocalMemoryStore 和假的 run。"""

    run: RunFn
    score: ScoreFn
    make_store: Callable[[], MemoryIndex] | None  # None = 版本没打开 L5，不建记忆索引
    embed: Callable[[Sequence[str]], list[list[float]]]
    policy: dict[str, Any]
    descriptions: dict[str, str]  # 根因标签 → 一句话说明（写进记忆的 root_cause_text）


def run_stream_set(
    streams: dict[str, list[str]],
    examples: dict[str, EvalExample],
    reps: int,
    deps: StreamDeps,
    log: Callable[[str], None] = print,
    workers: int | None = None,
) -> tuple[list[RunScore], list[MemoryRow]]:
    """一条流内部必须按顺序（后面的用例要用前面写的记忆）；流与流、重复与重复之间没有共享状态，并行跑。
    每个（重复, 流）任务有自己的记忆索引；结果按任务的固定顺序合并，和并行完成的先后无关。"""
    labels = {eid: examples[eid].outputs.root_cause_label for ids in streams.values() for eid in ids}
    occ = occurrences(streams, labels)
    jobs = [(rep, name, ids) for rep in range(reps) for name, ids in streams.items()]

    def one_stream(rep: int, name: str, ids: list[str]) -> tuple[list[RunScore], list[MemoryRow]]:
        scores: list[RunScore] = []
        rows: list[MemoryRow] = []
        store = deps.make_store() if deps.make_store else None  # 每条流、每次重复都从空的记忆开始（05 §4.2）
        try:
            for eid in ids:
                s, row = _one(examples[eid], rep, name, occ[eid], store, deps)
                scores.append(s)
                rows.append(row)
                log(
                    f"rep{rep} {name} {eid} 第 {occ[eid]} 次  top1={s.top1_correct} 步数={s.steps} "
                    f"L5={row.l5}{'  伤害' if row.harm else ''}"
                )
        finally:
            if store is not None:
                store.drop()
        return scores, rows

    # TODO(edge)：某个任务失败时，其余任务照样跑完，最后才抛出第一个异常；部分结果不保存
    with ThreadPoolExecutor(max_workers=workers or len(jobs)) as pool:
        done = list(pool.map(lambda j: one_stream(*j), jobs))
    return [s for sc, _ in done for s in sc], [r for _, rw in done for r in rw]


def run_streams(
    pack: str,
    dataset_tag: str | None,
    reps: int,
    version_arg: str | None,
    log: Callable[[str], None] = print,
    workers: int | None = None,
) -> tuple[Path, Path]:
    tag = require_dataset_tag(dataset_tag)
    check_no_cache()
    version: AgentVersion = checked_version(version_arg)
    cfg = load_models_config()
    model = cfg.purposes["diagnosis"].model
    dims = int(cfg.purposes["embedding"].params()["output_dimensionality"])
    pricing = load_pricing()
    examples = {e.id: e for e in fetch_examples(pack, tag, "dev")}

    def make_store() -> MemoryIndex:
        st = EsMemoryStore(f"mem-eval-{str(ULID()).lower()}")
        st.ensure(dims)
        return st

    def run(ex: EvalExample, provider: StoreMemoryProvider | None) -> dict[str, Any]:
        return run_one(ex.inputs.model_dump(mode="json"), version, memory=provider)

    def score_fn(out: dict[str, Any], ex: EvalExample, rep: int) -> RunScore:
        return score(
            out=out,
            truth=ex.outputs,
            example_id=ex.id,
            rep=rep,
            agent_version=version.hash,
            dataset_tag=tag,
            model=model,
            pricing=pricing,
        )

    memory_on = bool(version.components.context_policy.get("layers", {}).get("L5"))
    deps = StreamDeps(
        run=run,
        score=score_fn,
        make_store=make_store if memory_on else None,
        embed=embed,
        policy=load_memory_policy(),
        descriptions={r.label: r.description for r in load_pack(pack).manifest.root_causes},
    )
    scores, rows = run_stream_set(load_streams(pack), examples, reps, deps, log, workers)
    path = results.result_file(f"v{version.seq}__stream", tag, "dev")
    results.save(path, scores)
    side = path.with_suffix(".memory.jsonl")
    side.write_text("".join(json.dumps(r.__dict__, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path, side


def _one(
    ex: EvalExample, rep: int, stream: str, occurrence: int, store: MemoryIndex | None, deps: StreamDeps
) -> tuple[RunScore, MemoryRow]:
    provider = StoreMemoryProvider(store, NAMESPACE, deps.policy, deps.embed) if store is not None else None
    out = deps.run(ex, provider)
    truth = ex.outputs.root_cause_label
    s = deps.score(out, ex, rep)
    l5 = _l5_ids(out.get("bundle"))
    l5_labels = [m.record.root_cause_label for i in l5 if store is not None and (m := store.get(i)) is not None]
    d = Diagnosis.model_validate(out["diagnosis"]) if out.get("diagnosis") else None
    h1 = d.hypotheses[0] if d and d.hypotheses else None
    harmed = list(h1.from_memory) if h1 and h1.from_memory and not s.top1_correct else []
    # memory_hit：这条流前面遇到过同一个根因时，相关记忆有没有进入 L5；第一次遇到时为 None（06 §3.1）
    s = s.model_copy(update={"memory_hit": (truth in l5_labels) if occurrence > 1 and store is not None else None})
    if store is not None:
        # 用真实答案模拟"人确认根因"（05 §4.2）：corrected = 模型的 H1 和真实答案不一致
        fp = build_fingerprint(
            [Finding.model_validate(f) for f in out.get("findings", [])],
            [FactSheet.model_validate(x) for x in out.get("fact_sheets", [])],
            [Change(**{**c, "since": datetime.fromisoformat(c["since"])}) for c in out.get("changes", [])],
        )
        noop: Callable[[str, dict[str, Any]], None] = lambda t, p: None  # noqa: E731  评测不写审计
        write_confirmed(
            store,
            namespace=NAMESPACE,
            investigation_id=out["inv_id"],
            fingerprint=fp,
            root_cause_label=truth,
            root_cause_description=deps.descriptions.get(truth, ""),
            corrected=not (h1 and h1.root_cause_label == truth),
            confirmed_by="sim:ground_truth",
            outcome="resolved",
            action_taken=None,
            target_version=ex.metadata.target_version,
            embed=deps.embed,
            policy=deps.policy,
            audit=noop,
        )
        memory_feedback.update(store, shown=l5, confirmed_label=truth, harmed=harmed, policy=deps.policy, audit=noop)
    return s, MemoryRow(ex.id, rep, stream, occurrence, truth, l5_labels, bool(harmed))


def memory_summary(side: Path, scores: Sequence[RunScore]) -> str:
    """05 §4.1 的三个记忆指标。伤害率的分母是全部运行：它衡量的是"开记忆"整体带来的副作用。"""
    if not side.exists():
        return "没有记忆侧文件"
    rows = [json.loads(x) for x in side.read_text(encoding="utf-8").splitlines() if x]
    hits = [s.memory_hit for s in scores if s.memory_hit is not None]
    shown = [(r["truth"], lab) for r in rows for lab in r["l5"]]
    harm = sum(r["harm"] for r in rows)
    hit = f"{sum(hits) / len(hits):.2f}（{sum(hits)}/{len(hits)}）" if hits else "—"
    prec = f"{sum(t == lab for t, lab in shown) / len(shown):.2f}（{len(shown)} 条）" if shown else "—"
    return f"命中率 {hit}  精度 {prec}  伤害率 {harm / len(rows):.2f}（{harm}/{len(rows)}）"
