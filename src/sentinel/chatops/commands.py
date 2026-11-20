"""飞书固定指令（09 §5.3；M5 D20）：群里的一条消息，或帮助卡片上的一个按钮，变成对现有函数的一次调用。

负责：确定性解析（不调 LLM）→ 只收配置里那个群 → 白名单 → 指令白名单（配置的 commands）→ 逐个校验参数
      → 调用注入进来的函数 → 回卡片。长任务（patrol、bench）先回"已开始"，跑完原地更新这张卡片。
不负责：
- lark SDK 对象的拆装（gateway.py）；审批按钮（approvals.py）；
- 巡检、bench、冻结本身：都是注入进来的函数（线上由 CLI 接到 core/graph/run.py、`sentinel bench` 子进程、
  governance/kill_switch.py），这里只决定"能不能调、参数对不对"。

为什么不做自然语言（09 §2 D5）：多一次 LLM 调用就多一条"LLM 决定去注入故障"的路径，要重新论证评测和安全。
认不出来的输入一律回帮助卡片。
"""

import hashlib
import logging
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sentinel.chatops.approvals import ApprovalHandler, CardAction, CardReply
from sentinel.chatops.cards import Card, approval_card, cmd_button, help_card, text_card
from sentinel.chatops.client import FeishuClient
from sentinel.chatops.config import ChatopsConfig
from sentinel.contracts import ActionSpec, Diagnosis
from sentinel.core.approval_view import load_approval_view, spec_from_pack
from sentinel.core.store import Store
from sentinel.governance.remediation_store import RemediationStore

log = logging.getLogger(__name__)

CHANNEL = "[feishu]"
_MENTION = re.compile(r"@_user_\d+")  # 消息文本里的 @ 是占位符（S7），先去掉再解析
BENCH_OPS = ("inject", "revert", "record")


@dataclass(frozen=True)
class ChatMessage:
    """一条指令：群消息里的文本，或帮助卡片按钮 value 里的 cmd。

    request_id 用来生成飞书发送接口的 uuid（幂等键）：消息用 message_id，同一条消息重复投递只回一次卡片；
    按钮每次点击由 gateway 生成一个新的，连点两次就是两次巡检（和手打两次一样）。
    """

    open_id: str
    chat_id: str | None
    request_id: str
    text: str


@dataclass(frozen=True)
class Parsed:
    name: str  # 第一个词，去掉开头的 /，小写
    args: list[str]  # 除 --pack 之外的参数
    pack: str | None  # --pack X / --pack=X
    rest: str  # 指令名之后的原文（/freeze 的原因可以带空格）


class ArgError(ValueError):
    """参数校验失败：回一张带模板的错误卡片。"""


def parse(text: str) -> Parsed | None:
    """确定性解析。空消息返回 None（只 @ 了机器人）。"""
    clean = _MENTION.sub(" ", text).strip()
    if not clean:
        return None
    head, _, rest = clean.partition(" ")
    tokens = rest.split()
    args: list[str] = []
    pack: str | None = None
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--pack":
            if i + 1 >= len(tokens):
                raise ArgError("--pack 后面要跟专家包名")
            pack, i = tokens[i + 1], i + 2
            continue
        if t.startswith("--pack="):
            pack = t.split("=", 1)[1]
        else:
            args.append(t)
        i += 1
    return Parsed(head.removeprefix("/").lower(), args, pack, rest.strip())


def _uuid(request_id: str, part: str) -> str:
    # 飞书 uuid 最长 50 个字符；一次指令可能发多张卡片（/approvals list），每张一个
    return "c" + hashlib.sha1(f"{request_id}:{part}".encode()).hexdigest()[:40]


def _in_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="chatops-command").start()


@dataclass
class CommandHandler:
    cfg: ChatopsConfig
    client: FeishuClient
    store: Store
    rem: RemediationStore
    targets: dict[str, str]  # 目标 → 专家包（config/governance.yaml 的 mcp.targets）
    scenarios: Callable[[str], set[str]]  # 专家包 → 场景 ID
    patrol: Callable[[str, str], dict[str, Any]]  # (target, pack) → 调查状态（core/graph/run.py: patrol 的返回）
    run_bench: Callable[[list[str]], tuple[int, str]]  # `sentinel bench` 的参数 → (退出码, 输出)
    freeze: Callable[[str, str], list[str]]  # (原因, 操作人) → 作废的审批 ID
    spawn: Callable[[Callable[[], None]], None] = _in_thread  # 测试里换成同步执行
    spec_for: Callable[[str], ActionSpec] = spec_from_pack
    now: Callable[[], datetime] = lambda: datetime.now(UTC)  # 判断审批有没有过期；测试里固定
    _handlers: dict[str, Callable[[ChatMessage, Parsed, str], None]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._handlers = {
            "help": self._help_cmd,
            "patrol": self._patrol,
            "inv": self._inv,
            "approvals": self._approvals,
            "bench": self._bench,
            "freeze": self._freeze,
        }

    # ---- 入口 ----

    def on_message(self, m: ChatMessage) -> None:
        if m.chat_id != self.cfg.resolved_chat_id():
            # 09 §6：别的群一律忽略，也不回话（T14）
            log.warning("忽略来自其他群的指令：chat=%s open_id=%s", m.chat_id, m.open_id)
            return
        name = self.cfg.approvers.get(m.open_id)
        if name is None:
            log.warning("白名单外的人发指令：open_id=%s text=%r", m.open_id, m.text)  # T13
            self._send(m, "main", self._refuse("你不在本环境的白名单里，指令没有执行"))
            return
        who = f"human:{name}"
        try:
            p = parse(m.text)
        except ArgError as e:
            self._send(m, "main", self._help(f"⚠️ {e}"))
            return
        if p is None or (p.name not in self._handlers and p.name != "unfreeze"):
            # 09 §2 D5：认不出来的输入回帮助卡片
            self._send(m, "main", self._help(None if p is None else f"认不出指令 `{p.name}`，可用的指令如下："))
            return
        if p.name == "unfreeze":
            # 09 §2 D4：紧急停止要方便，恢复要谨慎——飞书永远不能解冻
            self._send(m, "main", self._refuse("飞书不能解冻，请到 CLI：`sentinel unfreeze --reason ... --as <名字>`"))
            return
        if p.name != "help" and p.name not in self.cfg.commands:
            log.warning("本环境没开放的指令：%s open_id=%s", p.name, m.open_id)
            self._send(m, "main", self._refuse(f"本环境（{self.cfg.env_label}）没有开放 `/{p.name}`"))
            return
        log.info("指令 %r 来自 %s", m.text, who)
        try:
            self._handlers[p.name](m, p, who)
        except ArgError as e:
            self._send(m, "main", self._refuse(str(e), "参数不对"))

    def on_button(self, a: CardAction) -> CardReply:
        """帮助卡片上的按钮。回调要在约 3 秒内返回（S7），所以只做最快的检查，指令本身放后台跑。"""
        cmd = a.value.get("cmd")
        if a.chat_id != self.cfg.resolved_chat_id():
            log.warning("忽略来自其他群的按钮：chat=%s open_id=%s", a.chat_id, a.open_id)
            return CardReply("这个群不归本环境管", "error")
        if a.open_id not in self.cfg.approvers:
            log.warning("白名单外的人点指令按钮：open_id=%s value=%s", a.open_id, a.value)
            return CardReply("你不在本环境的白名单里，没有执行", "error")
        if not isinstance(cmd, str):
            return CardReply("按钮内容不对，没有执行", "error")
        # 按钮 value 来自客户端、不可信：交给和手打一样的 on_message，那里会重新做全部检查
        rid = f"btn-{uuid.uuid4().hex}"
        self.spawn(lambda: self.on_message(ChatMessage(a.open_id, a.chat_id, rid, cmd)))
        return CardReply(f"收到：{cmd}", "success")

    # ---- 各条指令 ----

    def _help_cmd(self, m: ChatMessage, p: Parsed, who: str) -> None:
        self._send(m, "main", self._help())

    def _patrol(self, m: ChatMessage, p: Parsed, who: str) -> None:
        if len(p.args) != 1:
            raise ArgError("用法：`/patrol <target> [--pack es]`")
        target = self._target(p.args[0])
        pack = self._pack(target, p.pack)
        started = f"巡检 `{target}`（{pack}）已开始；有问题会诊断并提议修复，审批卡片另发"
        self._long_task(m, f"巡检 {target}", started, lambda: self._patrol_result(target, pack))

    def _patrol_result(self, target: str, pack: str) -> tuple[str, str, list[dict[str, Any]]]:
        s = self.patrol(target, pack)
        inv = s.get("inv_id", "?")
        if "__interrupt__" in s:  # 停在审批：图还没走到 report，状态里没有 status
            return "green", f"调查 `{inv}` 停在审批：修复方案 `{s.get('approval_id')}`，审批卡片另发", []
        status = s.get("status", "?")
        if status == "healthy":
            # 09 §4.1：patrol.healthy 不主动推送，只在回复 /patrol 时返回
            return "green", f"健康，0 个问题  `{inv}`", []
        n = len(s.get("findings", []))
        return "orange", f"调查 `{inv}` 状态 {status}，问题 {n} 个\n`/inv show {inv}`", []

    def _inv(self, m: ChatMessage, p: Parsed, who: str) -> None:
        sub = p.args[0] if p.args else ""
        if sub == "list" and len(p.args) == 1:
            rows = [
                f"`{r.id}`  {r.created_at:%m-%d %H:%M}  {r.target}  {r.status}  问题 {len(r.findings)}"
                for r in self.store.list_investigations(limit=10)
            ]
            self._send(m, "main", text_card(self.cfg, "最近的调查", "\n".join(rows) or "还没有调查"))
            return
        if sub == "show" and len(p.args) == 2:
            r = self.store.get_investigation(p.args[1])
            if r is None:
                raise ArgError(f"找不到调查 `{p.args[1]}`")
            lines = [f"**集群** `{r.target}` · **状态** {r.status} · **模式** {r.mode}"]
            lines += [f"问题事件  {f['detector']}  {f['severity']}  {f['summary']}" for f in r.findings]
            if r.diagnosis is not None:
                # 只放假设的摘要，不贴原始观测和证据引文（09 §8 T15）
                for h in Diagnosis.model_validate(r.diagnosis).hypotheses:
                    lines.append(f"**{h.id}** {h.confidence:.2f} [{h.root_cause_label or '未归类'}] {h.root_cause}")
            if r.escalate_reason:
                lines.append(f"已升级给人：{r.escalate_reason}")
            lines.append(f"证据详情：CLI `sentinel investigations show {r.id}`")
            self._send(m, "main", text_card(self.cfg, f"调查 {r.id}", "\n".join(lines)))
            return
        raise ArgError("用法：`/inv list` 或 `/inv show <调查ID>`")

    def _approvals(self, m: ChatMessage, p: Parsed, who: str) -> None:
        if p.args != ["list"]:
            raise ArgError("用法：`/approvals list`")
        now = self.now()
        pending = [
            r for r in self.rem.list_approval_requests() if self.rem.get_decision(r.id) is None and now <= r.expires_at
        ]
        self._send(m, "main", text_card(self.cfg, "待审批列表", f"待审批 {len(pending)} 个，每个重新发一张审批卡片"))
        for r in pending:
            v = load_approval_view(self.rem, r.id, self.spec_for)
            if v is not None:
                # 和推送的审批卡片同一个模板：按钮、风险上限、内容都一样（09 §4.3）
                self._send(m, r.id, approval_card(self.cfg, v))

    def _bench(self, m: ChatMessage, p: Parsed, who: str) -> None:
        if len(p.args) != 2 or p.args[0] not in BENCH_OPS:
            raise ArgError("用法：`/bench inject|revert|record <scenario> [--pack es]`")
        op, scenario = p.args
        pack = p.pack or "es"
        if pack not in set(self.targets.values()):
            raise ArgError(f"不认识的专家包 `{pack}`，可选：{', '.join(sorted(set(self.targets.values())))}")
        if scenario not in self.scenarios(pack):
            raise ArgError(f"专家包 {pack} 里没有场景 `{scenario}`")
        argv = [op, scenario, "--pack", pack]
        self._long_task(
            m,
            f"bench {op}",
            f"`sentinel bench {' '.join(argv)}` 已开始，跑完更新这张卡片",
            lambda: self._bench_result(argv),
        )

    def _bench_result(self, argv: list[str]) -> tuple[str, str, list[dict[str, Any]]]:
        rc, out = self.run_bench(argv)
        tail = "\n".join(out.strip().splitlines()[-12:])
        body = f"`sentinel bench {' '.join(argv)}` 退出码 {rc}\n```\n{tail}\n```"
        if rc != 0:
            return "red", body, []
        buttons = []
        if argv[0] == "inject":
            # 接上整条链路：注入之后下一步就是巡检（golden-demo 第 2 → 3 步）
            pack = argv[3]
            buttons = [
                cmd_button(f"巡检 {t}", f"/patrol {t}", "primary") for t, pk in self.targets.items() if pk == pack
            ]
        return "green", body, buttons

    def _freeze(self, m: ChatMessage, p: Parsed, who: str) -> None:
        if not self.cfg.high_risk_enabled:
            # 09 §2 D4：/freeze 和"高风险授权"共用一个开关，默认关
            log.warning("开关关闭时的 /freeze：%s", who)
            self._send(
                m,
                "main",
                self._refuse(
                    "本环境没有开启 high_risk_enabled，飞书不能冻结。"
                    "请到 CLI：`sentinel freeze --reason ... --as <名字>`"
                ),
            )
            return
        if not p.rest:
            raise ArgError("用法：`/freeze <原因>`，原因会写进审计")
        voided = self.freeze(f"{CHANNEL} {p.rest}", who)
        self._send(
            m,
            "main",
            text_card(self.cfg, "已冻结", f"执行器拒绝所有动作；作废待审批 {len(voided)} 个\n解冻只能在 CLI", "red"),
        )

    # ---- 共用 ----

    def _target(self, t: str) -> str:
        if t not in self.targets:
            raise ArgError(f"不认识的目标 `{t}`，可选：{', '.join(sorted(self.targets))}")
        return t

    def _pack(self, target: str, pack: str | None) -> str:
        expected = self.targets[target]
        if pack is not None and pack != expected:
            raise ArgError(f"目标 `{target}` 属于专家包 {expected}，不是 {pack}")
        return expected

    def _long_task(
        self, m: ChatMessage, title: str, started: str, run: Callable[[], tuple[str, str, list[dict[str, Any]]]]
    ) -> None:
        """09 §5.3：先回"已开始"，跑完原地更新。发"已开始"失败就不跑——用户看不到结果的任务不该开始。"""
        msg_id = self._send(m, "main", text_card(self.cfg, f"{title}：进行中", started, "wathet"))
        if msg_id is None:
            return

        def work() -> None:
            try:
                template, body, buttons = run()
                word = "完成" if template == "green" else "结束"
            except Exception as e:  # 后台线程：异常只能落在卡片和日志上
                log.exception("%s 失败", title)
                template, body, buttons, word = "red", f"失败：{e}", [], "失败"
            try:
                self.client.patch_card(msg_id, text_card(self.cfg, f"{title}：{word}", body, template, buttons))
            except Exception:
                # TODO(edge)：更新卡片失败时补发一张新卡片；现在只有日志
                log.exception("更新卡片 %s 失败", msg_id)

        self.spawn(work)

    def _send(self, m: ChatMessage, part: str, card: Card) -> str | None:
        try:
            return self.client.send_card(self.cfg.resolved_chat_id(), card, uuid=_uuid(m.request_id, part))
        except Exception:  # 网络、限流：只记日志；指令没有回音，用户可以再发一次
            log.exception("回复指令失败：%r", m.text)
            return None

    def _help(self, note: str | None = None) -> Card:
        return help_card(self.cfg, sorted(self.targets), note)

    def _refuse(self, text: str, title: str = "已拒绝") -> Card:
        return text_card(self.cfg, title, text, "grey")


def route_card(a: CardAction, approvals: ApprovalHandler, commands: CommandHandler) -> CardReply:
    """卡片回调分流：value 里带 cmd 的是帮助卡片的指令按钮，其余是审批按钮（只认 approval_id + decision）。"""
    if "cmd" in a.value:
        return commands.on_button(a)
    return approvals.on_card_action(a)
