"""执行器的核心流程（03 §4.4；02 §3.4；ADR-005）：真实执行器和回放模拟器共用同一段校验。

负责：拿到 (plan_id, approval_id, idempotency_key) 之后，按顺序做——
  0. 系统冻结（07 §7）就拒绝；
  1. 从存储读出方案和审批请求，重算 plan_hash，三处（方案、审批请求、重算值）必须一致；
  2. 审批的决定是 approved，并且执行时没有过期（回滚不检查过期：回滚是恢复原状）；
  3. 幂等键必须等于 hash(调查 ID, plan_hash)；已经有结果就直接返回 duplicate，不再执行；
  4. 重新做一次前置检查（防止"审批时满足、执行时已经不满足"）；回滚不重做；
  5. 执行（真实执行器调用动作的 execute；回放模拟器切到录制的 post 阶段），写结果和审计。
不负责：怎么拿到写连接、怎么轮询队列（__main__.py）；回放时怎么切阶段（调用方传入 run_action）。
不依赖 core：只信任存储里的记录，调查引擎传来的其他东西一概不用。
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from sentinel.contracts import ActionSpec, ExecutionResult, RemediationPlan
from sentinel.governance.plan_hash import idempotency_key, plan_hash
from sentinel.governance.remediation_store import ExecRequest, RemediationStore
from sentinel.sdk import ActionContext, ExecContext, ToolCaller, resolve_entry

ACTOR = "executor"
ExecStatus = Literal["succeeded", "failed", "precheck_failed", "duplicate"]
# 真实执行器：调用动作函数；回放模拟器：切阶段、不写任何东西
RunAction = Callable[[ActionSpec, RemediationPlan, str], dict[str, Any]]


def rollback_key(investigation_id: str, plan_hash_: str) -> str:
    # 回滚是另一次执行，有自己的幂等键：同一个方案的执行和回滚各只能发生一次
    return idempotency_key(investigation_id, plan_hash_ + "#rollback")


def _result(req: ExecRequest, status: ExecStatus, output: dict[str, Any], rolled_back: bool = False) -> ExecutionResult:
    return ExecutionResult(
        plan_id=req.plan_id,
        approval_id=req.approval_id,
        idempotency_key=req.idempotency_key,
        status=status,
        output=output,
        verify_passed=None,  # 验证在调查图的 verify 节点里做
        rolled_back=rolled_back,
    )


def process(
    req: ExecRequest,
    store: RemediationStore,
    specs: dict[str, ActionSpec],
    tools: ToolCaller,
    run_action: RunAction,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    frozen: Callable[[], bool] = lambda: False,
) -> ExecutionResult:
    plan = store.get_plan(req.plan_id)
    apv = store.get_approval_request(req.approval_id)
    inv = plan.investigation_id if plan else None

    def refuse(event: str, stage: str, reason: str) -> ExecutionResult:
        r = _result(req, "precheck_failed", {"stage": stage, "reason": reason})
        store.append_audit(investigation_id=inv, actor=ACTOR, type=event, payload={"plan_id": req.plan_id, **r.output})
        return r

    # 0. 紧急停止（07 §7）：每次执行前都读冻结状态，冻结时什么都不做（回滚也不做：冻结就是要人接手）
    if frozen():
        return refuse("action.precheck_failed", "frozen", "系统已冻结（sentinel freeze），拒绝所有动作")
    # 1. 方案和审批请求都在，并且哈希三处一致
    if plan is None or apv is None or apv.plan_id != plan.id:
        return refuse("action.precheck_failed", "approval", "方案或审批请求不存在，或者两者不匹配")
    recomputed = plan_hash(plan.action_id, plan.target, plan.params, plan.precheck_snapshot)
    if not recomputed == plan.plan_hash == apv.plan_hash:
        return refuse("action.precheck_failed", "approval", "plan_hash 不一致：方案在审批之后被改过")
    # 2. 审批决定
    d = store.get_decision(apv.id)
    if d is None or d.decision != "approved":
        return refuse("action.precheck_failed", "approval", f"审批没有通过（{d.decision if d else '没有决定'}）")
    if req.kind == "execute" and now() > apv.expires_at:
        return refuse("approval.expired", "approval", f"审批已在 {apv.expires_at.isoformat()} 过期")
    # 3. 幂等
    expected = (
        idempotency_key(plan.investigation_id, plan.plan_hash)
        if req.kind == "execute"
        else rollback_key(plan.investigation_id, plan.plan_hash)
    )
    if req.idempotency_key != expected:
        return refuse("action.precheck_failed", "approval", "幂等键和方案不匹配")
    prev = store.get_execution(req.idempotency_key)
    if prev is not None:
        return prev.model_copy(update={"status": "duplicate"})
    spec = specs.get(plan.action_id)
    if spec is None:
        return refuse("action.precheck_failed", "approval", f"动作 {plan.action_id} 不在动作目录里")
    # 4. 执行前重新做前置检查（TOCTOU）
    if req.kind == "execute":
        ctx = ActionContext(plan.target, plan.params, tools)
        for entry in spec.preconditions:
            chk = resolve_entry(entry)(ctx)
            if not chk.ok:
                r = _result(req, "precheck_failed", {"stage": "precondition", "reason": chk.reason})
                store.put_execution(r)
                store.append_audit(
                    investigation_id=inv,
                    actor=ACTOR,
                    type="action.precheck_failed",
                    payload={"plan_id": plan.id, **r.output},
                )
                return r
    # 5. 执行或回滚
    try:
        out = run_action(spec, plan, req.kind)
        status: ExecStatus = "succeeded"
    except Exception as e:  # 动作函数的任何异常都记为失败，不能让执行器进程退出
        out, status = {"error": f"{type(e).__name__}: {e}"[:500]}, "failed"
    r = _result(req, status, out, rolled_back=req.kind == "rollback" and status == "succeeded")
    store.put_execution(r)
    event = "action.rolled_back" if req.kind == "rollback" else "action.executed"
    store.append_audit(
        investigation_id=inv,
        actor=ACTOR,
        type=event,
        payload={
            "plan_id": plan.id,
            "plan_hash": plan.plan_hash,
            "idempotency_key": req.idempotency_key,
            "status": status,
        },
    )
    return r


def live_runner(make_http: Callable[[], Any]) -> RunAction:
    """真实执行：按动作规格导入 execute / rollback，传入写连接（只有执行器进程能构造它）。"""

    def run(spec: ActionSpec, plan: RemediationPlan, kind: str) -> dict[str, Any]:
        entry = spec.execute if kind == "execute" else spec.rollback
        if entry is None:
            raise RuntimeError(f"{spec.id} 没有回滚函数")
        http = make_http()
        try:
            return dict(resolve_entry(entry)(ExecContext(plan.target, plan.params, plan.precheck_snapshot, http)))
        finally:
            close = getattr(http, "close", None)
            if close:
                close()

    return run
