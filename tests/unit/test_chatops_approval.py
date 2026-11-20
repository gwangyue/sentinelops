"""飞书卡片审批（09 §5.1–5.2；M5 D19）：白名单、风险上限、伪造按钮、重复点击、换群、后台恢复。

离线：MemoryStore + MemoryRemediationStore；resume 用一个记录调用的假函数，spawn 同步执行。
"""

from collections.abc import Callable
from typing import Any

from sentinel.chatops.approvals import CHANNEL, ApprovalHandler, CardAction
from sentinel.chatops.cards import approval_card
from sentinel.chatops.config import ApprovalPolicy, ChatopsConfig
from sentinel.core.approval_view import decide_approval, load_approval_view
from sentinel.core.store import MemoryStore
from sentinel.governance.remediation_store import MemoryRemediationStore
from tests.unit.test_chatops_notifier import _approval

ALICE = "ou_alice"


def _setup(
    max_risk: str = "medium", resume: Callable[[str], Any] | None = None
) -> tuple[MemoryStore, MemoryRemediationStore, ApprovalHandler, list[str]]:
    store = MemoryStore()
    rem = MemoryRemediationStore(audit_sink=store.append_audit, audit_reader=store.audit_events)
    _approval(rem)  # es.relocate_shards，风险 medium
    resumed: list[str] = []
    cfg = ChatopsConfig(
        env_label="kind-dev",
        chat_id="oc_test",
        approvers={ALICE: "alice"},
        approval=ApprovalPolicy(max_risk_via_chat=max_risk),  # type: ignore[arg-type]
    )
    h = ApprovalHandler(
        rem, store.append_audit, cfg, resume=resume or (lambda inv: resumed.append(inv) or {}), spawn=lambda fn: fn()
    )
    return store, rem, h, resumed


def _click(open_id: str = ALICE, decision: str = "approved", chat: str = "oc_test", **value: Any) -> CardAction:
    return CardAction(open_id, chat, "om_1", {"approval_id": "apv_1", "decision": decision, **value})


def _decided(store: MemoryStore) -> list[Any]:
    return [e for e in store.audit_events() if e.type == "approval.decided"]


def test_approve_records_human_name_with_feishu_reason_and_resumes() -> None:
    store, rem, h, resumed = _setup()
    r = h.on_card_action(_click())
    d = rem.get_decision("apv_1")
    assert d is not None and d.approver == "human:alice" and d.decision == "approved"
    (e,) = _decided(store)
    assert e.actor == "human:alice" and e.payload["reason"].startswith(CHANNEL)
    assert resumed == ["inv_1"]  # 恢复的是这个审批所属的调查
    # 卡片原地更新成"已批准，执行中"，按钮去掉
    assert r.toast_type == "success" and r.card is not None
    text = str(r.card)
    assert "已批准" in text and "执行中" in text and "button" not in text


def test_reject_also_resumes_so_the_investigation_closes() -> None:
    # 和 CLI 一样：拒绝之后也要恢复，调查才会以"只出报告"结束，而不是一直停在审批
    _, rem, h, resumed = _setup()
    r = h.on_card_action(_click(decision="rejected"))
    assert rem.get_decision("apv_1").decision == "rejected"  # type: ignore[union-attr]
    assert resumed == ["inv_1"] and "已拒绝" in str(r.card)


def test_not_in_whitelist_is_refused_and_nothing_is_written() -> None:
    store, rem, h, resumed = _setup()
    r = h.on_card_action(_click(open_id="ou_mallory"))
    assert r.toast_type == "error" and r.card is None
    assert rem.get_decision("apv_1") is None and _decided(store) == [] and resumed == []


def test_other_chat_is_ignored() -> None:
    # 09 §6：只接收配置里那个群；别的群来的点击说明配置串了（T14）
    _, rem, h, resumed = _setup()
    r = h.on_card_action(_click(chat="oc_prod"))
    assert r.toast_type == "error" and rem.get_decision("apv_1") is None and resumed == []


def test_forged_button_above_risk_cap_is_refused() -> None:
    # 上限 low：medium 的卡片本来没有按钮；有人伪造了回调，服务端照样拒绝（09 §5.2 第 2 步）
    _, rem, h, resumed = _setup(max_risk="low")
    v = load_approval_view(rem, "apv_1")
    assert v is not None and "button" not in str(approval_card(h.cfg, v))
    r = h.on_card_action(_click())
    assert r.toast_type == "error" and "CLI" in r.toast
    assert rem.get_decision("apv_1") is None and resumed == []


def test_button_value_is_not_trusted_beyond_id_and_decision() -> None:
    # 卡片里塞一个假哈希：决定和审计里的哈希仍然来自存储（T3、T4）
    store, _, h, _ = _setup()
    h.on_card_action(_click(plan_hash="sha256:forged"))
    (e,) = _decided(store)
    assert e.payload["plan_hash"] == "sha256:abc"


def test_bad_button_value_is_refused() -> None:
    _, rem, h, _ = _setup()
    assert h.on_card_action(_click(decision="maybe")).toast_type == "error"
    assert h.on_card_action(CardAction(ALICE, "oc_test", "om_1", {})).toast_type == "error"
    assert rem.get_decision("apv_1") is None


def test_second_click_says_already_decided_and_does_not_resume_again() -> None:
    _, _, h, resumed = _setup()
    h.on_card_action(_click())
    r = h.on_card_action(_click(decision="rejected"))
    assert r.toast_type == "warning" and "human:alice" in r.toast
    assert resumed == ["inv_1"]
    assert "button" not in str(r.card)


def test_decided_from_cli_then_clicked_in_feishu() -> None:
    # CLI 先批了（同一个 decide_approval），飞书上再点：同样返回"已被决定过"
    store, rem, h, resumed = _setup()
    assert decide_approval(rem, store.append_audit, "apv_1", "approved", "human:bob", None).ok
    r = h.on_card_action(_click())
    assert r.toast_type == "warning" and "human:bob" in r.toast and resumed == []


def test_unknown_approval() -> None:
    _, _, h, _ = _setup()
    r = h.on_card_action(CardAction(ALICE, "oc_test", "om_1", {"approval_id": "apv_x", "decision": "approved"}))
    assert r.toast_type == "error" and "apv_x" in r.toast


def test_resume_failure_is_logged_not_raised() -> None:
    # 后台线程里的异常不能把回调拖垮：决定已经写了，卡片照常返回
    def boom(inv: str) -> None:
        raise RuntimeError("pg down")

    _, rem, h, _ = _setup(resume=boom)
    r = h.on_card_action(_click())
    assert r.toast_type == "success" and rem.get_decision("apv_1") is not None


def test_cli_has_no_risk_cap() -> None:
    # CLI 走同一个函数，默认上限 high：飞书上限 low 时，CLI 照样能批 medium
    store, rem, _, _ = _setup(max_risk="low")
    assert decide_approval(rem, store.append_audit, "apv_1", "approved", "human:bob", None).ok
