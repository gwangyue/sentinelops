"""修复方案、审批、执行的存储（03 §4.3–4.4；02 §3.4）：调查引擎和执行器共用。

负责：plans / approval_requests / approval_decisions / execution_requests / executions 五张表的读写（DDL 在
      core/store/sql/003_remediation.sql），以及执行器写审计（和调查引擎同一条哈希链）。
不负责：哪些检查通过才能写（plan 节点、执行器）。
放在 governance：执行器不能依赖 core（ADR-005），而 plan 节点和执行器必须读写同一份记录。
Memory 实现给测试和回放评测用：评测不写 Postgres（06 §9）。
"""

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import psycopg
from psycopg.rows import dict_row

from sentinel.contracts import ApprovalDecision, ApprovalRequest, AuditEvent, ExecutionResult, RemediationPlan
from sentinel.governance.audit import new_event, pg_append

ExecKind = Literal["execute", "rollback"]


@dataclass(frozen=True)
class ExecRequest:
    """调查引擎交给执行器的全部内容：只有三个 ID（ADR-005），其余执行器自己读。"""

    id: int
    kind: ExecKind
    plan_id: str
    approval_id: str
    idempotency_key: str


class DuplicateError(RuntimeError):
    """唯一约束挡住的写入：同一个审批的第二个决定、同一个幂等键的第二个结果。"""


class RemediationStore(Protocol):
    def put_plan(self, plan: RemediationPlan) -> None: ...
    def get_plan(self, plan_id: str) -> RemediationPlan | None: ...
    def put_approval_request(self, req: ApprovalRequest) -> None: ...
    def get_approval_request(self, approval_id: str) -> ApprovalRequest | None: ...
    def list_approval_requests(self) -> list[ApprovalRequest]: ...
    def put_decision(self, d: ApprovalDecision) -> None: ...
    def get_decision(self, approval_id: str) -> ApprovalDecision | None: ...
    def request_execution(self, kind: ExecKind, plan_id: str, approval_id: str, idempotency_key: str) -> None: ...
    def claim_execution_request(self) -> ExecRequest | None: ...
    def put_execution(self, r: ExecutionResult) -> None: ...
    def get_execution(self, idempotency_key: str) -> ExecutionResult | None: ...
    def append_audit(
        self, *, investigation_id: str | None, actor: str, type: str, payload: dict[str, Any]
    ) -> AuditEvent: ...


class MemoryRemediationStore:
    def __init__(self) -> None:
        self.plans: dict[str, RemediationPlan] = {}
        self.requests: dict[str, ApprovalRequest] = {}
        self.decisions: dict[str, ApprovalDecision] = {}
        self.queue: list[ExecRequest] = []
        self.executions: dict[str, ExecutionResult] = {}
        self.audit: list[AuditEvent] = []

    def put_plan(self, plan: RemediationPlan) -> None:
        self.plans[plan.id] = plan

    def get_plan(self, plan_id: str) -> RemediationPlan | None:
        return self.plans.get(plan_id)

    def put_approval_request(self, req: ApprovalRequest) -> None:
        self.requests[req.id] = req

    def get_approval_request(self, approval_id: str) -> ApprovalRequest | None:
        return self.requests.get(approval_id)

    def list_approval_requests(self) -> list[ApprovalRequest]:
        return sorted(self.requests.values(), key=lambda r: r.requested_at, reverse=True)

    def put_decision(self, d: ApprovalDecision) -> None:
        if d.approval_id in self.decisions:
            raise DuplicateError(f"{d.approval_id} 已经有决定")
        self.decisions[d.approval_id] = d

    def get_decision(self, approval_id: str) -> ApprovalDecision | None:
        return self.decisions.get(approval_id)

    def request_execution(self, kind: ExecKind, plan_id: str, approval_id: str, idempotency_key: str) -> None:
        self.queue.append(ExecRequest(len(self.queue) + 1, kind, plan_id, approval_id, idempotency_key))

    def claim_execution_request(self) -> ExecRequest | None:
        return self.queue.pop(0) if self.queue else None

    def put_execution(self, r: ExecutionResult) -> None:
        if r.idempotency_key in self.executions:
            raise DuplicateError(f"{r.idempotency_key} 已经有执行结果")
        self.executions[r.idempotency_key] = r

    def get_execution(self, idempotency_key: str) -> ExecutionResult | None:
        return self.executions.get(idempotency_key)

    def append_audit(
        self, *, investigation_id: str | None, actor: str, type: str, payload: dict[str, Any]
    ) -> AuditEvent:
        e = new_event(
            self.audit[-1] if self.audit else None,
            investigation_id=investigation_id,
            agent_version=None,
            actor=actor,
            type=type,
            payload=payload,
        )
        self.audit.append(e)
        return e


class PgRemediationStore:
    def __init__(self, url: str) -> None:
        self.url = url

    def _conn(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self.url, row_factory=dict_row, autocommit=False)

    def put_plan(self, plan: RemediationPlan) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO plans (id, investigation_id, plan_hash, body) VALUES (%s, %s, %s, %s)",
                (plan.id, plan.investigation_id, plan.plan_hash, plan.model_dump_json()),
            )

    def get_plan(self, plan_id: str) -> RemediationPlan | None:
        with self._conn() as c:
            r = c.execute("SELECT body FROM plans WHERE id = %s", (plan_id,)).fetchone()
        return RemediationPlan.model_validate(r["body"]) if r else None

    def put_approval_request(self, req: ApprovalRequest) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO approval_requests (id, plan_id, plan_hash, requested_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (req.id, req.plan_id, req.plan_hash, req.requested_at, req.expires_at),
            )

    def get_approval_request(self, approval_id: str) -> ApprovalRequest | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM approval_requests WHERE id = %s", (approval_id,)).fetchone()
        return ApprovalRequest.model_validate(r) if r else None

    def list_approval_requests(self) -> list[ApprovalRequest]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM approval_requests ORDER BY requested_at DESC LIMIT 50").fetchall()
        return [ApprovalRequest.model_validate(r) for r in rows]

    def put_decision(self, d: ApprovalDecision) -> None:
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO approval_decisions (approval_id, body) VALUES (%s, %s)",
                    (d.approval_id, d.model_dump_json()),
                )
        except psycopg.errors.UniqueViolation as e:
            raise DuplicateError(f"{d.approval_id} 已经有决定") from e

    def get_decision(self, approval_id: str) -> ApprovalDecision | None:
        with self._conn() as c:
            r = c.execute("SELECT body FROM approval_decisions WHERE approval_id = %s", (approval_id,)).fetchone()
        return ApprovalDecision.model_validate(r["body"]) if r else None

    def request_execution(self, kind: ExecKind, plan_id: str, approval_id: str, idempotency_key: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO execution_requests (kind, plan_id, approval_id, idempotency_key) VALUES (%s, %s, %s, %s)",
                (kind, plan_id, approval_id, idempotency_key),
            )

    def claim_execution_request(self) -> ExecRequest | None:
        # SKIP LOCKED：以后起多个执行器进程时，同一个请求也只会被一个进程认领
        with self._conn() as c:
            r = c.execute(
                "UPDATE execution_requests SET claimed_at = now() WHERE id = ("
                "  SELECT id FROM execution_requests WHERE claimed_at IS NULL"
                "  ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
                ") RETURNING id, kind, plan_id, approval_id, idempotency_key"
            ).fetchone()
        return ExecRequest(**r) if r else None

    def put_execution(self, r: ExecutionResult) -> None:
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO executions (idempotency_key, body) VALUES (%s, %s)",
                    (r.idempotency_key, json.dumps(r.model_dump(mode="json"))),
                )
        except psycopg.errors.UniqueViolation as e:
            raise DuplicateError(f"{r.idempotency_key} 已经有执行结果") from e

    def get_execution(self, idempotency_key: str) -> ExecutionResult | None:
        with self._conn() as c:
            r = c.execute("SELECT body FROM executions WHERE idempotency_key = %s", (idempotency_key,)).fetchone()
        return ExecutionResult.model_validate(r["body"]) if r else None

    def append_audit(
        self, *, investigation_id: str | None, actor: str, type: str, payload: dict[str, Any]
    ) -> AuditEvent:
        with self._conn() as c:
            return pg_append(
                c, investigation_id=investigation_id, agent_version=None, actor=actor, type=type, payload=payload
            )
