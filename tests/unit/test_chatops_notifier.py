"""飞书推送（09 §4；M5 D18）：游标、事件过滤、失败重试、审批卡片的按钮规则、卡片和 CLI 内容一致。

离线：MemoryStore + MemoryRemediationStore + 记录调用的假飞书客户端。
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sentinel.chatops.cards import approval_card
from sentinel.chatops.config import ChatopsConfig, load_chatops_config, load_env_file
from sentinel.chatops.fake import FakeFeishuClient
from sentinel.chatops.notifier import NOTIFY_TYPES, Notifier
from sentinel.contracts import ApprovalRequest, RemediationPlan
from sentinel.core.approval_view import ApprovalView, load_approval_view, risk_allowed
from sentinel.core.store import InvestigationRecord, MemoryStore
from sentinel.governance.remediation_store import MemoryRemediationStore

NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _cfg(**kw: Any) -> ChatopsConfig:
    return ChatopsConfig(env_label="kind-dev", chat_id="oc_test", **kw)


def _approval(rem: MemoryRemediationStore, action_id: str = "es.relocate_shards") -> str:
    plan = RemediationPlan(
        id="plan_1",
        investigation_id="inv_1",
        action_id=action_id,
        target="es-target",
        params={"index": "logs-1"},
        rationale_hypothesis="H1",
        precheck_snapshot={"unassigned": 3},
        dry_run_result={"shards": 2},
        rollback_plan=None,
        plan_hash="sha256:abc",
    )
    req = ApprovalRequest(
        id="apv_1", plan_id=plan.id, plan_hash=plan.plan_hash, requested_at=NOW, expires_at=NOW + timedelta(minutes=30)
    )
    assert rem.request_approval_exclusive(plan, req, NOW) is None
    return req.id


def _setup() -> tuple[MemoryStore, MemoryRemediationStore, FakeFeishuClient, Notifier]:
    store = MemoryStore()
    rem = MemoryRemediationStore(audit_sink=store.append_audit, audit_reader=store.audit_events)
    client = FakeFeishuClient()
    return store, rem, client, Notifier(store, rem, client, _cfg())


def _audit(store: MemoryStore, type_: str, payload: dict[str, Any], inv: str | None = "inv_1") -> None:
    store.append_audit(investigation_id=inv, agent_version="v41", actor="agent", type=type_, payload=payload)


def test_first_tick_starts_from_latest_and_sends_nothing() -> None:
    # 第一次启动不把历史事件推出去：上线 notifier 那天，群里不会被几百条旧事件刷屏
    store, _, client, n = _setup()
    _audit(store, "investigation.escalated", {"reason": "old"})
    assert n.tick() == 0
    assert client.sent == []
    assert store.get_notify_cursor("kind-dev") == 1


def test_only_selected_types_and_no_resend() -> None:
    store, rem, client, n = _setup()
    n.tick()  # 初始化游标
    store.create_investigation(
        InvestigationRecord(
            id="inv_1",
            target="es-target",
            mode="report_only",
            agent_version="v41",
            status="diagnosed",
            created_at=NOW,
            findings=[{"detector": "es.unassigned_shards"}],
        )
    )
    _audit(store, "investigation.started", {})
    _audit(store, "patrol.healthy", {})  # 健康不推送（09 §4.1）
    _audit(
        store, "diagnosis.completed", {"hypotheses": [{"id": "H1", "label": "es.disk_watermark", "confidence": 0.8}]}
    )
    apv = _approval(rem)
    _audit(store, "approval.requested", {"approval_id": apv})
    _audit(store, "verify.passed", {"plan_id": "plan_1", "detail": "green"})

    assert n.tick() == 3
    titles = [c["header"]["title"]["content"] for _, c, _ in client.sent]
    assert titles == [
        "[kind-dev] 发现异常：es-target",
        "[kind-dev] 待审批：es.relocate_shards",
        "[kind-dev] 修复验证通过",
    ]
    assert all(chat == "oc_test" for chat, _, _ in client.sent)
    # 幂等键就是审计事件 ID：同一事件重发，飞书只留一条（09 §4.2）
    pushed = [e for e in store.audit if e.type in NOTIFY_TYPES]
    assert [u for _, _, u in client.sent] == [e.id for e in pushed]
    # 游标推到最后一条；再跑一次不重发
    assert store.get_notify_cursor("kind-dev") == store.latest_audit_seq()
    assert n.tick() == 0 and len(client.sent) == 3


def test_failure_keeps_cursor_and_retries() -> None:
    store, _, client, n = _setup()
    n.tick()
    _audit(store, "investigation.escalated", {"reason": "budget"})
    before = store.get_notify_cursor("kind-dev")
    client.fail_next = 1
    assert n.tick() == 0
    assert store.get_notify_cursor("kind-dev") == before  # 没发出去就不推进
    assert n.tick() == 1  # 下一轮重试成功
    assert client.sent[0][1]["header"]["title"]["content"] == "[kind-dev] 需要人接手"


def test_freeze_card_without_investigation() -> None:
    store, _, client, n = _setup()
    n.tick()
    _audit(store, "freeze.enabled", {"reason": "演练"}, inv=None)
    assert n.tick() == 1
    assert "演练" in str(client.sent[0][1])


def _view(rem: MemoryRemediationStore, risk: str) -> ApprovalView:
    v = load_approval_view(rem, _approval(rem))
    assert v is not None
    return ApprovalView(v.request, v.plan, v.spec.model_copy(update={"risk": risk}), v.decision)


def _actions(card: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for el in card["elements"] if el["tag"] == "action" for a in el["actions"]]


def test_buttons_follow_risk_ceiling() -> None:
    rem = MemoryRemediationStore()
    medium = _view(rem, "medium")
    # medium ≤ 默认上限：有按钮，按钮里只带 approval_id 和决定（T3、T4：服务端重新读方案）
    btns = _actions(approval_card(_cfg(), medium))
    assert [b["value"] for b in btns] == [
        {"approval_id": "apv_1", "decision": "approved"},
        {"approval_id": "apv_1", "decision": "rejected"},
    ]
    # high：照样推送、完整显示，但没有按钮，提示 CLI（09 §2 D3）
    high = ApprovalView(medium.request, medium.plan, medium.spec.model_copy(update={"risk": "high"}), None)
    card = approval_card(_cfg(), high)
    assert _actions(card) == []
    assert "sentinel approvals approve apv_1" in str(card) and "飞书审批未开启" in str(card)
    # 打开高风险开关之后才有按钮（09 §2 D4）
    assert len(_actions(approval_card(_cfg(high_risk_enabled=True), high))) == 2
    assert not risk_allowed("critical", "high")  # 未知等级一律不允许


def test_card_shows_everything_cli_shows(monkeypatch: pytest.MonkeyPatch) -> None:
    # 09 §4.3：人不能在信息更少的界面上做批准。CLI 打印的每一个值，卡片上都要有
    from sentinel.cli import main as cli

    rem = MemoryRemediationStore()
    apv = _approval(rem)
    monkeypatch.setattr(cli, "_rem_store", lambda: rem)
    out = CliRunner().invoke(cli.app, ["approvals", "show", apv])
    assert out.exit_code == 0, out.output
    v = load_approval_view(rem, apv)
    assert v is not None
    # 比对卡片上真正显示的文字（markdown 元素），不用 str(dict)：那会把引号转义
    card = "\n".join(el["content"] for el in approval_card(_cfg(), v)["elements"] if el["tag"] == "markdown")
    for label, value in v.lines():
        assert value in out.output and label in out.output
        assert value in card


def test_env_configs() -> None:
    dev = load_chatops_config(ROOT / "config/chatops.dev.yaml")
    prod = load_chatops_config(ROOT / "config/chatops.prod.yaml")
    assert (dev.env_label, prod.env_label) == ("kind-dev", "eks-prod")
    assert "bench" in dev.commands and "bench" not in prod.commands  # 09 §2 D2
    assert dev.max_risk == prod.max_risk == "medium" and not prod.high_risk_enabled


def test_env_file_overrides_stale_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # S7 踩过的坑：会话继承了旧凭证。显式给了文件，就以文件为准
    monkeypatch.setenv("FEISHU_APP_ID", "cli_old")
    f = tmp_path / "feishu.env"
    f.write_text("# dev bot\nexport FEISHU_APP_ID='cli_new'\nFEISHU_CHAT_ID=oc_x\n", encoding="utf-8")
    monkeypatch.delenv("FEISHU_CHAT_ID", raising=False)
    assert load_env_file(f) == ["FEISHU_APP_ID", "FEISHU_CHAT_ID"]
    assert os.environ["FEISHU_APP_ID"] == "cli_new"
    assert ChatopsConfig(env_label="x").resolved_chat_id() == "oc_x"


def test_run_forever_survives_a_failed_round_and_catches_up() -> None:
    # M5 独立审阅发现 1：Postgres 抖一下不能让 notifier 线程永久退出；恢复之后从游标处补发
    store, _, client, n = _setup()
    n.tick()  # 初始化游标
    _audit(store, "investigation.escalated", {"reason": "x"})
    real = store.audit_events_since
    calls = {"n": 0}

    def flaky(after: int, types: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("postgres 重启中")
        return real(after, types)

    store.audit_events_since = flaky  # type: ignore[method-assign]
    n.run_forever(sleep=lambda s: None, rounds=2)
    assert calls["n"] == 2 and len(client.sent) == 1
