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

from sentinel.contracts import AgentVersion, ExampleOutputs, RunScore
from sentinel.core.api import (
    MemoryProvider,
    MemoryStore,
    Mode,
    ObservationStore,
    current_version,
    investigate,
    load_models_config,
    load_packs,
    make_deps,
    open_store,
    resume,
)
from sentinel.eval import results
from sentinel.eval.dataset import REPO
from sentinel.eval.guards import check_no_cache, require_dataset_tag
from sentinel.eval.replay_exec import approve_all, replay_remediation
from sentinel.eval.scorers import as_feedback, load_pricing, score
from sentinel.governance.remediation_store import MemoryRemediationStore


class EvalError(RuntimeError):
    pass


def checked_version(version_arg: str | None) -> AgentVersion:
    v = current_version(open_store())
    if version_arg and version_arg not in (f"v{v.seq}", str(v.seq), v.hash):
        raise EvalError(f"只能评测当前检出的代码（v{v.seq}）；要评测 {version_arg}，先切到它对应的代码")
    return v


def run_one(
    inputs: dict[str, Any],
    version: AgentVersion,
    memory: MemoryProvider | None = None,
    mode: str = "report_only",
    pack: str = "es",
) -> dict[str, Any]:
    """一次调查：和线上同一套组装（core.make_deps），只是换成独立的内存存储 + 回放 fixture_ref。
    不写 Postgres，不留审计（评测不是线上调查）。memory 由事故流运行器传入（每条流一个临时记忆索引）。
    mode=remediate（M3）：修复段用回放执行器（eval/replay_exec.py）；停在审批时以 sim:eval 批准后恢复。"""
    store = MemoryStore()
    obs = ObservationStore(Path(tempfile.mkdtemp(prefix="sentinel-eval-")), index=store)
    rem_store = MemoryRemediationStore()
    holder: dict[str, Any] = {}
    remediation = (
        replay_remediation(
            rem_store, load_packs()[pack].actions, lambda inv: holder["deps"].backend(load_packs()[pack], inv)
        )
        if mode == "remediate"
        else None
    )
    deps = make_deps(
        store,
        obs,
        Mode.REPLAY,
        REPO / inputs["fixture_ref"],
        version=lambda: version,
        memory=memory,
        remediation=remediation,
    )
    holder["deps"] = deps
    saver = InMemorySaver()  # investigate 和 resume 共用：恢复时要找回同一个线程的状态
    s = investigate(deps, inputs["trigger"]["target"], pack, saver, mode=mode)
    if "__interrupt__" in s and approve_all(rem_store):
        s = resume(deps, s["inv_id"], saver)
    return {
        "status": s.get("status"),
        "escalate_reason": s.get("escalate_reason"),
        "diagnosis": s.get("diagnosis"),
        "bundle": s.get("bundle"),
        "observations": [o.model_dump(mode="json") for o in obs.list(s["inv_id"])],
        # 事故流要用：模拟"人确认"后写记忆时，指纹从这些算出来（05 §4.2）
        "findings": s.get("findings", []),
        "fact_sheets": s.get("fact_sheets", []),
        "changes": s.get("changes", []),
        "inv_id": s["inv_id"],
        # 修复段（M3）：评分要用
        "proposal": s.get("proposal"),
        "unsafe_proposals": s.get("unsafe_proposals", 0),
        "remediation": s.get("remediation"),
    }


@dataclass(frozen=True)
class RunSummary:
    experiment: str
    result_file: Path
    scores: list[RunScore]


def run(
    pack: str, split: str, dataset_tag: str | None, reps: int, version_arg: str | None, mode: str = "report_only"
) -> RunSummary:
    tag = require_dataset_tag(dataset_tag)
    check_no_cache()  # 被测 Agent 不开缓存（08 §6.4；06 §4.4）
    version = checked_version(version_arg)
    model = load_models_config().purposes["diagnosis"].model
    pricing = load_pricing()
    client = Client()
    examples = list(client.list_examples(dataset_name=f"sentinel-{pack}", as_of=tag, splits=[split]))
    if not examples:
        raise EvalError(f"sentinel-{pack} 在 {tag} / {split} 下没有用例")

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        return run_one(inputs, version, mode=mode, pack=pack)

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
        experiment_prefix=f"v{version.seq}" + ("-remediate" if mode == "remediate" else ""),
        client=client,
    )
    # 本地也存一份 RunScore（统计从这里读）。rep 按同一用例出现的顺序编号
    seen: collections.Counter[str] = collections.Counter()
    scores = []
    runs = []
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
        runs.append(results.run_record(eid, seen[eid], run_.outputs))
        seen[eid] += 1
    # 修复模式单独存一份：和只报告模式的结果不是同一个实验，不能互相覆盖
    path = results.result_file(f"v{version.seq}" + ("__remediate" if mode == "remediate" else ""), tag, split)
    results.save(path, scores)
    results.save_runs(path, runs)
    return RunSummary(res.experiment_name, path, scores)
