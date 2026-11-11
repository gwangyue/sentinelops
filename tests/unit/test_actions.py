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
    healthy = [{"node": "data-2", "disk.percent": "50"}]  # 低于低水位 85%
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


def test_raise_watermark_sets_low_high_headrooms_and_rolls_back_all() -> None:
    alloc = [{"node": "data-2", "disk.percent": "93"}]
    pre = A.raise_watermark_precheck(ctx({"high_pct": 94}, es_cat_allocation=alloc))
    assert pre.ok and pre.snapshot["nodes_over_low"] == ["data-2"]
    w = FakeWrite()
    A.raise_watermark_execute(ExecContext("es-target", {"high_pct": 94}, pre.snapshot, w))
    A.raise_watermark_rollback(ExecContext("es-target", {"high_pct": 94}, pre.snapshot, w))
    # 低水位决定新分片能不能放上去；ES 9 只设水位不设 headroom 会 400
    assert w.calls[0][2] == {
        "persistent": {A.LOW: "94%", A.LOW_HEADROOM: "200GB", A.HIGH: "94%", A.HIGH_HEADROOM: "150GB"}
    }
    assert w.calls[1][2]["persistent"] == dict.fromkeys(A.WATERMARK_KEYS)
    # 节点在低水位和高水位之间（87%）也会挡住新副本
    assert A.raise_watermark_precheck(ctx({"high_pct": 90}, es_cat_allocation=[{"node": "d", "disk.percent": "87"}])).ok


def test_audit_chain_detects_tamper_delete_and_recompute() -> None:
    from sentinel.core.store import MemoryStore
    from sentinel.governance.audit import event_hash, verify_anchors, verify_chain

    st = MemoryStore()
    for i in range(5):
        st.append_audit(investigation_id="inv", agent_version=None, actor="agent", type=f"t{i}", payload={"i": i})
    ev = st.audit_events()
    assert verify_chain(ev) == [] and verify_chain(ev[2:], ev[1]) == []  # --since：从窗口前一条接上
    tampered = [*ev[:2], ev[2].model_copy(update={"payload": {"i": 99}}), *ev[3:]]
    assert verify_chain(tampered)[0].startswith(ev[3].id)  # 改了第 3 条，第 4 条的链接对不上
    deleted = [*ev[:2], *ev[3:]]
    assert verify_chain(deleted)[0].startswith(ev[3].id)
    anchors = [(ev[-1].id, event_hash(ev[-1]))]
    assert verify_anchors(ev, anchors) == []
    # 最后一条被改、整条链又被重算一遍：链本身查不出来，锚定能查出来
    last = ev[-1].model_copy(update={"payload": {"i": -1}})
    assert verify_chain([*ev[:-1], last]) == [] and verify_anchors([*ev[:-1], last], anchors)
