"""笨基线（06 §4.5）：多数类、纯规则。都不用 LLM，结果是确定的，所以只跑 1 次。

负责：给出基线的 RunScore，用来和 Agent 版本做配对比较。基线只比较主指标 top1；
      其余字段对基线没有意义（没有论断、没有 token），记为 0 / False。
不负责："上下文完美"基线（需要跑模型，M2 D9）。
"""

import collections
import tempfile
from pathlib import Path
from typing import Any

import yaml
from langsmith import Client

from sentinel.contracts import EvalExample, ExampleMetadata, RunScore
from sentinel.core.detect import load_env, run_detect
from sentinel.core.observations.store import ObservationStore
from sentinel.core.packs_registry import load_pack
from sentinel.core.toolbackend import Mode, ToolBackend
from sentinel.eval import results
from sentinel.eval.dataset import DATASETS, REPO
from sentinel.eval.guards import require_dataset_tag


def _score(ex: EvalExample, label: str, tag: str, name: str, replay_misses: int = 0) -> RunScore:
    hit = label == ex.outputs.root_cause_label
    return RunScore(
        example_id=ex.id,
        rep=0,
        agent_version=f"baseline:{name}",
        dataset_version=tag,
        top1_correct=hit,
        top3_correct=hit,
        escalated=False,
        escalation_appropriate=not ex.outputs.expect_escalation,
        context_recall=0.0,
        memory_hit=None,
        evidence_support_rate=0.0,
        cited_required_tools=False,
        unsafe_proposals=0,
        remediation_completed=None,
        steps=0,
        tokens=0,
        cost_usd=0.0,
        replay_misses=replay_misses,
        failure_tags=[],
    )


def majority(examples: list[EvalExample], tag: str) -> list[RunScore]:
    """永远回答这批用例里最常见的根因标签（同票时按标签名排序，保证确定）。"""
    counts = collections.Counter(e.outputs.root_cause_label for e in examples)
    label = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return [_score(e, label, tag, "majority") for e in examples]


def rules_label(detectors: set[str], rules: dict[str, Any]) -> str:
    """按顺序匹配：第一条 when_all 全部包含在本次触发的检测器里的规则给出答案。"""
    for r in rules["rules"]:
        if set(r["when_all"]) <= detectors:
            return str(r["label"])
    return str(rules["default"])


def rules(examples: list[EvalExample], tag: str, pack: str) -> list[RunScore]:
    """在回放上只跑检测器（不调用 LLM），按人写的映射表 datasets/baselines/<pack>_rules.yaml 给出根因。"""
    table = yaml.safe_load((DATASETS / "baselines" / f"{pack}_rules.yaml").read_text(encoding="utf-8"))
    p = load_pack(pack)
    out = []
    for e in examples:
        obs = ObservationStore(Path(tempfile.mkdtemp(prefix="sentinel-baseline-")))
        tb = ToolBackend(p.tools, obs, f"inv_{e.id}", Mode.REPLAY, fixture=REPO / e.inputs.fixture_ref)
        res = run_detect(p, e.inputs.trigger["target"], tb, load_env(e.inputs.trigger["target"]))
        misses = sum(1 for o in obs.list(f"inv_{e.id}") if o.status == "replay_miss")
        out.append(_score(e, rules_label({f.detector for f in res.findings}, table), tag, "rules", misses))
    return out


def fetch_examples(pack: str, tag: str, split: str) -> list[EvalExample]:
    """和评测运行器一样，从 LangSmith 按标签和 split 拉用例，保证基线和 Agent 面对的是同一批用例。"""
    c = Client()
    rows = c.list_examples(dataset_name=f"sentinel-{pack}", as_of=tag, splits=[split])
    out = []
    for r in rows:
        inputs, md = r.inputs or {}, r.metadata or {}
        out.append(
            EvalExample.model_validate(
                {
                    "id": inputs["example_id"],
                    "scenario_id": inputs["scenario_id"],
                    "split": split,
                    "inputs": {k: inputs[k] for k in ("trigger", "fixture_ref", "memory_snapshot")},
                    "outputs": r.outputs or {},
                    # LangSmith 会在 metadata 里加自己的键（例如 dataset_split），只取契约里有的字段
                    "metadata": {k: md.get(k) for k in ExampleMetadata.model_fields if k in md},
                }
            )
        )
    return sorted(out, key=lambda e: e.id)


def run(pack: str, split: str, dataset_tag: str | None) -> dict[str, Path]:
    tag = require_dataset_tag(dataset_tag)
    examples = fetch_examples(pack, tag, split)
    paths = {}
    for name, scores in (("majority", majority(examples, tag)), ("rules", rules(examples, tag, pack))):
        path = results.result_file(f"baseline-{name}", tag, split)
        results.save(path, scores)
        paths[name] = path
    return paths
