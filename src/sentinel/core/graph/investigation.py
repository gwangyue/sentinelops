"""调查图的只读路径（02 §1；08 M1 D3）：intake → detect → route → build_context → diagnose → report。

负责：节点之间的流转、每个节点的审计事件、调查记录的状态。图本身是确定性的，只有 diagnose 调用 LLM。
不负责：plan / approve / execute / verify / confirm（M3）。D3 的图里没有 interrupt()。
状态里只放 JSON（契约模型的 model_dump），这样 checkpointer 能序列化。
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from sentinel.contracts import AgentVersion, ContextBundle, FactSheet, Finding
from sentinel.core.context.build import build_context
from sentinel.core.detect import run_detect
from sentinel.core.diagnosis.agent import BudgetExceeded, run_diagnosis
from sentinel.core.diagnosis.evidence import check_evidence, support_rate
from sentinel.core.llm.chat import run_tags
from sentinel.core.observations.store import ObservationStore
from sentinel.core.packs_registry import LoadedPack
from sentinel.core.store import InvestigationRecord, Store
from sentinel.core.toolbackend import ToolBackend

ACTOR = "agent"
# 02 §2：同一个指纹 30 分钟内只调查一次，否则同一个问题每次巡检都会触发一次调查
DEDUP_WINDOW = timedelta(minutes=30)


class InvState(TypedDict, total=False):
    inv_id: str
    target: str
    pack: str
    mode: Literal["report_only"]
    agent_version: str
    findings: list[dict[str, Any]]
    fact_sheets: list[dict[str, Any]]
    bundle: dict[str, Any]
    diagnosis: dict[str, Any] | None  # 预算中途耗尽时为 None
    status: str
    escalate_reason: str | None
    model_calls: int
    deduplicated: bool  # 本次的问题事件 30 分钟内都已经调查过


@dataclass
class Deps:
    """图运行时需要的外部依赖。CLI、API 和测试各自组装一份（测试用内存存储、回放和假模型）。"""

    store: Store
    obs_store: ObservationStore
    packs: dict[str, LoadedPack]
    make_backend: Callable[[LoadedPack, str], ToolBackend]
    make_model: Callable[[str], BaseChatModel]
    current_version: Callable[[], AgentVersion]  # 当前行为对应的已冻结版本，没冻结就抛 FreezeError
    load_env: Callable[[str], dict[str, Any]]
    policy: dict[str, Any]
    _backends: dict[str, ToolBackend] = field(default_factory=dict)

    def backend(self, pack: LoadedPack, inv_id: str) -> ToolBackend:
        # detect 和 diagnose 用同一个后端：同一次调查里同一个调用只执行一次，回放的顺序也保持一致
        key = f"{inv_id}/{pack.manifest.pack}"
        if key not in self._backends:
            self._backends[key] = self.make_backend(pack, inv_id)
        return self._backends[key]


def build_graph(deps: Deps, checkpointer: BaseCheckpointSaver[Any] | None = None) -> CompiledStateGraph:  # type: ignore[type-arg]
    def audit(state: InvState, type_: str, payload: dict[str, Any]) -> None:
        deps.store.append_audit(
            investigation_id=state["inv_id"],
            agent_version=state.get("agent_version"),
            actor=ACTOR,
            type=type_,
            payload=payload,
        )

    def intake(state: InvState) -> InvState:
        v = deps.current_version()
        deps.store.create_investigation(
            InvestigationRecord(
                id=state["inv_id"],
                target=state["target"],
                mode=state["mode"],
                agent_version=v.hash,
                status="running",
                created_at=datetime.now(UTC),
            )
        )
        s: InvState = {**state, "agent_version": v.hash, "model_calls": 0}
        audit(s, "investigation.started", {"target": state["target"], "mode": state["mode"], "version": f"v{v.seq}"})
        return s

    def detect(state: InvState) -> InvState:
        pack = deps.packs[state["pack"]]
        res = run_detect(pack, state["target"], deps.backend(pack, state["inv_id"]), deps.load_env(state["target"]))
        for f in res.findings:
            audit(
                state,
                "finding.detected",
                {"finding": f.id, "detector": f.detector, "severity": f.severity, "fingerprint": f.fingerprint},
            )
        findings = [f.model_dump(mode="json") for f in res.findings]
        sheets = [s.model_dump(mode="json") for s in res.fact_sheets]
        deps.store.update_investigation(state["inv_id"], findings=findings, fact_sheets=sheets)
        seen = deps.store.recent_fingerprints(state["target"], datetime.now(UTC) - DEDUP_WINDOW, state["inv_id"])
        fresh = [f for f in res.findings if f.fingerprint not in seen]
        for f in res.findings:
            if f.fingerprint in seen:
                audit(state, "finding.deduplicated", {"finding": f.id, "fingerprint": f.fingerprint})
        # 只要有一个新问题就照常诊断（上下文里仍然带上全部问题事件）；全部重复才跳过诊断
        return {"findings": findings, "fact_sheets": sheets, "deduplicated": bool(res.findings) and not fresh}

    def deduplicated(state: InvState) -> InvState:
        deps.store.update_investigation(state["inv_id"], status="deduplicated")
        audit(state, "investigation.closed", {"outcome": "deduplicated"})
        return {"status": "deduplicated"}

    def healthy(state: InvState) -> InvState:
        # 快速路径：没有问题事件就结束，整个流程不调用 LLM（02 §2）
        deps.store.update_investigation(state["inv_id"], status="healthy")
        audit(state, "patrol.healthy", {"detectors": len(deps.packs[state["pack"]].detectors)})
        audit(state, "investigation.closed", {"outcome": "healthy"})
        return {"status": "healthy"}

    def route(state: InvState) -> InvState:
        # 按问题事件的领域选专家包。M1 只有 es；多个领域同时出问题时的拆分是 TODO(edge)
        packs = {f["pack"] for f in state["findings"]}
        assert packs <= set(deps.packs), f"没有注册的专家包：{packs - set(deps.packs)}"
        return {}

    def context(state: InvState) -> InvState:
        bundle = build_context(
            state["inv_id"],
            [Finding.model_validate(f) for f in state["findings"]],
            [FactSheet.model_validate(s) for s in state["fact_sheets"]],
            deps.load_env(state["target"]),
            deps.policy,
        )
        deps.store.update_investigation(state["inv_id"], context_bundle=bundle.model_dump(mode="json"))
        return {"bundle": bundle.model_dump(mode="json")}

    def diagnose(state: InvState) -> InvState:
        pack = deps.packs[state["pack"]]
        bundle = ContextBundle.model_validate(state["bundle"])
        budget = deps.policy["investigation_token_budget"]
        try:
            res = run_diagnosis(
                model=deps.make_model("diagnosis"),
                pack=pack,
                backend=deps.backend(pack, state["inv_id"]),
                obs_store=deps.obs_store,
                bundle=bundle,
                run_config=run_tags("diagnosis", state["agent_version"], state["inv_id"]),
                token_budget=budget,
            )
        except BudgetExceeded as e:
            # 中途中止：没有诊断结果，直接升级给人（02 §1；07 §3 T8）
            audit(state, "budget.exceeded", {"tokens": e.used, "budget": e.budget})
            return {"diagnosis": None, "escalate_reason": f"budget: {e}"}
        d = res.diagnosis
        rate = support_rate(check_evidence(d, deps.obs_store.list(state["inv_id"])))
        audit(
            state,
            "diagnosis.completed",
            {
                "hypotheses": [
                    {"id": h.id, "label": h.root_cause_label, "confidence": h.confidence} for h in d.hypotheses
                ],
                "evidence_support_rate": round(rate, 3),
                "steps": d.steps,
                "tokens_in": d.tokens_in,
                "tokens_out": d.tokens_out,
            },
        )
        reason = _escalation_reason(d.escalate, d.escalate_reason, [h.confidence for h in d.hypotheses], deps.policy)
        return {
            "diagnosis": d.model_dump(mode="json"),
            "escalate_reason": reason,
            "model_calls": state.get("model_calls", 0) + res.model_calls,
        }

    def report(state: InvState) -> InvState:
        escalated = state.get("escalate_reason") is not None
        status = "escalated" if escalated else "diagnosed"
        deps.store.update_investigation(
            state["inv_id"], status=status, diagnosis=state["diagnosis"], escalate_reason=state.get("escalate_reason")
        )
        if escalated:
            audit(state, "investigation.escalated", {"reason": state["escalate_reason"]})
        audit(state, "investigation.closed", {"outcome": status})
        return {"status": status}

    g = StateGraph(InvState)
    for name, fn in [
        ("intake", intake),
        ("detect", detect),
        ("healthy", healthy),
        ("deduplicated", deduplicated),
        ("route", route),
        ("build_context", context),
        ("diagnose", diagnose),
        ("report", report),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_edge("intake", "detect")
    g.add_conditional_edges(
        "detect",
        lambda s: "healthy" if not s["findings"] else "deduplicated" if s.get("deduplicated") else "route",
        ["route", "healthy", "deduplicated"],
    )
    g.add_edge("healthy", END)
    g.add_edge("deduplicated", END)
    g.add_edge("route", "build_context")
    g.add_edge("build_context", "diagnose")
    g.add_edge("diagnose", "report")  # 只报告模式：诊断之后直接出报告；plan 节点在 M3
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)


def _escalation_reason(escalate: bool, why: str | None, confidences: list[float], policy: dict[str, Any]) -> str | None:
    """02 §1"置信度低 → 升级给人"。返回 None 表示不升级。token 预算在诊断过程中检查（agent.TokenBudget）。"""
    if escalate:
        return f"model: {why or '模型认为证据不足'}"
    if not confidences:
        return "no_hypothesis: 没有给出任何假设"
    if confidences[0] < policy["escalation"]["min_confidence"]:
        return f"low_confidence: H1 置信度 {confidences[0]:.2f} 低于 {policy['escalation']['min_confidence']}"
    return None
