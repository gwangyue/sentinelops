"""调查图（02 §1）：intake → detect → route → build_context → diagnose → report；
修复模式在 diagnose 和 report 之间加上 plan → approve → execute → verify。

负责：节点之间的流转、每个节点的审计事件、调查记录的状态。图本身是确定性的，只有 diagnose 和 plan 调用 LLM。
修复段（plan 之后）的逻辑在 remediate.py；只有 mode=remediate 并且 Deps 里有 remediation 时才会走到。
不负责：confirm 和写记忆（M3 D12）。
状态里只放 JSON（契约模型的 model_dump），这样 checkpointer 能序列化。
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypedDict, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from sentinel.contracts import (
    AgentVersion,
    ContextBundle,
    Diagnosis,
    FactSheet,
    Finding,
    RemediationPlan,
)
from sentinel.core.context.build import build_context
from sentinel.core.context.changes import Change, ReplaySnapshots, StoreSnapshots, collect_changes
from sentinel.core.context.knowledge_layer import KnowledgeRetriever, NoKnowledge
from sentinel.core.context.memory_layer import MemoryProvider, NoMemory
from sentinel.core.detect import run_detect
from sentinel.core.diagnosis.agent import BudgetExceeded, run_diagnosis
from sentinel.core.diagnosis.evidence import check_evidence, support_rate
from sentinel.core.graph.remediate import (
    NO_ACTION,
    Remediation,
    build_plan,
    check_approval,
    execution_keys,
    parse_proposal,
    plan_prompt_message,
    request_approval,
    run_verify,
    validate_proposal,
    wait_approval,
)
from sentinel.core.llm.chat import run_tags
from sentinel.core.observations.store import ObservationStore
from sentinel.core.packs_registry import LoadedPack
from sentinel.core.store import InvestigationRecord, Store
from sentinel.core.toolbackend import Mode, ToolBackend

ACTOR = "agent"
# 02 §2：同一个指纹 30 分钟内只调查一次，否则同一个问题每次巡检都会触发一次调查
DEDUP_WINDOW = timedelta(minutes=30)


class InvState(TypedDict, total=False):
    inv_id: str
    target: str
    pack: str
    mode: Literal["report_only", "remediate"]
    agent_version: str
    findings: list[dict[str, Any]]
    fact_sheets: list[dict[str, Any]]
    bundle: dict[str, Any]
    diagnosis: dict[str, Any] | None  # 预算中途耗尽时为 None
    status: str
    escalate_reason: str | None
    model_calls: int
    changes: list[dict[str, Any]]  # L3：和上一次快照比较得到的变更
    deduplicated: bool  # 本次的问题事件 30 分钟内都已经调查过
    # 修复段（M3）
    proposal: dict[str, Any] | None  # ActionProposal：LLM 的提议（不含任何安全字段）
    plan: dict[str, Any] | None  # RemediationPlan：确定性代码组装
    approval_id: str | None
    execution: dict[str, Any] | None  # ExecutionResult
    unsafe_proposals: int
    remediation: str | None  # 修复段的结局：resolved / no_action / unsafe / plan_failed / rejected / expired / ...


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
    knowledge: KnowledgeRetriever = field(default_factory=NoKnowledge)  # L4（04 §3.5）
    memory: MemoryProvider = field(default_factory=NoMemory)  # L5 接口；记忆服务在 D7–D8
    remediation: Remediation | None = None  # 修复段（M3）；没有时 mode=remediate 也只出报告
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
        changes = _changes(deps, pack, state["target"], deps.backend(pack, state["inv_id"]))
        seen = deps.store.recent_fingerprints(state["target"], datetime.now(UTC) - DEDUP_WINDOW, state["inv_id"])
        fresh = [f for f in res.findings if f.fingerprint not in seen]
        for f in res.findings:
            if f.fingerprint in seen:
                audit(state, "finding.deduplicated", {"finding": f.id, "fingerprint": f.fingerprint})
        # 只要有一个新问题就照常诊断（上下文里仍然带上全部问题事件）；全部重复才跳过诊断
        return {
            "findings": findings,
            "fact_sheets": sheets,
            "deduplicated": bool(res.findings) and not fresh,
            "changes": changes,
        }

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
        findings = [Finding.model_validate(f) for f in state["findings"]]
        sheets = [FactSheet.model_validate(s) for s in state["fact_sheets"]]
        layers = deps.policy.get("layers", {})
        # L4：按检测器过滤，用问题事件的说明做查询（04 §3.5）
        knowledge = (
            deps.knowledge.search(
                sorted({f.detector for f in findings}),
                " ".join(f.summary for f in findings),
                deps.policy["knowledge"]["top_k"],
            )
            if layers.get("L4")
            else []
        )
        changes = [Change(**{**c, "since": datetime.fromisoformat(c["since"])}) for c in state.get("changes", [])]
        memories = (
            deps.memory.similar(findings, sheets, changes, deps.policy["memory"]["top_k"]) if layers.get("L5") else []
        )
        bundle = build_context(
            state["inv_id"],
            findings,
            sheets,
            deps.load_env(state["target"]),
            deps.policy,
            changes=changes,
            knowledge=knowledge,
            memories=memories,
            raw=_raw_facts(deps, state["inv_id"], sheets) if deps.policy.get("L1", {}).get("format") == "raw" else None,
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
                compaction={**deps.policy["compaction"], "l2_max": deps.policy["budgets"]["L2_max"]},
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
                # L2 压缩记录（04 §3.11）：ContextBundle 只描述预组装的部分，压缩发生在诊断过程中，所以记在这里
                "compaction": {
                    "compacted": res.compaction.compacted,
                    "tokens_before": res.compaction.tokens_before,
                    "tokens_after": res.compaction.tokens_after,
                },
            },
        )
        reason = _escalation_reason(d.escalate, d.escalate_reason, [h.confidence for h in d.hypotheses], deps.policy)
        return {
            "diagnosis": d.model_dump(mode="json"),
            "escalate_reason": reason,
            "model_calls": state.get("model_calls", 0) + res.model_calls,
        }

    # ---------- 修复段（02 §1；remediate.py） ----------

    def plan(state: InvState) -> InvState:
        assert deps.remediation is not None
        pack = deps.packs[state["pack"]]
        if deps.remediation.frozen():
            # 07 §7：冻结之后照常诊断，但不进入方案环节
            audit(state, "plan.proposed", {"action": None, "reason": "frozen"})
            return {"remediation": "frozen"}
        d = Diagnosis.model_validate(state["diagnosis"])
        msg = deps.make_model("diagnosis").invoke(
            plan_prompt_message(pack.actions, d),
            cast(RunnableConfig, run_tags("plan", state["agent_version"], state["inv_id"])),
        )
        calls = state.get("model_calls", 0) + 1
        p = parse_proposal(str(msg.text))
        if p is not None and p.action_id == NO_ACTION:
            audit(state, "plan.proposed", {"action": None, "reason": "动作目录里没有合适的动作"})
            return {"model_calls": calls, "proposal": p.model_dump(mode="json"), "remediation": "no_action"}
        why = "模型的输出不是合法的 ActionProposal" if p is None else validate_proposal(p, pack.actions)
        if why is not None:
            # 03 §4.3：不在白名单、参数通不过校验 → 一次不安全提议，直接升级给人
            audit(state, "plan.rejected_unsafe", {"reason": why, "proposal": p.model_dump(mode="json") if p else None})
            return {
                "model_calls": calls,
                "proposal": p.model_dump(mode="json") if p else None,
                "unsafe_proposals": state.get("unsafe_proposals", 0) + 1,
                "remediation": "unsafe",
                "escalate_reason": f"unsafe_proposal: {why}",
            }
        assert p is not None
        built, reason = build_plan(
            state["inv_id"], state["target"], p, pack.actions[p.action_id], deps.backend(pack, state["inv_id"])
        )
        if built is None:
            audit(state, "plan.proposed", {"action": p.action_id, "params": p.params, "rejected": reason})
            return {
                "model_calls": calls,
                "proposal": p.model_dump(mode="json"),
                "remediation": "plan_failed",
                "escalate_reason": f"plan: {reason}",
            }
        req = request_approval(deps.remediation.store, built, deps.remediation.approval_ttl)
        audit(
            state,
            "plan.proposed",
            {
                "plan_id": built.id,
                "action": built.action_id,
                "params": built.params,
                "plan_hash": built.plan_hash,
                "dry_run": built.dry_run_result,
            },
        )
        audit(
            state,
            "approval.requested",
            {"approval_id": req.id, "plan_hash": req.plan_hash, "expires_at": req.expires_at.isoformat()},
        )
        deps.store.update_investigation(state["inv_id"], status="awaiting_approval", diagnosis=state["diagnosis"])
        return {
            "model_calls": calls,
            "proposal": p.model_dump(mode="json"),
            "plan": built.model_dump(mode="json"),
            "approval_id": req.id,
        }

    def approve(state: InvState) -> InvState:
        # 只有 interrupt（02 §3.4）：恢复时带回来的值不用，审批结果在 execute 节点里从存储读
        wait_approval(str(state["approval_id"]))
        return {}

    def execute(state: InvState) -> InvState:
        assert deps.remediation is not None
        plan = RemediationPlan.model_validate(state["plan"])
        ok, event, _ = check_approval(deps.remediation.store, str(state["approval_id"]), datetime.now(UTC))
        if not ok:
            if event == "approval.expired":
                audit(state, "approval.expired", {"approval_id": state["approval_id"]})
            outcome = "expired" if event == "approval.expired" else "rejected"
            return {"remediation": outcome}
        r = deps.remediation.executor("execute", plan.id, str(state["approval_id"]), execution_keys(plan, "execute"))
        if r.status in ("succeeded", "duplicate"):
            return {"execution": r.model_dump(mode="json")}
        not_recorded = "PostNotRecorded" in str(r.output.get("error", ""))
        return {
            "execution": r.model_dump(mode="json"),
            "remediation": "not_recorded" if not_recorded else f"exec_{r.status}",
            "escalate_reason": None if not_recorded else f"execution: {r.status} {r.output}",
        }

    def verify(state: InvState) -> InvState:
        assert deps.remediation is not None
        pack = deps.packs[state["pack"]]
        plan = RemediationPlan.model_validate(state["plan"])
        spec = pack.actions[plan.action_id]
        passed, detail = run_verify(spec, plan, deps.backend(pack, state["inv_id"]), deps.remediation.verify_once)
        audit(state, "verify.passed" if passed else "verify.failed", {"plan_id": plan.id, "detail": detail})
        if passed:
            return {"remediation": "resolved"}
        # 验证失败就回滚（02 §3.4"不让 LLM 临场想办法补救"），然后升级给人
        rb = deps.remediation.executor("rollback", plan.id, str(state["approval_id"]), execution_keys(plan, "rollback"))
        return {
            "remediation": "rolled_back" if rb.rolled_back else "rollback_failed",
            "escalate_reason": f"verify_failed: {detail}",
        }

    def report(state: InvState) -> InvState:
        escalated = state.get("escalate_reason") is not None
        status = "escalated" if escalated else "resolved" if state.get("remediation") == "resolved" else "diagnosed"
        deps.store.update_investigation(
            state["inv_id"], status=status, diagnosis=state["diagnosis"], escalate_reason=state.get("escalate_reason")
        )
        if escalated:
            audit(state, "investigation.escalated", {"reason": state["escalate_reason"]})
        audit(state, "investigation.closed", {"outcome": status, "remediation": state.get("remediation")})
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
        ("plan", plan),
        ("approve", approve),
        ("execute", execute),
        ("verify", verify),
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
    # 修复模式、有修复段依赖、诊断没有升级时才进入 plan；巡检（只报告）诊断之后直接出报告（02 §2）
    g.add_conditional_edges(
        "diagnose",
        lambda s: (
            "plan"
            if s["mode"] == "remediate"
            and deps.remediation is not None
            and s.get("diagnosis")
            and not s.get("escalate_reason")
            else "report"
        ),
        ["plan", "report"],
    )
    g.add_conditional_edges("plan", lambda s: "approve" if s.get("approval_id") else "report", ["approve", "report"])
    g.add_edge("approve", "execute")
    g.add_conditional_edges(
        "execute",
        lambda s: "verify" if s.get("execution") and not s.get("remediation") else "report",
        ["verify", "report"],
    )
    g.add_edge("verify", "report")
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


def _raw_facts(deps: Deps, inv_id: str, sheets: list[FactSheet]) -> dict[str, tuple[str, Any]]:
    """E1（04 §5）：事实卡片引用的那些观测，取出原始输出，按出现顺序去重。"""
    out: dict[str, tuple[str, Any]] = {}
    for x in (f for s in sheets for f in s.facts):
        if x.observation_id not in out:
            obs, raw = deps.obs_store.get(inv_id, x.observation_id)
            out[x.observation_id] = (obs.tool, raw)
    return out


def _changes(deps: Deps, pack: LoadedPack, target: str, backend: ToolBackend) -> list[dict[str, Any]]:
    """L3（04 §3.4）：读快照工具的当前值，和上一次快照比较；线上巡检时把这次的快照存起来，给下一次用。

    回放时，上一次快照来自录制里 phase=before 的记录，并且不写快照（评测不能改变存储的状态）。
    """
    if not deps.policy.get("layers", {}).get("L3"):
        return []
    now = datetime.now(UTC)
    current = {}
    for tool in deps.policy["changes"]["snapshot_tools"].get(pack.manifest.pack, []):
        r = backend.call(tool)
        if r.ok:
            current[tool] = (r.raw, r.observation_id)
    replay = backend.mode is Mode.REPLAY
    source = ReplaySnapshots(backend.replay_before) if replay else StoreSnapshots(deps.store, target)
    # 回放时"现在"取录制的时间之后：快照和故障状态只差几分钟，不能用今天的时间去算 24 小时窗口
    changes = collect_changes(current, source, now, deps.policy["changes"]["window_hours"] if not replay else 1e9)
    if not replay:
        for tool, (raw, _) in current.items():
            deps.store.save_snapshot(target, tool, raw, now)
    return [{**c.__dict__, "since": c.since.isoformat()} for c in changes]
