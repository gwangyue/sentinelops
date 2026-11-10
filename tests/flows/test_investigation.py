"""02 §1–2 的流程测试：调查图的只读路径。回放真实录制 + 假模型 + 内存存储。"""

import itertools
from collections.abc import Callable
from typing import Any

import pytest

from sentinel.contracts import KNOWN_EVENT_TYPES, Diagnosis
from sentinel.core.diagnosis.evidence import check_evidence, support_rate
from sentinel.core.store import GENESIS_HASH, event_hash
from sentinel.core.versioning.components import load_context_policy
from sentinel.core.versioning.freeze import FreezeError
from tests.flows.scripts import diagnose_script

H = Callable[..., Any]


def test_healthy_patrol_never_calls_model(harness: H) -> None:
    h = harness("healthy")
    s = h.run()
    assert s["status"] == "healthy"
    assert h.audit_types(s["inv_id"]) == ["investigation.started", "patrol.healthy", "investigation.closed"]
    assert h.model.calls == 0  # golden-demo 第 1 步：健康时不调用 LLM


def test_disk_watermark_diagnosed(harness: H) -> None:
    h = harness("disk_watermark", diagnose_script())
    s = h.run()
    inv = s["inv_id"]
    assert s["status"] == "diagnosed"
    types = h.audit_types(inv)
    assert types[0] == "investigation.started" and types[-1] == "investigation.closed"
    assert types.count("finding.detected") == 2 and "diagnosis.completed" in types
    assert set(types) <= KNOWN_EVENT_TYPES

    d = Diagnosis.model_validate(s["diagnosis"])
    assert d.hypotheses[0].root_cause_label == "es.disk_high_watermark"
    assert d.steps == 1 and d.tokens_in == 2000  # 剧本里 1 次工具调用、2 次模型调用
    checks = check_evidence(d, h.obs.list(inv))
    assert support_rate(checks) == 1.0
    # 每次工具调用（检测阶段 + 诊断阶段）都有观测记录，并且都属于这次调查
    assert all(o.investigation_id == inv for o in h.obs.list(inv))


def test_low_confidence_escalates(harness: H) -> None:
    h = harness("disk_watermark", diagnose_script(confidence=0.2))
    s = h.run()
    assert s["status"] == "escalated" and s["escalate_reason"].startswith("low_confidence")
    assert "investigation.escalated" in h.audit_types(s["inv_id"])


def test_fabricated_evidence_is_caught(harness: H) -> None:
    h = harness("disk_watermark", diagnose_script(evidence="obs_999"))
    s = h.run()
    d = Diagnosis.model_validate(s["diagnosis"])
    [c] = check_evidence(d, h.obs.list(s["inv_id"]))
    assert not c.supported and "不存在" in c.reason


def test_fabricated_quote_is_caught(harness: H) -> None:
    h = harness("disk_watermark", diagnose_script(quote="the node is on fire"))
    s = h.run()
    [c] = check_evidence(Diagnosis.model_validate(s["diagnosis"]), h.obs.list(s["inv_id"]))
    assert not c.supported and "quote" in c.reason


def test_unknown_label_becomes_none(harness: H) -> None:
    h = harness("disk_watermark", diagnose_script(label="es.disk_is_full"))
    d = Diagnosis.model_validate(h.run()["diagnosis"])
    assert d.hypotheses[0].root_cause_label is None


def test_token_budget_escalates(harness: H) -> None:
    policy = {**load_context_policy(), "investigation_token_budget": 100}
    h = harness("disk_watermark", diagnose_script(), policy)
    s = h.run()
    assert s["escalate_reason"].startswith("budget")
    assert "budget.exceeded" in h.audit_types(s["inv_id"])


def test_unfrozen_version_refuses_to_start(harness: H) -> None:
    h = harness("healthy")
    h.store.versions.clear()
    with pytest.raises(FreezeError):
        h.run()


def test_audit_hash_chain(harness: H) -> None:
    h = harness("disk_watermark", diagnose_script())
    h.run()
    h.run()
    events = h.store.audit_events()
    assert events[0].prev_hash == GENESIS_HASH
    for prev, cur in itertools.pairwise(events):
        assert cur.prev_hash == event_hash(prev)


def test_context_bundle_keeps_l0_and_abnormal(harness: H) -> None:
    h = harness("disk_watermark", diagnose_script())
    s = h.run()
    items = s["bundle"]["items"]
    assert all(i["included"] for i in items if i["layer"] == "L0")
    assert "node.disk_used_pct" in s["bundle"]["rendered"] and "<untrusted" in s["bundle"]["rendered"]
    budgets = load_context_policy()["budgets"]
    assert s["bundle"]["tokens_by_layer"]["L0"] <= budgets["L0"]
    assert s["bundle"]["tokens_by_layer"]["L1"] <= budgets["L1"]
