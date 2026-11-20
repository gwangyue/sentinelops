"""审批的两个入口（CLI、飞书）共用的部分（02 §3.3；09 §4.3、§5.2）。

负责：
- 看：按 approval_id 读出审批请求、方案、动作规格和决定，排成"标签 → 值"的行（`approvals show` 和审批卡片）。
- 决定：decide_approval——查审批、校验风险上限、调 record_decision，重复决定变成返回值（D19 从 CLI `_decide` 抽出）。
不负责：输出格式（CLI 打印文本，chatops 拼卡片）；恢复调查（调用方的事：CLI 同步等，飞书放后台线程）；
写决定和审计本身（core/graph/remediate.py 的 record_decision）。

为什么要抽出来：09 §4.3 要求"人不能在信息更少的界面上做批准"。两个入口各写一份，迟早会有一边漏掉一行；
共用一个函数之后，CLI 能看到什么，卡片上就一定有什么（测试里直接比对两边的行）。
放在 core 而不是 governance：要用 packs_registry 取动作规格，而 governance 会被执行器 import，
执行器不能依赖 core（.importlinter 的 exec-no-core）。
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from sentinel.contracts import ActionSpec, ApprovalDecision, ApprovalRequest, RemediationPlan
from sentinel.governance.remediation_store import DuplicateError, RemediationStore

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def spec_from_pack(action_id: str) -> ActionSpec:
    from sentinel.core.packs_registry import load_pack

    # 动作 ID 的前缀就是专家包名（<pack>.<动词_宾语>，03 §2）
    return load_pack(action_id.split(".", 1)[0]).actions[action_id]


@dataclass(frozen=True)
class ApprovalView:
    """一次审批的全部上下文。字段都是契约模型，这里只是把它们放在一起，不另造结构。"""

    request: ApprovalRequest
    plan: RemediationPlan
    spec: ActionSpec
    decision: ApprovalDecision | None

    @property
    def state(self) -> str:
        return self.decision.decision if self.decision else "pending"

    def lines(self) -> list[tuple[str, str]]:
        """审批人要看的每一行：动作、参数、前置检查、预演、回滚、验证条件、有效期、方案哈希。"""
        p, s, r = self.plan, self.spec, self.request
        return [
            ("动作", f"{p.action_id}（风险：{s.risk}，依据 {p.rationale_hypothesis}）"),
            ("参数", json.dumps(p.params, ensure_ascii=False)),
            ("前置检查", json.dumps(p.precheck_snapshot, ensure_ascii=False)),
            ("预演", f"{json.dumps(p.dry_run_result, ensure_ascii=False)}  上限 {s.max_blast_radius}"),
            ("回滚", str(p.rollback_plan)),
            ("验证条件", f"{s.verify.predicate.split(':')[-1]}，{s.verify.timeout_s} 秒内达成"),
            ("有效期", f"至 {r.expires_at:%Y-%m-%d %H:%M:%S %Z}"),
            ("方案哈希", p.plan_hash),
        ]


def load_approval_view(
    rem: RemediationStore, approval_id: str, spec_for: Callable[[str], ActionSpec] = spec_from_pack
) -> ApprovalView | None:
    r = rem.get_approval_request(approval_id)
    plan = rem.get_plan(r.plan_id) if r else None
    if r is None or plan is None:
        return None
    return ApprovalView(request=r, plan=plan, spec=spec_for(plan.action_id), decision=rem.get_decision(r.id))


def risk_allowed(risk: str, max_risk: str) -> bool:
    """这个风险等级能不能在飞书上审批（09 §4.3）。未知的等级一律不允许。"""
    return risk in RISK_ORDER and max_risk in RISK_ORDER and RISK_ORDER[risk] <= RISK_ORDER[max_risk]


Outcome = Literal["decided", "not_found", "risk_too_high", "duplicate"]


@dataclass(frozen=True)
class DecideResult:
    """一次决定的结果。decided 时 view 已经是写入决定之后重新读出来的（state 不再是 pending）。"""

    outcome: Outcome
    view: ApprovalView | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "decided"


def decide_approval(
    rem: RemediationStore,
    audit: Callable[..., Any],
    approval_id: str,
    decision: Literal["approved", "rejected"],
    approver: str,
    reason: str | None,
    max_risk: str = "high",
    spec_for: Callable[[str], ActionSpec] = spec_from_pack,
) -> DecideResult:
    """CLI 和飞书的审批都走这里。max_risk：这个入口最高能审批到哪个风险等级（CLI 是 high，飞书看配置）。

    风险上限对"拒绝"也生效：飞书上高于上限的卡片本来就没有按钮，收到这样的点击只可能是伪造的（09 §5.2 第 2 步）。
    哈希、方案只从存储里读，调用方只给 approval_id（T3、T4）。
    有效期不在这里查：和 CLI 一样，过期的审批由执行器拒绝。TODO(edge)：在入口提前提示"已过期"。
    """
    from sentinel.core.graph.remediate import record_decision

    v = load_approval_view(rem, approval_id, spec_for)
    if v is None:
        return DecideResult("not_found")
    if not risk_allowed(v.spec.risk, max_risk):
        return DecideResult("risk_too_high", v)
    if v.decision is not None:
        return DecideResult("duplicate", v)
    try:
        record_decision(rem, audit, approval_id, decision, approver, reason)
    except DuplicateError:
        # 两个人几乎同时点：上面的检查都通过了，唯一约束挡住后到的那个
        return DecideResult("duplicate", load_approval_view(rem, approval_id, spec_for))
    return DecideResult("decided", load_approval_view(rem, approval_id, spec_for))


def resume_summary(out: dict[str, Any]) -> str:
    """resume 之后调查停在哪：修好之后停在 confirm（interrupt），图还没走到 report，状态里没有 status。"""
    return "awaiting_confirmation" if "__interrupt__" in out else str(out.get("status"))
