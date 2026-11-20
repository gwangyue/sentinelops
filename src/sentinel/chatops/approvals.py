"""飞书卡片审批（09 §5.1–5.2；M5 D19）：审批卡片上的「批准」「拒绝」被点之后做什么。

负责：只处理配置里那个群的点击 → 白名单（open_id → human:<名字>）→ 只从按钮里取 approval_id 和决定
      → decide_approval（和 CLI 同一个函数，风险上限取配置）→ 立刻返回更新后的卡片 → 后台恢复调查。
不负责：
- 收发飞书消息（gateway.py 把 lark 的回调翻译成 CardAction，再把 CardReply 翻译回去）；
- 审批规则本身（core/approval_view.py、core/graph/remediate.py）；执行（独立执行器自己读 Postgres）；
- 执行和验证的结果：不在这里等，由 notifier 按审计事件推送（verify.passed / verify.failed）。

为什么恢复要放后台：飞书回调超过约 3 秒就判定超时，而且不重发（S7 Q5），而 resume_online 最多阻塞 300 秒。
这里先写决定（一次 Postgres 写入，毫秒级），马上把卡片改成"已批准，执行中"，恢复调查交给 spawn。
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from sentinel.chatops.cards import Card, approval_card
from sentinel.chatops.config import ChatopsConfig
from sentinel.contracts import ActionSpec
from sentinel.core.approval_view import decide_approval, resume_summary, spec_from_pack
from sentinel.governance.remediation_store import RemediationStore

log = logging.getLogger(__name__)

CHANNEL = "[feishu]"  # 写在 approval.decided 的 reason 前面，区分渠道；不改契约（ADR-015 第 3 条）


@dataclass(frozen=True)
class CardAction:
    """从飞书卡片回调里取出的四样东西。value 来自客户端，不可信：只用里面的 approval_id 和 decision。"""

    open_id: str
    chat_id: str | None
    message_id: str | None
    value: dict[str, Any]


@dataclass(frozen=True)
class CardReply:
    """回给飞书的东西：一条 toast，和（可选）原地替换的新卡片。card 为 None 表示卡片不动。"""

    toast: str
    toast_type: Literal["success", "info", "warning", "error"]
    card: Card | None = None


def _in_thread(fn: Callable[[], None]) -> None:
    # 不设 daemon：进程收到退出信号时，正在恢复的调查要跑完，不能半路被杀掉
    threading.Thread(target=fn, name="chatops-resume").start()


@dataclass
class ApprovalHandler:
    rem: RemediationStore
    audit: Callable[..., Any]
    cfg: ChatopsConfig
    resume: Callable[[str], Any]  # 调查 ID → 恢复之后的状态。线上是 resume_online，测试和 demo 是 Harness.resume
    spawn: Callable[[Callable[[], None]], None] = _in_thread  # 测试里换成同步执行
    spec_for: Callable[[str], ActionSpec] = spec_from_pack

    def on_card_action(self, a: CardAction) -> CardReply:
        if a.chat_id != self.cfg.resolved_chat_id():
            # 09 §6：别的群一律忽略。dev 和 prod 是两个应用，正常不会出现；出现了说明配置串了（T14）
            log.warning("忽略来自其他群的卡片点击：chat=%s open_id=%s", a.chat_id, a.open_id)
            return CardReply("这个群不归本环境管", "error")
        name = self.cfg.approvers.get(a.open_id)
        if name is None:
            log.warning("白名单外的审批点击：open_id=%s value=%s", a.open_id, a.value)  # T13
            return CardReply("你不在审批人名单里，这次点击没有生效", "error")
        approval_id, decision = a.value.get("approval_id"), a.value.get("decision")
        if not isinstance(approval_id, str) or decision not in ("approved", "rejected"):
            log.warning("按钮内容不对：open_id=%s value=%s", a.open_id, a.value)
            return CardReply("按钮内容不对，没有生效", "error")

        who = f"human:{name}"  # 和 CLI 的 --as 同一种格式（09 §5.1）
        r = decide_approval(
            self.rem,
            self.audit,
            approval_id,
            decision,
            who,
            CHANNEL,
            max_risk=self.cfg.max_risk,
            spec_for=self.spec_for,
        )
        if r.outcome == "not_found":
            return CardReply(f"没有审批请求 {approval_id}", "error")
        if r.outcome == "risk_too_high":
            # 高于上限的卡片本来就没有按钮：走到这里说明按钮是伪造的（09 §5.2 第 2 步）
            log.warning("高于飞书风险上限的审批点击（按钮可能被伪造）：%s open_id=%s", approval_id, a.open_id)
            return CardReply(f"这个动作的风险高于飞书上限 {self.cfg.max_risk}，请到 CLI 审批", "error")
        assert r.view is not None  # not_found 之外的结果都带着 view
        if r.outcome == "duplicate":
            prev = r.view.decision
            by = prev.approver if prev else "别人"
            return CardReply(f"已被 {by} 决定过", "warning", approval_card(self.cfg, r.view, "，这次点击没有生效"))

        inv = r.view.plan.investigation_id
        self.spawn(lambda: self._resume(inv, approval_id))
        word = "执行中，结果会另发一张卡片" if decision == "approved" else "调查将以只出报告结束"
        return CardReply("已记录", "success", approval_card(self.cfg, r.view, f"，{word}"))

    def _resume(self, inv: str, approval_id: str) -> None:
        try:
            out = self.resume(inv)
        except Exception:  # 后台线程：异常不能丢，只能记日志
            # TODO(edge)：恢复失败时把卡片改成"恢复失败，请用 CLI 处理"；现在只有日志，卡片停在"执行中"
            log.exception("审批 %s 之后恢复调查 %s 失败", approval_id, inv)
            return
        log.info("审批 %s 之后调查 %s 恢复完成：%s", approval_id, inv, resume_summary(out))
