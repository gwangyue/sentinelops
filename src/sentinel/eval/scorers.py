"""用例级评分（06 §3.1）：全部用确定性方法计算，不用 LLM。

负责：从一次调查的输出 + 用例的真实答案，算出 RunScore（03 §6.3）。
不负责：失败归类标签（M2 D9）；聚合和区间（stats/）。
M1 没有修复方案和记忆：unsafe_proposals 记 0，remediation_completed / memory_hit 记 None。
"""

from pathlib import Path
from typing import Any

import yaml

from sentinel.contracts import ContextBundle, Diagnosis, ExampleOutputs, Observation, RunScore
from sentinel.core.api import check_evidence, support_rate

PRICING = Path(__file__).resolve().parents[3] / "config" / "pricing.yaml"


def load_pricing() -> dict[str, dict[str, Any]]:
    return dict(yaml.safe_load(PRICING.read_text(encoding="utf-8"))["models"])


def cost_usd(model: str, tokens_in: int, tokens_out: int, pricing: dict[str, dict[str, Any]]) -> float:
    p = pricing[model]  # 没有单价就直接报错：成本不能悄悄记成 0
    return round(float(tokens_in * p["input"] + tokens_out * p["output"]) / 1_000_000, 6)


def score(
    *,
    out: dict[str, Any],
    truth: ExampleOutputs,
    example_id: str,
    rep: int,
    agent_version: str,
    dataset_tag: str,
    model: str,
    pricing: dict[str, dict[str, Any]],
) -> RunScore:
    """out 是评测 target 的返回值（runner.run_one）。"""
    d = Diagnosis.model_validate(out["diagnosis"]) if out.get("diagnosis") else None
    obs = [Observation.model_validate(o) for o in out.get("observations", [])]
    escalated = out.get("status") == "escalated"
    labels = [h.root_cause_label for h in d.hypotheses] if d else []

    if truth.expect_escalation:
        top1 = top3 = escalated
    else:
        # 06 §3.1：不该升级的场景，升级了就算错
        top1 = not escalated and bool(labels) and labels[0] == truth.root_cause_label
        top3 = not escalated and truth.root_cause_label in labels[:3]

    checks = check_evidence(d, obs) if d else []
    ok = {(c.hypothesis_id, c.claim_id) for c in checks if c.supported}
    by_id = {o.id: o for o in obs}
    # 06 §3.1：must_cite_tools 里的每个工具，至少有一条"通过检查"的证据来自它
    supported_tools = {
        by_id[e].tool
        for h in (d.hypotheses if d else [])
        for c in h.claims
        if (h.id, c.id) in ok
        for e in c.evidence
        if e in by_id
    }
    rendered = ContextBundle.model_validate(out["bundle"]).rendered if out.get("bundle") else ""
    recall = sum(1 for k in truth.key_facts if f" {k}=" in rendered) / len(truth.key_facts) if truth.key_facts else 1.0
    tin, tout = (d.tokens_in, d.tokens_out) if d else (0, 0)
    return RunScore(
        example_id=example_id,
        rep=rep,
        agent_version=agent_version,
        dataset_version=dataset_tag,
        top1_correct=top1,
        top3_correct=top3,
        escalated=escalated,
        escalation_appropriate=escalated == truth.expect_escalation,
        context_recall=recall,
        memory_hit=None,
        evidence_support_rate=support_rate(checks),
        cited_required_tools=all(t in supported_tools for t in truth.must_cite_tools),
        unsafe_proposals=0,
        remediation_completed=None,
        steps=d.steps if d else 0,
        tokens=tin + tout,
        cost_usd=cost_usd(model, tin, tout, pricing),
        replay_misses=sum(1 for o in obs if o.status == "replay_miss"),
        failure_tags=[],  # M2 D9：规则 + 评审模型
    )


FEEDBACK_KEYS = (
    "top1_correct",
    "top3_correct",
    "evidence_support_rate",
    "cited_required_tools",
    "context_recall",
    "escalated",
    "steps",
    "tokens",
    "cost_usd",
    "replay_misses",
)


def as_feedback(s: RunScore) -> list[dict[str, Any]]:
    """写进 LangSmith 的 feedback（06 §9"评分"）。"""
    return [{"key": k, "score": float(getattr(s, k))} for k in FEEDBACK_KEYS]
