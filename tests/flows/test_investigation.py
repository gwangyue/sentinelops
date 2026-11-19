"""02 §1–2 的流程测试：调查图的只读路径。回放真实录制 + 假模型 + 内存存储。"""

import dataclasses
import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sentinel.contracts import KNOWN_EVENT_TYPES, Claim, Diagnosis
from sentinel.core.diagnosis.evidence import check_claim, check_evidence, support_rate
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


def test_quote_with_wrapper_status_prefix(harness: H) -> None:
    # 模型看到的工具返回是 "[ok] 摘要"，照提示词逐字复制会带上前缀（教程 03 §3.8，v36 实测 21 条）
    h = harness("disk_watermark", diagnose_script())
    s = h.run()
    o = next(o for o in h.obs.list(s["inv_id"]) if o.tool == "es.allocation_explain")
    first = o.digest.splitlines()[0]

    def check(quote: str) -> bool:
        return check_claim(Claim(id="c", text="-", evidence=[o.id], quote=quote), {o.id: o}, s["inv_id"])[0]

    assert check(f"[ok] {first}")  # 和观测的真实状态一致的前缀：去掉后比对
    assert not check(f"[error] {first}")  # 状态对不上：不去掉
    assert not check("[ok] the node is on fire")  # 去掉前缀后仍然是编造的


def test_unknown_label_becomes_none(harness: H) -> None:
    h = harness("disk_watermark", diagnose_script(label="es.disk_is_full"))
    d = Diagnosis.model_validate(h.run()["diagnosis"])
    assert d.hypotheses[0].root_cause_label is None


def test_token_budget_aborts_mid_run(harness: H) -> None:
    # 假模型每次调用记 1100 token：第 1 次 1100 < 1500，第 2 次累计 2200 > 1500 → 立刻中止，不等诊断结束
    policy = {**load_context_policy(), "investigation_token_budget": 1500}
    h = harness("disk_watermark", diagnose_script(), policy)
    s = h.run()
    assert h.model.calls == 2
    assert s["status"] == "escalated" and s["escalate_reason"].startswith("budget")
    assert s["diagnosis"] is None
    types = h.audit_types(s["inv_id"])
    assert "budget.exceeded" in types and "diagnosis.completed" not in types


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


def test_same_fingerprint_within_30_minutes_is_deduplicated(harness: H) -> None:
    # 02 §2：同一份录制连续巡检两次，第二次的问题事件指纹和第一次相同 → 不进入诊断、不调用模型
    h = harness("disk_watermark", diagnose_script())
    first = h.run()
    calls = h.model.calls
    second = h.run()
    assert first["status"] == "diagnosed" and second["status"] == "deduplicated"
    assert h.model.calls == calls
    types = h.audit_types(second["inv_id"])
    assert types.count("finding.deduplicated") == 2 and "diagnosis.completed" not in types


def test_dedup_window_does_not_slide_on_deduplicated_runs(harness: H) -> None:
    # 每 15 分钟巡检一次、问题一直在：第 1 次诊断；第 2 次（25 分钟后）重复；
    # 第 3 次时第 1 次已出窗口（40 分钟前），第 2 次只是一次"判为重复"的调查，不能把窗口往后推 → 应重新诊断。
    # 修之前被判重复的调查也算数，持续存在的问题只诊断一次（教程 03 §3.3 实验 E）。
    h = harness("disk_watermark", diagnose_script())

    def age(minutes_ago: dict[str, int]) -> None:
        now = datetime.now(UTC)
        for inv, m in minutes_ago.items():
            h.store.invs[inv] = dataclasses.replace(h.store.invs[inv], created_at=now - timedelta(minutes=m))

    first = h.run()
    age({first["inv_id"]: 25})
    second = h.run()
    age({first["inv_id"]: 40, second["inv_id"]: 15})
    third = h.run()
    assert [first["status"], second["status"], third["status"]] == ["diagnosed", "deduplicated", "diagnosed"]
