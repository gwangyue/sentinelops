"""紧急停止（07 §7）：sentinel freeze / unfreeze。

负责：冻结状态的读写（只追加，最新一行决定当前状态）；冻结时作废所有待审批的请求；写审计 freeze.enabled / .disabled
      （ADR-014）。
不负责：冻结之后各处怎么表现——执行器拒绝执行（exec/core.py），plan 节点不出方案（core/graph），诊断照常进行
       （07 §7"出了问题的时候，最需要的就是诊断信息"）。
放在 governance：执行器不能依赖 core，但必须自己读冻结状态。
"""

from datetime import UTC, datetime
from typing import Protocol

import psycopg

from sentinel.contracts import ApprovalDecision
from sentinel.governance.remediation_store import DuplicateError, RemediationStore

SYSTEM_FREEZE = "system:freeze"


class FreezeState(Protocol):
    def is_frozen(self) -> bool: ...
    def set(self, frozen: bool, reason: str, actor: str) -> None: ...


class MemoryFreezeState:
    def __init__(self) -> None:
        self.events: list[tuple[bool, str, str]] = []

    def is_frozen(self) -> bool:
        return bool(self.events) and self.events[-1][0]

    def set(self, frozen: bool, reason: str, actor: str) -> None:
        self.events.append((frozen, reason, actor))


class PgFreezeState:
    def __init__(self, url: str) -> None:
        self.url = url

    def is_frozen(self) -> bool:
        with psycopg.connect(self.url) as c:
            r = c.execute("SELECT frozen FROM freeze_events ORDER BY seq DESC LIMIT 1").fetchone()
        return bool(r and r[0])

    def set(self, frozen: bool, reason: str, actor: str) -> None:
        with psycopg.connect(self.url) as c:
            c.execute("INSERT INTO freeze_events (frozen, reason, actor) VALUES (%s, %s, %s)", (frozen, reason, actor))


def freeze(state: FreezeState, rem: RemediationStore, reason: str, actor: str) -> list[str]:
    """冻结，并作废所有还没有决定的审批：写一个 rejected 的决定（审批人 system:freeze），
    这样之后谁也批不了它；审计记 approval.expired（reason=frozen）。返回作废的审批 ID。"""
    state.set(True, reason, actor)
    rem.append_audit(investigation_id=None, actor=actor, type="freeze.enabled", payload={"reason": reason})
    voided = []
    for req in rem.list_approval_requests():
        if rem.get_decision(req.id) is not None:
            continue
        try:
            rem.put_decision(
                ApprovalDecision(
                    approval_id=req.id,
                    decision="rejected",
                    approver=SYSTEM_FREEZE,
                    reason=f"frozen: {reason}",
                    decided_at=datetime.now(UTC),
                )
            )
        except DuplicateError:
            continue  # 同时有人决定了：以那个决定为准，执行器仍然会因为冻结而拒绝
        plan = rem.get_plan(req.plan_id)
        rem.append_audit(
            investigation_id=plan.investigation_id if plan else None,
            actor=SYSTEM_FREEZE,
            type="approval.expired",
            payload={"approval_id": req.id, "reason": "frozen"},
        )
        voided.append(req.id)
    return voided


def unfreeze(state: FreezeState, rem: RemediationStore, reason: str, actor: str) -> None:
    # 07 §7"解冻需要审批人角色"：第一版的角色就是 --as 传入的身份，只记进审计（07 §4）
    state.set(False, reason, actor)
    rem.append_audit(investigation_id=None, actor=actor, type="freeze.disabled", payload={"reason": reason})
