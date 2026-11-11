"""执行器核心流程（03 §4.4；02 §3.4）：哈希、审批、过期、幂等、执行前重做前置检查、回滚。离线：内存存储 + 假工具。"""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sentinel.contracts import ActionSpec, ApprovalDecision, ApprovalRequest, RemediationPlan
from sentinel.core.packs_registry import load_pack
from sentinel.exec.core import process, rollback_key
from sentinel.governance.plan_hash import idempotency_key, plan_hash
from sentinel.governance.remediation_store import ExecRequest, MemoryRemediationStore
from tests.unit.test_actions import FakeTools

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
SPECS = load_pack("es").actions
SETTINGS_DISABLED = {"persistent": {"cluster.routing.allocation.enable": "none"}, "transient": {}}
HEALTH = {"status": "yellow", "unassigned_shards": 2, "number_of_data_nodes": 3, "active_shards": 17}


def setup(
    decision: Literal["approved", "rejected"] = "approved", tamper: bool = False
) -> tuple[MemoryRemediationStore, ExecRequest]:
    st = MemoryRemediationStore()
    snap = {"before": {"persistent": "none", "transient": None}}
    h = plan_hash("es.enable_allocation", "es-target", {}, snap)
    plan = RemediationPlan(
        id="pln_1",
        investigation_id="inv_1",
        action_id="es.enable_allocation",
        target="es-target",
        params={"tampered": True} if tamper else {},  # 审批之后改了参数，但哈希还是旧的
        rationale_hypothesis="H1",
        precheck_snapshot=snap,
        dry_run_result={},
        rollback_plan="恢复原值",
        plan_hash=h,
    )
    st.put_plan(plan)
    st.put_approval_request(
        ApprovalRequest(
            id="apv_1", plan_id="pln_1", plan_hash=h, requested_at=NOW, expires_at=NOW + timedelta(minutes=30)
        )
    )
    st.put_decision(
        ApprovalDecision(approval_id="apv_1", decision=decision, approver="human:a", reason=None, decided_at=NOW)
    )
    return st, ExecRequest(1, "execute", "pln_1", "apv_1", idempotency_key("inv_1", h))


def tools(settings: dict[str, Any] = SETTINGS_DISABLED) -> FakeTools:
    return FakeTools({"es.cluster_settings": settings, "es.cluster_health": HEALTH})


class Runner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, spec: ActionSpec, plan: RemediationPlan, kind: str) -> dict[str, Any]:
        self.calls.append(kind)
        return {"acknowledged": True}


def at(minutes: int) -> Any:
    return lambda: NOW + timedelta(minutes=minutes)


def test_success_then_duplicate() -> None:
    st, req = setup()
    run = Runner()
    r = process(req, st, SPECS, tools(), run, at(1))
    assert r.status == "succeeded" and run.calls == ["execute"]
    again = process(req, st, SPECS, tools(), run, at(2))  # 恢复、重试时同一个键再来一次
    assert again.status == "duplicate" and run.calls == ["execute"]  # 没有执行第二次
    assert [e.type for e in st.audit] == ["action.executed"]


def test_refuses_tampered_plan_rejected_or_expired() -> None:
    for st, req, now, event in (
        (*setup(tamper=True), at(1), "action.precheck_failed"),
        (*setup(decision="rejected"), at(1), "action.precheck_failed"),
        (*setup(), at(31), "approval.expired"),
    ):
        run = Runner()
        r = process(req, st, SPECS, tools(), run, now)
        assert r.status == "precheck_failed" and r.output["stage"] == "approval" and run.calls == []
        assert st.audit[-1].type == event


def test_wrong_idempotency_key_refused() -> None:
    st, _ = setup()
    bad = ExecRequest(1, "execute", "pln_1", "apv_1", "sha256:forged")
    assert process(bad, st, SPECS, tools(), Runner(), at(1)).status == "precheck_failed"


def test_precheck_rerun_at_execution_time() -> None:
    # 审批之后，有人已经手动恢复了分配：执行时重做前置检查，不再执行（TOCTOU）
    st, req = setup()
    run = Runner()
    r = process(req, st, SPECS, tools({"persistent": {}, "transient": {}}), run, at(1))
    assert r.status == "precheck_failed" and r.output["stage"] == "precondition" and run.calls == []


def test_rollback_has_own_key_and_ignores_expiry() -> None:
    st, first = setup()
    process(first, st, SPECS, tools(), Runner(), at(1))
    h = st.get_plan("pln_1").plan_hash  # type: ignore[union-attr]
    rb = ExecRequest(2, "rollback", "pln_1", "apv_1", rollback_key("inv_1", h))
    run = Runner()
    r = process(rb, st, SPECS, tools(), run, at(60))  # 审批已经过期，回滚照样可以做
    assert r.status == "succeeded" and r.rolled_back and run.calls == ["rollback"]
    assert st.audit[-1].type == "action.rolled_back"


def test_action_exception_recorded_as_failed() -> None:
    st, req = setup()

    def boom(spec: ActionSpec, plan: RemediationPlan, kind: str) -> dict[str, Any]:
        raise RuntimeError("ES 返回 500")

    r = process(req, st, SPECS, tools(), boom, at(1))
    assert r.status == "failed" and "ES 返回 500" in r.output["error"]


def test_replay_runner_switches_phase_only_for_recorded_action() -> None:
    from sentinel.exec.replay import replay_runner

    switched: list[str] = []

    def enter_post(aid: str) -> bool:
        switched.append(aid)
        return aid == "es.enable_allocation"

    st, req = setup()
    r = process(req, st, SPECS, tools(), replay_runner(enter_post), at(1))
    assert r.status == "succeeded" and r.output["simulated"] and switched == ["es.enable_allocation"]
    st2, req2 = setup()
    r2 = process(req2, st2, SPECS, tools(), replay_runner(lambda aid: False), at(1))
    assert r2.status == "failed" and "PostNotRecorded" in r2.output["error"]
