"""飞书固定指令（09 §5.3；M5 D20）：白名单、指令白名单、/freeze 开关、/unfreeze、帮助卡片、参数校验、长任务。

离线：假飞书客户端记录发出的卡片；巡检、bench、冻结都换成记录调用的假函数；spawn 同步执行。
"""

from typing import Any

import pytest

from sentinel.chatops.approvals import ApprovalHandler, CardAction
from sentinel.chatops.commands import ArgError, ChatMessage, CommandHandler, parse, route_card
from sentinel.chatops.config import ChatopsConfig, load_chatops_config
from sentinel.chatops.fake import FakeFeishuClient
from sentinel.core.store import MemoryStore
from sentinel.governance.remediation_store import MemoryRemediationStore
from tests.unit.test_chatops_notifier import NOW, ROOT, _approval

ALICE = "ou_alice"
DEV_COMMANDS = ["help", "patrol", "inv", "approvals", "bench", "freeze"]


class Calls:
    def __init__(self) -> None:
        self.patrol: list[tuple[str, str]] = []
        self.bench: list[list[str]] = []
        self.freeze: list[tuple[str, str]] = []


def _setup(
    cfg: ChatopsConfig | None = None, patrol_out: dict[str, Any] | None = None
) -> tuple[CommandHandler, FakeFeishuClient, Calls, MemoryRemediationStore]:
    cfg = cfg or ChatopsConfig(
        env_label="kind-dev", chat_id="oc_test", approvers={ALICE: "alice"}, commands=DEV_COMMANDS
    )
    store = MemoryStore()
    rem = MemoryRemediationStore(audit_sink=store.append_audit, audit_reader=store.audit_events)
    client = FakeFeishuClient()
    calls = Calls()

    def patrol(t: str, p: str) -> dict[str, Any]:
        calls.patrol.append((t, p))
        return patrol_out or {"inv_id": "inv_9", "status": "healthy"}

    def bench(argv: list[str]) -> tuple[int, str]:
        calls.bench.append(argv)
        return 0, "症状在 42 秒后出现"

    def freeze(reason: str, who: str) -> list[str]:
        calls.freeze.append((reason, who))
        return []

    h = CommandHandler(
        cfg,
        client,
        store,
        rem,
        targets={"es-target": "es", "mysql-target": "mysql"},
        scenarios=lambda pack: {"disk_watermark.single"} if pack == "es" else set(),
        patrol=patrol,
        run_bench=bench,
        freeze=freeze,
        spawn=lambda fn: fn(),
        now=lambda: NOW,
    )
    return h, client, calls, rem


def _say(h: CommandHandler, text: str, open_id: str = ALICE, chat: str = "oc_test", rid: str = "om_1") -> None:
    h.on_message(ChatMessage(open_id, chat, rid, text))


def _title(card: dict[str, Any]) -> str:
    return str(card["header"]["title"]["content"])


def _last(client: FakeFeishuClient) -> str:
    return str(client.sent[-1][1])


# ---- 解析 ----


def test_parse_strips_mention_placeholders_and_pack_option() -> None:
    p = parse("@_user_1 /bench inject disk_watermark.single --pack es")
    assert p is not None and (p.name, p.args, p.pack) == ("bench", ["inject", "disk_watermark.single"], "es")
    p = parse("@_user_1  /PATROL es-target --pack=es")
    assert p is not None and (p.name, p.args, p.pack) == ("patrol", ["es-target"], "es")
    assert parse("@_user_1 ") is None  # 只 @ 了机器人
    with pytest.raises(ArgError):
        parse("/patrol es-target --pack")


# ---- 身份和群 ----


def test_not_in_whitelist_is_refused_and_nothing_runs() -> None:
    h, client, calls, _ = _setup()
    _say(h, "/patrol es-target", open_id="ou_mallory")
    assert calls.patrol == [] and "白名单" in _last(client)
    _say(h, "/freeze 停", open_id="ou_mallory", rid="om_2")
    assert calls.freeze == []


def test_other_chat_is_ignored_silently() -> None:
    h, client, calls, _ = _setup()
    _say(h, "/patrol es-target", chat="oc_prod")
    assert client.sent == [] and calls.patrol == []


# ---- 帮助卡片 ----


def test_unknown_input_returns_help_card_with_buttons() -> None:
    h, client, _, _ = _setup()
    _say(h, "@_user_1 帮我看看集群怎么了")
    card = client.sent[-1][1]
    assert _title(card) == "[kind-dev] 指令帮助" and "认不出指令" in str(card)
    buttons = [a for el in card["elements"] if el["tag"] == "action" for a in el["actions"]]
    # 每个目标一个「巡检」，再加「待审批列表」；按钮只带一条指令文本
    assert [b["value"] for b in buttons] == [
        {"cmd": "/patrol es-target"},
        {"cmd": "/patrol mysql-target"},
        {"cmd": "/approvals list"},
    ]
    assert "/bench inject" in str(card)  # dev 列出 bench 的模板


def test_help_on_prod_does_not_list_bench() -> None:
    prod = load_chatops_config(ROOT / "config/chatops.prod.yaml").model_copy(
        update={"chat_id": "oc_test", "approvers": {ALICE: "alice"}}
    )
    h, client, _, _ = _setup(prod)
    _say(h, "/help")
    assert "/bench" not in _last(client) and "[eks-prod]" in _last(client)


# ---- 指令白名单和开关 ----


def test_bench_is_refused_on_prod_config() -> None:
    # 09 §2 D2：注入故障只在 dev；prod 配置的 commands 里没有 bench
    prod = load_chatops_config(ROOT / "config/chatops.prod.yaml").model_copy(
        update={"chat_id": "oc_test", "approvers": {ALICE: "alice"}}
    )
    h, client, calls, _ = _setup(prod)
    _say(h, "/bench inject disk_watermark.single")
    assert calls.bench == [] and "没有开放" in _last(client)


def test_freeze_refused_when_switch_off() -> None:
    h, client, calls, _ = _setup()
    assert not h.cfg.high_risk_enabled
    _say(h, "/freeze 误操作，先停下")
    assert calls.freeze == [] and "high_risk_enabled" in _last(client) and "sentinel freeze" in _last(client)


def test_freeze_allowed_when_switch_on_records_channel_and_human() -> None:
    cfg = ChatopsConfig(
        env_label="kind-dev",
        chat_id="oc_test",
        approvers={ALICE: "alice"},
        commands=DEV_COMMANDS,
        high_risk_enabled=True,
    )
    h, client, calls, _ = _setup(cfg)
    _say(h, "/freeze 误操作 先停下")
    assert calls.freeze == [("[feishu] 误操作 先停下", "human:alice")] and "已冻结" in _last(client)


def test_unfreeze_always_refused_even_with_switch_on() -> None:
    cfg = ChatopsConfig(
        env_label="kind-dev", chat_id="oc_test", approvers={ALICE: "alice"}, commands=[*DEV_COMMANDS, "unfreeze"]
    )
    cfg = cfg.model_copy(update={"high_risk_enabled": True})
    h, client, calls, _ = _setup(cfg)
    _say(h, "/unfreeze")
    assert "飞书不能解冻" in _last(client) and calls.freeze == []


# ---- 参数校验 ----


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        ("/patrol prod-cluster", "不认识的目标"),
        ("/patrol es-target --pack mysql", "属于专家包 es"),
        ("/patrol", "用法"),
        ("/bench inject no_such_scenario", "没有场景"),
        ("/bench nuke disk_watermark.single", "用法"),
        ("/bench inject disk_watermark.single --pack redis", "不认识的专家包"),
        ("/inv show inv_missing", "找不到调查"),
        ("/inv delete inv_1", "用法"),
        ("/approvals approve apv_1", "用法"),
    ],
)
def test_bad_arguments_are_refused_before_anything_runs(text: str, expect: str) -> None:
    h, client, calls, _ = _setup()
    _say(h, text)
    assert expect in _last(client) and "参数不对" in _last(client)
    assert calls.patrol == [] and calls.bench == []


def test_freeze_without_reason_is_refused() -> None:
    cfg = ChatopsConfig(
        env_label="kind-dev",
        chat_id="oc_test",
        approvers={ALICE: "alice"},
        commands=DEV_COMMANDS,
        high_risk_enabled=True,
    )
    h, client, calls, _ = _setup(cfg)
    _say(h, "/freeze")
    assert calls.freeze == [] and "原因" in _last(client)


# ---- 长任务：先回"已开始"，跑完原地更新 ----


def test_patrol_replies_started_then_patches_result() -> None:
    out = {"inv_id": "inv_7", "approval_id": "apv_7", "__interrupt__": ()}
    h, client, calls, _ = _setup(patrol_out=out)
    _say(h, "@_user_1 /patrol es-target")
    assert calls.patrol == [("es-target", "es")]  # 专家包从目标推出来
    assert "进行中" in _title(client.sent[0][1])
    (msg_id, card) = client.patched[-1]
    assert msg_id == "om_0" and "完成" in _title(card) and "apv_7" in str(card)


def test_bench_inject_runs_subprocess_args_and_offers_patrol_button() -> None:
    h, client, calls, _ = _setup()
    _say(h, "/bench inject disk_watermark.single")
    assert calls.bench == [["inject", "disk_watermark.single", "--pack", "es"]]
    _, card = client.patched[-1]
    assert "退出码 0" in str(card) and {"cmd": "/patrol es-target"} in [
        b["value"] for el in card["elements"] if el["tag"] == "action" for b in el["actions"]
    ]


def test_long_task_failure_lands_on_the_card() -> None:
    h, client, _, _ = _setup()

    def boom(t: str, p: str) -> dict[str, Any]:
        raise RuntimeError("冻结中")

    h.patrol = boom
    _say(h, "/patrol es-target")
    assert "失败" in _title(client.patched[-1][1]) and "冻结中" in str(client.patched[-1][1])


# ---- 只读指令 ----


def test_approvals_list_resends_pending_approval_cards() -> None:
    h, client, _, rem = _setup()
    _approval(rem)
    _say(h, "/approvals list")
    assert "待审批 1 个" in str(client.sent[0][1])
    card = client.sent[1][1]
    assert "待审批：es.relocate_shards" in _title(card) and "button" in str(card)
    assert len({u for _, _, u in client.sent}) == 2  # 每张卡片一个 uuid


# ---- 卡片按钮分流 ----


def test_command_button_goes_through_same_checks_as_typed_text() -> None:
    h, client, calls, rem = _setup()
    approvals = ApprovalHandler(rem, lambda **kw: None, h.cfg, resume=lambda inv: {}, spawn=lambda fn: fn())
    r = route_card(CardAction(ALICE, "oc_test", "om_help", {"cmd": "/patrol es-target"}), approvals, h)
    assert r.toast_type == "success" and calls.patrol == [("es-target", "es")]
    # 伪造的按钮：value 里塞一个不存在的目标，照样被参数校验拦下
    route_card(CardAction(ALICE, "oc_test", "om_help", {"cmd": "/patrol prod-db"}), approvals, h)
    assert calls.patrol == [("es-target", "es")] and "不认识的目标" in _last(client)
    # 白名单外的人点按钮：回调里直接拒绝，不进后台
    r = route_card(CardAction("ou_mallory", "oc_test", "om_help", {"cmd": "/patrol es-target"}), approvals, h)
    assert r.toast_type == "error" and len(calls.patrol) == 1
