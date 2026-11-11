"""修复段的流程（02 §3；M3 D10–D11）：提议 → 不安全拦截 → 前置检查 → 审批暂停 → 恢复 → 回放执行 → 验证 / 回滚。

假模型 + 回放 + 内存存储，不访问网络。录制在 disk_watermark 的基础上补一条 post 阶段（执行 raise_watermark 之后变绿）。
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

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


def approve(h: Harness, decision: str = "approved") -> None:
    from datetime import UTC, datetime

    from sentinel.contracts import ApprovalDecision

    for req in h.rem_store.list_approval_requests():
        h.rem_store.put_decision(
            ApprovalDecision(
                approval_id=req.id,
                decision=decision,
                approver="human:t",
                reason=None,  # type: ignore[arg-type]
                decided_at=datetime.now(UTC),
            )
        )


def test_full_path_resolves(harness: H, tmp_path: Path) -> None:
    h: Harness = harness(fixture(tmp_path), with_plan(GOOD), remediate=True)
    s = h.run("remediate")
    assert "__interrupt__" in s and s["approval_id"]  # 停在 approve 节点等人
    plan = s["plan"]
    assert plan["plan_hash"].startswith("sha256:") and plan["precheck_snapshot"]["nodes_over_high"] == [
        "es-target-es-data-2"
    ]
    assert h.rem_store.get_execution  # 审批之前什么都没执行
    assert not h.rem_store.executions
    approve(h)
    out = h.resume(s["inv_id"])
    assert out["status"] == "resolved" and out["remediation"] == "resolved"
    types = h.audit_types(s["inv_id"])
    i = types.index
    assert i("plan.proposed") < i("approval.requested") < i("verify.passed") < i("investigation.closed")
    assert [e.type for e in h.rem_store.audit] == ["action.executed"]  # 执行器写的审计（同一条哈希链的规则）


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
    assert [e.type for e in h.rem_store.audit] == ["action.executed", "action.rolled_back"]


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
