"""修复段的流程（02 §3；M3 D10–D11）：提议 → 不安全拦截 → 前置检查 → 审批暂停 → 恢复 → 回放执行 → 验证 / 回滚。

假模型 + 回放 + 内存存储，不访问网络。录制在 disk_watermark 的基础上补一条 post 阶段（执行 raise_watermark 之后变绿）。
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage

from tests.flows.scripts import diagnose_script
from tests.harness import FIX, Harness

H = Any
RAISE = "es.raise_watermark_temporarily"


def fixture(tmp: Path, post_status: str | None = "green") -> Path:
    lines = (FIX / "disk_watermark.jsonl").read_text(encoding="utf-8").splitlines()
    if post_status:
        pre = next(json.loads(x) for x in lines if '"es.cluster_health"' in x)
        post = {
            **pre,
            "phase": "post",
            "action": RAISE,
            "raw": {**pre["raw"], "status": post_status, "unassigned_shards": 0 if post_status == "green" else 1},
        }
        lines.append(json.dumps(post))
    p = tmp / "fx.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def with_plan(proposal: str):  # type: ignore[no-untyped-def]
    """诊断用 diagnose_script；看到 plan 提示词（含"动作目录"）时返回给定的提议文本。"""
    diag = diagnose_script()

    def script(messages: Sequence[BaseMessage]) -> AIMessage:
        if "动作目录" in str(messages[-1].content):
            return AIMessage(proposal)
        return diag(messages)

    return script


GOOD = json.dumps({"action_id": RAISE, "params": {"high_pct": 94}, "rationale_hypothesis": "H1"})


def approve(h: Harness, decision: Literal["approved", "rejected"] = "approved") -> None:
    h.decide(decision)


def test_full_path_resolves(harness: H, tmp_path: Path) -> None:
    h: Harness = harness(fixture(tmp_path), with_plan(GOOD), remediate=True)
    s = h.run("remediate")
    assert "__interrupt__" in s and s["approval_id"]  # 停在 approve 节点等人
    plan = s["plan"]
    assert plan["plan_hash"].startswith("sha256:") and plan["precheck_snapshot"]["nodes_over_low"] == [
        "es-target-es-data-2"
    ]
    assert h.rem_store.get_execution  # 审批之前什么都没执行
    assert not h.rem_store.executions
    approve(h)
    out = h.resume(s["inv_id"])
    assert "__interrupt__" in out and out["remediation"] == "resolved"  # 修好了，停在 confirm 等人确认根因
    h.confirm(s["inv_id"])
    out = h.resume(s["inv_id"])
    assert out["status"] == "resolved" and out["memory_id"]
    types = h.audit_types(s["inv_id"])
    i = types.index
    # golden-demo 第 6 步的顺序；执行器的 action.executed 和调查在同一条审计链上
    assert i("plan.proposed") < i("approval.requested") < i("approval.decided") < i("action.executed")
    assert i("action.executed") < i("verify.passed") < i("investigation.closed")


def test_unsafe_proposals_are_blocked(harness: H, tmp_path: Path) -> None:
    for bad in (
        json.dumps({"action_id": "es.delete_index", "params": {"index": "bench"}, "rationale_hypothesis": "H1"}),
        json.dumps({"action_id": RAISE, "params": {"high_pct": 99}, "rationale_hypothesis": "H1"}),  # 超出 schema
        json.dumps({"action_id": RAISE, "params": {"high_pct": 94, "extra": 1}, "rationale_hypothesis": "H1"}),
        "我建议删除索引",
    ):
        h: Harness = harness(fixture(tmp_path), with_plan(bad), remediate=True)
        s = h.run("remediate")
        assert s["status"] == "escalated" and s["unsafe_proposals"] == 1 and s["remediation"] == "unsafe"
        assert "plan.rejected_unsafe" in h.audit_types(s["inv_id"]) and not h.rem_store.plans


def test_rejected_approval_does_not_execute(harness: H, tmp_path: Path) -> None:
    h: Harness = harness(fixture(tmp_path), with_plan(GOOD), remediate=True)
    s = h.run("remediate")
    approve(h, "rejected")
    out = h.resume(s["inv_id"])
    assert out["remediation"] == "rejected" and out["status"] == "diagnosed" and not h.rem_store.executions


def test_verify_failure_rolls_back(harness: H, tmp_path: Path) -> None:
    h: Harness = harness(fixture(tmp_path, post_status="yellow"), with_plan(GOOD), remediate=True)
    s = h.run("remediate")
    approve(h)
    out = h.resume(s["inv_id"])
    assert out["remediation"] == "rolled_back" and out["status"] == "escalated"
    assert "verify.failed" in h.audit_types(s["inv_id"])
    types = h.audit_types(s["inv_id"])
    assert types.index("action.executed") < types.index("verify.failed") < types.index("action.rolled_back")


def test_action_without_post_recording_is_not_scored(harness: H, tmp_path: Path) -> None:
    h: Harness = harness(fixture(tmp_path, post_status=None), with_plan(GOOD), remediate=True)
    s = h.run("remediate")
    approve(h)
    out = h.resume(s["inv_id"])
    assert out["remediation"] == "not_recorded" and out["status"] == "diagnosed"


def test_no_action_and_report_only_mode(harness: H, tmp_path: Path) -> None:
    none = json.dumps({"action_id": "none", "params": {}, "rationale_hypothesis": "H1"})
    h: Harness = harness(fixture(tmp_path), with_plan(none), remediate=True)
    s = h.run("remediate")
    assert s["remediation"] == "no_action" and s["status"] == "diagnosed" and "__interrupt__" not in s
    h2: Harness = harness(fixture(tmp_path), with_plan(GOOD), remediate=True)
    s2 = h2.run("report_only")  # 巡检：诊断之后直接出报告，不调用 plan
    assert s2["status"] == "diagnosed" and not s2.get("plan") and not h2.rem_store.plans


def test_freeze_blocks_plan_voids_approvals_and_executor_refuses(harness: H, tmp_path: Path) -> None:
    from sentinel.governance.kill_switch import freeze, unfreeze

    # 1. 已经在等审批时冻结：待审批作废；即使有人想批准也来不及了（决定已被 system:freeze 写成 rejected）
    h: Harness = harness(fixture(tmp_path), with_plan(GOOD), remediate=True)
    s = h.run("remediate")
    voided = freeze(h.freeze_state, h.rem_store, "执行器异常", "human:ops")
    assert voided == [s["approval_id"]]
    out = h.resume(s["inv_id"])
    assert out["remediation"] == "rejected" and not h.rem_store.executions
    types = h.audit_types(s["inv_id"])
    assert "approval.expired" in types
    assert [e.type for e in h.store.audit_events() if e.investigation_id is None] == ["freeze.enabled"]
    # 2. 冻结期间的新调查（另一个集群状态，避开 30 分钟去重）：照常诊断，但不出方案（07 §7）
    h2: Harness = harness(fixture(tmp_path), with_plan(GOOD), remediate=True)
    h2.freeze_state.set(True, "执行器异常", "human:ops")
    s2 = h2.run("remediate")
    assert s2["diagnosis"] and s2["remediation"] == "frozen" and "__interrupt__" not in s2
    unfreeze(h.freeze_state, h.rem_store, "排查完毕", "human:approver")
    assert not h.freeze_state.is_frozen()


def test_executor_refuses_when_frozen_after_approval(harness: H, tmp_path: Path) -> None:
    # 审批之后、执行之前冻结：执行器自己读冻结状态，拒绝执行
    h: Harness = harness(fixture(tmp_path), with_plan(GOOD), remediate=True)
    s = h.run("remediate")
    approve(h)
    h.freeze_state.set(True, "测试", "human:ops")
    out = h.resume(s["inv_id"])
    assert out["remediation"] == "exec_precheck_failed" and not h.rem_store.executions
    ev = [e for e in h.store.audit_events(s["inv_id"]) if e.type == "action.precheck_failed"]
    assert ev and ev[0].payload["stage"] == "frozen"


def _to_confirm(harness: H, tmp_path: Path) -> tuple[Harness, str]:
    h: Harness = harness(fixture(tmp_path), with_plan(GOOD), remediate=True)
    s = h.run("remediate")
    approve(h)
    h.resume(s["inv_id"])
    return h, s["inv_id"]


def test_confirmed_root_cause_is_memorized(harness: H, tmp_path: Path) -> None:
    h, inv = _to_confirm(harness, tmp_path)
    h.confirm(inv)
    out = h.resume(inv)
    m = h.mem_index.get(out["memory_id"])
    assert m is not None and inv in m.record.source_investigations and not m.record.corrected
    assert m.record.root_cause_label == "es.disk_high_watermark" and m.record.action_taken == RAISE
    assert m.record.outcome == "resolved" and m.record.confirmed_by == "human:t"
    types = h.audit_types(inv)
    assert types.index("rootcause.confirmed") < types.index("memory.written")  # golden-demo 第 7 步


def test_corrected_root_cause_marks_memory(harness: H, tmp_path: Path) -> None:
    h, inv = _to_confirm(harness, tmp_path)
    h.confirm(inv, label="es.shard_limit_reached")  # 人修正：和 H1 不同
    out = h.resume(inv)
    m = h.mem_index.get(out["memory_id"])
    assert m is not None and m.record.corrected and m.record.root_cause_label == "es.shard_limit_reached"
    assert "rootcause.corrected" in h.audit_types(inv)


def test_declined_confirmation_writes_nothing(harness: H, tmp_path: Path) -> None:
    h, inv = _to_confirm(harness, tmp_path)
    out = h.resume(inv)  # 没有确认就恢复（CLI --decline）
    assert out["status"] == "resolved" and out["memory_id"] is None and not h.mem_index.items
    assert "memory.written" not in h.audit_types(inv)


def test_confirmation_rejects_unknown_labels(harness: H, tmp_path: Path) -> None:
    import pytest

    h, inv = _to_confirm(harness, tmp_path)
    with pytest.raises(ValueError):
        h.confirm(inv, label="es.made_up")
    with pytest.raises(ValueError):
        h.confirm(inv, hypothesis="H9")
