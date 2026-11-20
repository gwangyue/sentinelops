"""推送（09 §4）：按 seq 游标读审计表，把少数几类事件转成卡片发到飞书群。

负责：选事件、渲染、发送、推进游标。
不负责：任何调查逻辑。图的节点一行都不改——飞书挂了，调查照常进行，恢复之后从游标处补发。

投递语义：发送成功才推进游标（至少一次）。同一事件可能发两次（例如发送成功、写游标前进程崩溃），
用审计事件 ID 当飞书的 uuid 去重，所以群里只会出现一条（09 §4.2）。
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from sentinel.chatops.cards import Card, anomaly_card, approval_card, event_card
from sentinel.chatops.client import FeishuClient
from sentinel.chatops.config import ChatopsConfig
from sentinel.contracts import AuditEvent
from sentinel.core.approval_view import load_approval_view
from sentinel.core.store import Store
from sentinel.governance.remediation_store import RemediationStore

log = logging.getLogger(__name__)

# 09 §4.1。patrol.healthy 故意不在里面：健康是常态，推了就是噪音
NOTIFY_TYPES = frozenset(
    {
        "approval.requested",
        "diagnosis.completed",
        "investigation.escalated",
        "verify.passed",
        "verify.failed",
        "action.rolled_back",
        "freeze.enabled",
    }
)


@dataclass
class Notifier:
    store: Store
    rem: RemediationStore
    client: FeishuClient
    cfg: ChatopsConfig

    def render(self, e: AuditEvent) -> Card | None:
        if e.type == "approval.requested":
            v = load_approval_view(self.rem, e.payload["approval_id"])
            # TODO(edge)：审批请求读不到（理论上不会：方案和审批在同一个事务里写入）时只跳过，不告警
            return approval_card(self.cfg, v) if v else None
        if e.type == "diagnosis.completed":
            rec = self.store.get_investigation(e.investigation_id) if e.investigation_id else None
            return anomaly_card(self.cfg, e, rec)
        return event_card(self.cfg, e)

    def tick(self) -> int:
        """处理一批新事件，返回发出去的卡片数。发送失败就停在这条，下一轮从这条重试。"""
        env = self.cfg.env_label
        cursor = self.store.get_notify_cursor(env)
        if cursor is None:
            # 第一次启动：从当前最新的事件开始，不把历史事件一股脑推出去
            cursor = self.store.latest_audit_seq()
            self.store.set_notify_cursor(env, cursor)
            log.info("推送游标初始化为 %s（%s）", cursor, env)
            return 0
        chat_id = self.cfg.resolved_chat_id()
        sent = 0
        for seq, e in self.store.audit_events_since(cursor, NOTIFY_TYPES):
            card = self.render(e)
            if card is not None:
                try:
                    self.client.send_card(chat_id, card, uuid=e.id)
                except Exception:  # 网络、限流、飞书故障：只记日志，不影响调查；下一轮重试
                    log.exception("推送失败，停在 seq=%s（%s），下一轮重试", seq, e.type)
                    return sent
                sent += 1
            self.store.set_notify_cursor(env, seq)
        # TODO(edge)：长时间停机积压大量事件时合并成一张汇总卡片（09 §4.2）；现在每轮最多 100 条，逐条发
        return sent

    def run_forever(self, sleep: Callable[[float], None] = time.sleep, rounds: int | None = None) -> None:
        """rounds：只跑几轮就返回（测试用）；None 表示一直跑。"""
        n = 0
        while rounds is None or n < rounds:
            n += 1
            try:
                self.tick()
            except Exception:
                # M5 独立审阅发现 1：tick 里读游标、读审计表、渲染卡片的异常（Postgres 重启、专家包里删了动作）
                # 以前会让 notifier 线程永久退出，长连接照常在线、从外面看不出来。现在记日志，下一轮重试；
                # 游标没推进，恢复之后从原处补发（09 §4.2、§12 第 2 问）
                log.exception("推送这一轮失败，%s 秒后重试", self.cfg.poll_interval_s)
            sleep(self.cfg.poll_interval_s)
