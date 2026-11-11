"""动作目录（03 §4.2）、方案哈希（03 §4.3）、修复存储的唯一约束（02 §3.4）。离线：假的只读工具 + 假的写连接。"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sentinel.contracts import ApprovalDecision, ApprovalRequest, ExecutionResult
from sentinel.core.packs_registry import load_pack
from sentinel.governance.plan_hash import idempotency_key, plan_hash
from sentinel.governance.remediation_store import DuplicateError, MemoryRemediationStore
from sentinel.packs.es.actions import es_actions as A
from sentinel.sdk import ActionContext, ExecContext, ToolResult, resolve_entry

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


class FakeTools:
    def __init__(self, raws: dict[str, Any]) -> None:
        self.raws = raws

    def call(self, tool: str, args: Mapping[str, Any] | None = None) -> ToolResult:
        return ToolResult(f"obs_{tool}", "ok", self.raws[tool], "")


class FakeWrite:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return {}

    def put(self, path: str, body: Mapping[str, Any]) -> Any:
        self.calls.append(("PUT", path, dict(body)))
        return {"acknowledged": True}

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        self.calls.append(("POST", path, dict(body)))
        return {"acknowledged": True}


HEALTH = {"status": "yellow", "unassigned_shards": 2, "number_of_data_nodes": 3, "active_shards": 17}


def ctx(params: dict[str, Any], **raws: Any) -> ActionContext:
    base = {"es.cluster_health": HEALTH, "es.cluster_settings": {"persistent": {}, "transient": {}}}
    return ActionContext("es-target", params, FakeTools(base | {k.replace("_", ".", 1): v for k, v in raws.items()}))


def test_all_specs_load_and_entries_resolve() -> None:
    p = load_pack("es")
    assert set(p.actions) == set(p.manifest.actions) and len(p.actions) == 5
    for a in p.actions.values():
        assert a.requires_approval and a.risk != "high"  # 07 §4：第一版不放 high 级别的动作
        for e in [*a.preconditions, a.dry_run, a.execute, a.rollback or "", a.verify.predicate]:
            assert callable(resolve_entry(e))


def test_enable_allocation_precheck_and_rollback_restore_original() -> None:
    s = {"persistent": {A.ENABLE: "primaries"}, "transient": {}}
    assert not A.enable_allocation_precheck(ctx({}, es_cluster_settings={"persistent": {}, "transient": {}})).ok
    pre = A.enable_allocation_precheck(ctx({}, es_cluster_settings=s))
    assert pre.ok and pre.snapshot["before"] == {"persistent": "primaries", "transient": None}
    w = FakeWrite()
    A.enable_allocation_execute(ExecContext("es-target", {}, pre.snapshot, w))
    A.enable_allocation_rollback(ExecContext("es-target", {}, pre.snapshot, w))
    assert w.calls[0][2] == {"persistent": {A.ENABLE: None}, "transient": {A.ENABLE: None}}
    assert w.calls[1][2] == {"persistent": {A.ENABLE: "primaries"}, "transient": {A.ENABLE: None}}  # 按原样恢复


def test_raise_watermark_bounds() -> None:
    alloc = [{"node": "data-2", "disk.percent": "93"}, {"node": "data-0", "disk.percent": "40"}]
    assert A.raise_watermark_precheck(ctx({"high_pct": 94}, es_cat_allocation=alloc)).ok
    assert not A.raise_watermark_precheck(ctx({"high_pct": 93}, es_cat_allocation=alloc)).ok  # 不高于使用率：没用
    assert not A.raise_watermark_precheck(ctx({"high_pct": 96}, es_cat_allocation=alloc)).ok  # 超过洪水位
    healthy = [{"node": "data-2", "disk.percent": "50"}]
    assert not A.raise_watermark_precheck(ctx({"high_pct": 94}, es_cat_allocation=healthy)).ok


def test_shard_limit_and_set_replicas_prechecks() -> None:
    s = {"persistent": {A.MAX_SHARDS: "6"}, "transient": {}}
    assert A.shard_limit_precheck(ctx({"max_shards_per_node": 10}, es_cluster_settings=s)).ok  # 17/18
    assert not A.shard_limit_precheck(ctx({"max_shards_per_node": 100}, es_cluster_settings=s)).ok  # 超过 2 倍
    rows = [{"index": "bench-new", "rep": "3", "health": "yellow"}]
    assert A.set_replicas_precheck(ctx({"index": "bench-new", "replicas": 2}, es_cat_indices=rows)).ok
    assert not A.set_replicas_precheck(ctx({"index": "bench-new", "replicas": 3}, es_cat_indices=rows)).ok
    assert not A.set_replicas_precheck(ctx({"index": "nope", "replicas": 1}, es_cat_indices=rows)).ok


def test_plan_hash_deterministic_and_sensitive() -> None:
    h = plan_hash("es.set_replicas", "es-target", {"index": "a", "replicas": 1}, {"before": 3})
    assert h == plan_hash("es.set_replicas", "es-target", {"replicas": 1, "index": "a"}, {"before": 3})  # 顺序无关
    for changed in (
        plan_hash("es.set_replicas", "es-target", {"index": "a", "replicas": 2}, {"before": 3}),
        plan_hash("es.set_replicas", "other", {"index": "a", "replicas": 1}, {"before": 3}),
        plan_hash("es.set_replicas", "es-target", {"index": "a", "replicas": 1}, {"before": 2}),
        plan_hash("es.enable_allocation", "es-target", {"index": "a", "replicas": 1}, {"before": 3}),
    ):
        assert changed != h
    assert idempotency_key("inv_1", h) != idempotency_key("inv_2", h)


def test_store_rejects_second_decision_and_second_result() -> None:
    st = MemoryRemediationStore()
    st.put_approval_request(
        ApprovalRequest(
            id="apv_1", plan_id="pln_1", plan_hash="h", requested_at=NOW, expires_at=NOW + timedelta(minutes=30)
        )
    )
    st.put_decision(
        ApprovalDecision(approval_id="apv_1", decision="approved", approver="a", reason=None, decided_at=NOW)
    )
    with pytest.raises(DuplicateError):
        st.put_decision(
            ApprovalDecision(approval_id="apv_1", decision="rejected", approver="b", reason=None, decided_at=NOW)
        )
    r = ExecutionResult(
        plan_id="pln_1", approval_id="apv_1", idempotency_key="k", status="succeeded", output={}, verify_passed=None
    )
    st.put_execution(r)
    with pytest.raises(DuplicateError):
        st.put_execution(r)
    e1 = st.append_audit(investigation_id="inv", actor="executor", type="action.executed", payload={})
    e2 = st.append_audit(investigation_id="inv", actor="executor", type="verify.passed", payload={})
    from sentinel.governance.audit import event_hash

    assert e2.prev_hash == event_hash(e1)
