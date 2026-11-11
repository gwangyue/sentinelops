"""调查图的修复段（02 §1、§3.4；03 §4.3–4.4；08 M3 D10–D11）：plan → approve → execute → verify。

负责：
- plan：LLM 只输出 ActionProposal；确定性代码校验白名单和 params_schema（不通过 = 不安全提议，升级给人），
  再做前置检查、预演、影响范围检查，算 plan_hash，写方案和审批请求（写入放在 plan 节点：02 §3.4）；
- approve：只有 interrupt()。恢复时只拿审批 ID，决定和有效期都从存储里读；
- execute：把 (plan_id, approval_id, 幂等键) 交给执行器（线上是独立进程，评测是回放模拟器），等结果；
- verify：按动作的 VerifySpec 轮询只读工具；失败就请求执行器回滚，然后升级给人。
不负责：执行本身（exec/）、审批的写入（CLI approvals）、根因确认和写记忆（confirm 节点，M3 D12）。
安全规则（CLAUDE.md）：plan_hash、审批 ID、幂等键都由这里的确定性代码生成，不从 LLM 的输出里读。
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import jsonschema
from langchain_core.messages import HumanMessage
from langgraph.types import interrupt
from ulid import ULID

from sentinel.contracts import (
    ActionProposal,
    ActionSpec,
    ApprovalDecision,
    ApprovalRequest,
    Diagnosis,
    ExecutionResult,
    RemediationPlan,
)
from sentinel.governance.plan_hash import idempotency_key, plan_hash
from sentinel.governance.remediation_store import ExecKind, RemediationStore
from sentinel.sdk import ActionContext, ToolCaller, resolve_entry

PLAN_PROMPT = Path(__file__).resolve().parents[1] / "diagnosis" / "prompts" / "plan.md"
APPROVAL_TTL = timedelta(minutes=30)  # 07 §4：low / medium 动作的审批有效期
NO_ACTION = "none"

# 执行器的接口：(kind, plan_id, approval_id, 幂等键) → 执行结果。线上写队列并等独立进程，评测直接调回放模拟器
Executor = Callable[[ExecKind, str, str, str], ExecutionResult]


@dataclass
class Remediation:
    """修复段的依赖。没有它（只报告模式、M1/M2 的评测）时，修复段不会被接进图里。"""

    store: RemediationStore
    executor: Executor
    approval_ttl: timedelta = APPROVAL_TTL
    # 回放评测：录制的状态不会随时间变化，只检查一次（轮询只会空转到超时，M3 D11 测试时踩过）
    verify_once: bool = False
    frozen: Callable[[], bool] = lambda: False  # 紧急停止（07 §7）：冻结时 plan 节点不出方案


def catalog_text(specs: dict[str, ActionSpec]) -> str:
    return "\n".join(
        f"- `{a.id}`（风险 {a.risk}）参数 schema：{json.dumps(a.params_schema, ensure_ascii=False)}"
        for a in specs.values()
    )


def render_plan_prompt(specs: dict[str, ActionSpec], d: Diagnosis, domain: str) -> str:
    h1 = d.hypotheses[0]
    diag = json.dumps(
        {
            "H1": {"root_cause": h1.root_cause, "root_cause_label": h1.root_cause_label, "confidence": h1.confidence},
            "claims": [c.text for c in h1.claims],
        },
        ensure_ascii=False,
    )
    return PLAN_PROMPT.read_text(encoding="utf-8").format(domain=domain, catalog=catalog_text(specs), diagnosis=diag)


def parse_proposal(text: str) -> ActionProposal | None:
    """模型输出 → ActionProposal；不是合法 JSON、字段不对都返回 None（由调用方记为不安全提议）。"""
    try:
        return ActionProposal.model_validate_json(text[text.index("{") : text.rindex("}") + 1])
    except ValueError:
        return None


def validate_proposal(p: ActionProposal, specs: dict[str, ActionSpec]) -> str | None:
    """白名单 + params_schema（03 §4.3）。返回拒绝原因，None 表示通过。"""
    spec = specs.get(p.action_id)
    if spec is None:
        return f"动作 {p.action_id} 不在动作目录里"
    try:
        jsonschema.validate(p.params, spec.params_schema)
    except jsonschema.ValidationError as e:
        return f"参数通不过 params_schema：{e.message}"
    return None


def over_blast_radius(dry_run: dict[str, Any], limit: dict[str, int] | None) -> str | None:
    for k, cap in (limit or {}).items():
        if int(dry_run.get(k, 0)) > cap:
            return f"预演的 {k}={dry_run[k]} 超过影响范围上限 {cap}（07 §3 T9）"
    return None


def build_plan(
    inv_id: str, target: str, p: ActionProposal, spec: ActionSpec, tools: ToolCaller
) -> tuple[RemediationPlan | None, str]:
    """前置检查 + 预演 + 影响范围，全部由确定性代码完成；任何一步不通过返回 (None, 原因)。"""
    ctx = ActionContext(target, p.params, tools)
    snapshot: dict[str, Any] = {}
    for entry in spec.preconditions:
        chk = resolve_entry(entry)(ctx)
        if not chk.ok:
            return None, f"前置检查没有通过：{chk.reason}"
        snapshot.update(chk.snapshot)
    dry = dict(resolve_entry(spec.dry_run)(ctx))
    if why := over_blast_radius(dry, spec.max_blast_radius):
        return None, why
    h = plan_hash(spec.id, target, p.params, snapshot)
    plan = RemediationPlan(
        id=f"pln_{ULID()}",
        investigation_id=inv_id,
        action_id=spec.id,
        target=target,
        params=dict(p.params),
        rationale_hypothesis=p.rationale_hypothesis,
        precheck_snapshot=snapshot,
        dry_run_result=dry,
        rollback_plan=f"{spec.rollback}（按前置检查快照恢复原值）" if spec.rollback else None,
        plan_hash=h,
    )
    return plan, "ok"


def request_approval(store: RemediationStore, plan: RemediationPlan, ttl: timedelta) -> ApprovalRequest:
    now = datetime.now(UTC)
    req = ApprovalRequest(
        id=f"apv_{ULID()}", plan_id=plan.id, plan_hash=plan.plan_hash, requested_at=now, expires_at=now + ttl
    )
    store.put_plan(plan)
    store.put_approval_request(req)
    return req


def wait_approval(approval_id: str) -> Any:
    """approve 节点的全部内容（02 §3.4"审批节点里没有副作用"）：恢复时整个节点会从第一行重新执行。
    这里不能包 try/except：interrupt 靠抛异常暂停图（lint 规则 tools/checks/interrupt_lint.py）。"""
    return interrupt({"approval_id": approval_id})


def check_approval(store: RemediationStore, approval_id: str, now: datetime) -> tuple[bool, str, str]:
    """返回 (是否可以执行, 审计事件类型, 说明)。只信存储里的记录，不信 resume 带来的任何内容。"""
    req = store.get_approval_request(approval_id)
    d = store.get_decision(approval_id)
    if req is None or d is None:
        return False, "approval.decided", "没有找到审批决定"
    if d.decision != "approved":
        return False, "approval.decided", f"审批被拒绝：{d.reason or '没有说明'}（{d.approver}）"
    if now > req.expires_at:
        return False, "approval.expired", f"审批在 {req.expires_at.isoformat()} 过期"
    return True, "approval.decided", f"已批准（{d.approver}）"


def run_verify(spec: ActionSpec, plan: RemediationPlan, tools: Any, once: bool = False) -> tuple[bool, str]:
    """按 VerifySpec 轮询（02 §1 verify 节点）。每次轮询前清掉调用缓存，读的是最新状态。"""
    pred = resolve_entry(spec.verify.predicate)
    deadline = time.monotonic() + spec.verify.timeout_s
    detail = ""
    while True:
        tools.refresh()
        r = pred(ActionContext(plan.target, plan.params, tools))
        detail = r.detail
        if r.passed:
            return True, detail
        if once or time.monotonic() >= deadline:
            return False, detail
        time.sleep(spec.verify.poll_interval_s)


def execution_keys(plan: RemediationPlan, kind: ExecKind) -> str:
    h = plan.plan_hash if kind == "execute" else plan.plan_hash + "#rollback"
    return idempotency_key(plan.investigation_id, h)


def plan_prompt_message(specs: dict[str, ActionSpec], d: Diagnosis, domain: str) -> list[HumanMessage]:
    return [HumanMessage(content=render_plan_prompt(specs, d, domain))]


def record_decision(
    rem: RemediationStore,
    audit: Callable[..., Any],
    approval_id: str,
    decision: Literal["approved", "rejected"],
    approver: str,
    reason: str | None,
) -> RemediationPlan:
    """写入审批决定和审计 approval.decided（02 §3.2"API 写入审批记录"）。CLI 和测试都用这一个函数。
    重复决定由存储的唯一约束挡住（DuplicateError）。返回被审批的方案（调用方用它找到要恢复的调查）。"""
    req = rem.get_approval_request(approval_id)
    plan = rem.get_plan(req.plan_id) if req else None
    if req is None or plan is None:
        raise KeyError(f"没有审批请求 {approval_id}")
    rem.put_decision(
        ApprovalDecision(
            approval_id=approval_id,
            decision=decision,
            approver=approver,
            reason=reason,
            decided_at=datetime.now(UTC),
        )
    )
    # 07 §4：审批人不能审批自己发起的调查——第一版不强制，只记进审计，周报里提示
    audit(
        investigation_id=plan.investigation_id,
        agent_version=None,
        actor=approver,
        type="approval.decided",
        payload={"approval_id": approval_id, "decision": decision, "plan_hash": req.plan_hash, "reason": reason},
    )
    return plan
