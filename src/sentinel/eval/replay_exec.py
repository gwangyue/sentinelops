"""回放评测里的修复段（01 §3.1；ADR-005 后果）：内存里的修复存储 + 进程内的回放执行器 + 模拟审批人。

负责：组装 core 需要的 Remediation（执行器接口直接调用 exec.core.process，校验和真实执行器是同一段代码，
      只把"执行"换成"回放切到 post 阶段"）；approve_all：以 sim:eval 身份批准待审批的方案（评测里没有人）。
不负责：线上的执行器（独立进程，exec/__main__.py）。
评测不写 Postgres（06 §9），所以用 MemoryRemediationStore。
"""

from collections.abc import Callable
from datetime import UTC, datetime

from sentinel.contracts import ActionSpec, ApprovalDecision, ExecutionResult
from sentinel.core.api import Remediation, ToolBackend
from sentinel.exec.core import process
from sentinel.exec.replay import replay_runner
from sentinel.governance.remediation_store import ExecKind, ExecRequest, MemoryRemediationStore

SIM_APPROVER = "sim:eval"


def replay_remediation(
    store: MemoryRemediationStore, specs: dict[str, ActionSpec], backend_for: Callable[[str], ToolBackend]
) -> Remediation:
    """backend_for(调查 ID) → 这次调查的回放后端（和诊断用的是同一个，切到 post 之后验证读到的就是执行之后的录制）。"""

    def executor(kind: ExecKind, plan_id: str, approval_id: str, key: str) -> ExecutionResult:
        plan = store.get_plan(plan_id)
        assert plan is not None
        b = backend_for(plan.investigation_id)
        return process(ExecRequest(0, kind, plan_id, approval_id, key), store, specs, b, replay_runner(b.enter_post))

    return Remediation(store=store, executor=executor, verify_once=True)


def approve_all(store: MemoryRemediationStore) -> list[str]:
    """批准所有还没有决定的审批请求。只有评测和测试用：线上的审批只能来自 CLI 里的人。"""
    done = []
    for req in store.list_approval_requests():
        if store.get_decision(req.id) is None:
            store.put_decision(
                ApprovalDecision(
                    approval_id=req.id,
                    decision="approved",
                    approver=SIM_APPROVER,
                    reason="评测模拟审批",
                    decided_at=datetime.now(UTC),
                )
            )
            done.append(req.id)
    return done
