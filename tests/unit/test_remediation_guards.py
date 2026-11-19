"""修复闭环的两道护栏（教程 00 §13.5 第 3、4 号缺陷）。离线：假模型 + 回放 + 内存存储。

1. 同一个集群同一时间只能有一份活动方案：审批中，或已批准但还没验证完。第二个调查不发起审批，只出报告。
2. 等执行器超时后，没被认领的执行请求要从队列里撤回，否则执行器之后仍可能执行，而人以为没执行。
Postgres 上的加锁和"只删未认领的请求"在 docs/module-tutorial/code/remediation_guards_pg.py 里实测。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sentinel.contracts import ApprovalRequest, ExecutionResult, RemediationPlan
from sentinel.core.graph import run as R
from sentinel.governance.remediation_store import MemoryRemediationStore
from tests.flows.test_remediation import GOOD, fixture, with_plan
from tests.harness import Harness

H = Any
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


# ---------- 缺陷 3：同一集群的并发修复互斥 ----------


def _two_problems(h: Harness) -> Harness:
    """去重（同一指纹 30 分钟内只调查一次）挡得住"同一个问题修两次"，挡不住"同一集群上两个不同的问题"。
    回放只有一份录制，所以这里关掉去重的查询，让第二次调查当作另一个问题处理。"""
    h.store.recent_fingerprints = lambda *a, **k: set()  # type: ignore[method-assign]
    return h


def test_second_plan_on_same_target_waits_for_first(harness: H, tmp_path: Path) -> None:
    h: Harness = _two_problems(harness(fixture(tmp_path), with_plan(GOOD), remediate=True))
    first = h.run("remediate")
    assert "__interrupt__" in first and first["approval_id"]  # 第一个调查停在审批

    second = h.run("remediate")  # 同一个集群又发现问题，诊断出同样的方案
    assert second["remediation"] == "target_busy" and not second.get("approval_id")
    assert second["status"] == "diagnosed" and not second.get("escalate_reason")  # 和冻结一样只出报告，不升级
    assert len(h.rem_store.list_approval_requests()) == 1  # 第二份方案没有发起审批
    proposed = [e for e in h.store.audit_events(second["inv_id"]) if e.type == "plan.proposed"]
    assert proposed[-1].payload["rejected"] == f"target_busy: {first['plan']['id']}"


def test_lock_released_after_verify_even_while_waiting_for_confirm(harness: H, tmp_path: Path) -> None:
    h: Harness = _two_problems(harness(fixture(tmp_path), with_plan(GOOD), remediate=True))
    first = h.run("remediate")
    h.decide("approved")
    out = h.resume(first["inv_id"])
    assert "__interrupt__" in out and out["remediation"] == "resolved"  # 验证通过，停在 confirm 等人确认根因
    # 改动已经验证完：等人确认根因可能要几个小时，这期间不能挡住同一集群的新方案
    second = h.run("remediate")
    assert second.get("approval_id") and second.get("remediation") != "target_busy"


def test_lock_released_when_first_is_rejected(harness: H, tmp_path: Path) -> None:
    h: Harness = _two_problems(harness(fixture(tmp_path), with_plan(GOOD), remediate=True))
    first = h.run("remediate")
    h.decide("rejected")
    h.resume(first["inv_id"])  # 被拒绝 → 调查结束
    second = h.run("remediate")
    assert second.get("approval_id")


def _plan(pid: str, target: str) -> RemediationPlan:
    return RemediationPlan(
        id=pid,
        investigation_id=f"inv_{pid}",
        action_id="es.enable_allocation",
        target=target,
        params={},
        rationale_hypothesis="H1",
        precheck_snapshot={},
        dry_run_result={},
        rollback_plan=None,
        plan_hash=f"sha256:{pid}",
    )


def _req(pid: str, at: datetime) -> ApprovalRequest:
    return ApprovalRequest(
        id=f"apv_{pid}", plan_id=pid, plan_hash=f"sha256:{pid}", requested_at=at, expires_at=at + timedelta(minutes=30)
    )


def test_exclusive_request_by_target_and_expiry() -> None:
    st = MemoryRemediationStore()
    assert st.request_approval_exclusive(_plan("p1", "es-target"), _req("p1", NOW), NOW) is None
    # 同一集群：被 p1 挡住，p2 的方案和审批请求都不写
    assert st.request_approval_exclusive(_plan("p2", "es-target"), _req("p2", NOW), NOW) == "p1"
    assert st.get_plan("p2") is None and st.get_approval_request("apv_p2") is None
    # 另一个集群不受影响
    assert st.request_approval_exclusive(_plan("p3", "mysql-target"), _req("p3", NOW), NOW) is None
    # p1 的审批过期了还没人决定：不再算活动方案
    later = NOW + timedelta(minutes=31)
    assert st.request_approval_exclusive(_plan("p4", "es-target"), _req("p4", later), later) is None


# ---------- 缺陷 4：等执行器超时后撤回请求 ----------


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R.time, "sleep", lambda s: None)


def test_unclaimed_request_is_withdrawn_on_timeout(no_sleep: None) -> None:
    st = MemoryRemediationStore()
    r = R.queue_executor(st, wait_s=0)("execute", "pln_1", "apv_1", "key_1")
    assert r.status == "failed" and "已撤回" in r.output["error"] and "没有执行" in r.output["error"]
    assert st.claim_execution_request() is None  # 执行器之后再来轮询，也拿不到这条请求了


class _ClaimedImmediately(MemoryRemediationStore):
    """执行器一插入就认领了，但一直没有写结果（例如卡在目标系统上）。"""

    def request_execution(self, kind: Any, plan_id: str, approval_id: str, idempotency_key: str) -> None:
        super().request_execution(kind, plan_id, approval_id, idempotency_key)
        assert self.claim_execution_request() is not None


def test_claimed_request_cannot_be_withdrawn_and_says_so(no_sleep: None) -> None:
    r = R.queue_executor(_ClaimedImmediately(), wait_s=0)("execute", "pln_1", "apv_1", "key_1")
    assert r.status == "failed" and "已认领" in r.output["error"] and "可能仍在执行" in r.output["error"]


class _FinishesDuringWithdraw(_ClaimedImmediately):
    """撤回的那一刻，执行器刚好写回了结果。"""

    def withdraw_execution_request(self, idempotency_key: str, kind: Any) -> bool:
        self.put_execution(
            ExecutionResult(
                plan_id="pln_1",
                approval_id="apv_1",
                idempotency_key=idempotency_key,
                status="succeeded",
                output={},
                verify_passed=None,
            )
        )
        return super().withdraw_execution_request(idempotency_key, kind)


def test_result_written_during_withdraw_is_used(no_sleep: None) -> None:
    r = R.queue_executor(_FinishesDuringWithdraw(), wait_s=0)("execute", "pln_1", "apv_1", "key_1")
    assert r.status == "succeeded"  # 不能把已经成功的执行报成失败
