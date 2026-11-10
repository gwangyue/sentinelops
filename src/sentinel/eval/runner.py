"""评测运行器（06 §4、§9；08 §6.4 的两条硬规则）。

负责：从 LangSmith 按版本标签和 split 拉用例；对每个用例在回放模式下跑完整的调查图（真实模型）；
      重复 N 次；评分写进 LangSmith feedback，同时存一份本地 RunScore。
不负责：统计比较（stats/）、基线（baselines.py）。
只能评测当前检出的代码：要评测旧版本，先切到对应的 git worktree（TODO(edge)）。
"""

import collections
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langsmith import Client, evaluate
from ulid import ULID

from sentinel.contracts import AgentVersion, ExampleOutputs, RunScore
from sentinel.core.detect import load_env
from sentinel.core.graph.investigation import Deps, build_graph
from sentinel.core.graph.run import load_packs
from sentinel.core.llm.chat import chat_model
from sentinel.core.llm.config import load_models_config
from sentinel.core.observations.store import ObservationStore
from sentinel.core.packs_registry import LoadedPack
from sentinel.core.store import MemoryStore, open_store
from sentinel.core.toolbackend import Mode, ToolBackend
from sentinel.core.versioning.components import behavior_dirty, collect_components, load_context_policy
from sentinel.core.versioning.freeze import require_frozen
from sentinel.eval import results
from sentinel.eval.dataset import REPO
from sentinel.eval.guards import check_no_cache, require_dataset_tag
from sentinel.eval.scorers import as_feedback, load_pricing, score


class EvalError(RuntimeError):
    pass


def current_version(version_arg: str | None) -> AgentVersion:
    packs = load_packs()
    v = require_frozen(open_store(), collect_components(list(packs.values())), dirty=behavior_dirty())
    if version_arg and version_arg not in (f"v{v.seq}", str(v.seq), v.hash):
        raise EvalError(f"只能评测当前检出的代码（v{v.seq}）；要评测 {version_arg}，先切到它对应的代码")
    return v


def run_one(inputs: dict[str, Any], version: AgentVersion) -> dict[str, Any]:
    """一次调查：独立的内存存储 + 回放 fixture_ref + 真实模型。不写 Postgres，不留审计（评测不是线上调查）。"""
    packs = load_packs()
    store = MemoryStore()
    tmp = Path(tempfile.mkdtemp(prefix="sentinel-eval-"))
    obs = ObservationStore(tmp, index=store)
    fixture = REPO / inputs["fixture_ref"]

    def make_backend(pack: LoadedPack, inv: str) -> ToolBackend:
        return ToolBackend(pack.tools, obs, inv, Mode.REPLAY, fixture=fixture)

    deps = Deps(
        store=store,
        obs_store=obs,
        packs=packs,
        make_backend=make_backend,
        make_model=chat_model,
        current_version=lambda: version,
        load_env=load_env,
        policy=load_context_policy(),
    )
    inv = f"inv_{ULID()}"
    graph = build_graph(deps, InMemorySaver())
    s = graph.invoke(
        {"inv_id": inv, "target": inputs["trigger"]["target"], "pack": "es", "mode": "report_only"},
        {"configurable": {"thread_id": inv}},
    )
    return {
        "status": s.get("status"),
        "escalate_reason": s.get("escalate_reason"),
        "diagnosis": s.get("diagnosis"),
        "bundle": s.get("bundle"),
        "observations": [o.model_dump(mode="json") for o in obs.list(inv)],
    }


@dataclass(frozen=True)
class RunSummary:
    experiment: str
    result_file: Path
    scores: list[RunScore]


def run(pack: str, split: str, dataset_tag: str | None, reps: int, version_arg: str | None) -> RunSummary:
    tag = require_dataset_tag(dataset_tag)
    check_no_cache()  # 被测 Agent 不开缓存（08 §6.4；06 §4.4）
    version = current_version(version_arg)
    model = load_models_config().purposes["diagnosis"].model
    pricing = load_pricing()
    client = Client()
    examples = list(client.list_examples(dataset_name=f"sentinel-{pack}", as_of=tag, splits=[split]))
    if not examples:
        raise EvalError(f"sentinel-{pack} 在 {tag} / {split} 下没有用例")

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        return run_one(inputs, version)

    def scorer(inputs: dict[str, Any], outputs: dict[str, Any], reference_outputs: dict[str, Any]) -> dict[str, Any]:
        s = score(
            out=outputs,
            truth=ExampleOutputs.model_validate(reference_outputs),
            example_id=inputs["example_id"],
            rep=0,
            agent_version=version.hash,
            dataset_tag=tag,
            model=model,
            pricing=pricing,
        )
        return {"results": as_feedback(s)}

    res = evaluate(
        target,
        data=examples,
        evaluators=[scorer],
        num_repetitions=reps,
        max_concurrency=4,
        metadata={
            "agent_version": version.hash,
            "agent_version_seq": version.seq,
            "dataset_tag": tag,
            "split": split,
            "pricing_basis": pricing[model]["basis"],
        },
        experiment_prefix=f"v{version.seq}",
        client=client,
    )
    # 本地也存一份 RunScore（统计从这里读）。rep 按同一用例出现的顺序编号
    seen: collections.Counter[str] = collections.Counter()
    scores = []
    for row in res:
        ex, run_ = row["example"], row["run"]
        eid = (ex.inputs or {})["example_id"]
        if run_.error or not run_.outputs:
            raise EvalError(f"{eid} 运行失败：{run_.error}")  # TODO(edge): 单个用例失败时记为错误而不是中止
        scores.append(
            score(
                out=run_.outputs,
                truth=ExampleOutputs.model_validate(ex.outputs or {}),
                example_id=eid,
                rep=seen[eid],
                agent_version=version.hash,
                dataset_tag=tag,
                model=model,
                pricing=pricing,
            )
        )
        seen[eid] += 1
    path = results.result_file(f"v{version.seq}", tag, split)
    results.save(path, scores)
    return RunSummary(res.experiment_name, path, scores)
