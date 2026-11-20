"""飞书长连接入口（09 §3、§7；S7）：把 lark-oapi 的回调翻译成 chatops 自己的结构，再翻译回去。

负责：建长连接客户端；注册卡片点击回调（审批按钮 + 帮助卡片的指令按钮，D19–D20）和消息事件
      im.message.receive_v1（固定指令，D20）；在同一个进程里起 notifier 线程（`sentinel chatops serve`）。
不负责：点击和指令之后的任何判断（approvals.py、commands.py）。

这一层故意很薄：所有规则都在 approvals.py / commands.py 里，用假数据就能测；这里只剩 SDK 对象的拆装。
长连接下没有逐条签名，鉴权在连接这一层（S7），所以 encrypt_key / verification_token 留空。
"""

import json
import logging
import os
import threading
from typing import Any

from sentinel.chatops.approvals import ApprovalHandler, CardAction
from sentinel.chatops.commands import ChatMessage, CommandHandler, route_card
from sentinel.chatops.notifier import Notifier

log = logging.getLogger(__name__)


def _on_card(handler: ApprovalHandler, commands: CommandHandler) -> Any:
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
        P2CardActionTriggerResponse,
    )

    def on_card(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        ev = data.event
        ctx = ev.context if ev else None
        a = CardAction(
            open_id=ev.operator.open_id if ev and ev.operator else "",
            chat_id=ctx.open_chat_id if ctx else None,
            message_id=ctx.open_message_id if ctx else None,
            value=(ev.action.value if ev and ev.action else None) or {},
        )
        r = route_card(a, handler, commands)
        body: dict[str, Any] = {"toast": {"type": r.toast_type, "content": r.toast}}
        if r.card is not None:
            body["card"] = {"type": "raw", "data": r.card}  # 回调里直接返回新卡片：原地更新（S7 Q4）
        return P2CardActionTriggerResponse(body)

    return on_card


def _on_message(commands: CommandHandler) -> Any:
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    def on_message(data: P2ImMessageReceiveV1) -> None:
        ev = data.event
        msg = ev.message if ev else None
        sender = ev.sender.sender_id if ev and ev.sender else None
        if msg is None:
            return
        try:
            # 文本消息的 content 是 {"text": "..."}；图片、文件等没有 text，当成空输入（回帮助卡片）
            text = str(json.loads(msg.content or "{}").get("text", ""))
        except ValueError:
            text = ""
        m = ChatMessage(
            open_id=(sender.open_id if sender else None) or "",
            chat_id=msg.chat_id,
            request_id=msg.message_id or "",
            text=text,
        )
        # 放到线程里：发卡片要走网络，不占住长连接的事件循环
        # TODO(edge)：同一条消息被重复投递时指令会执行两次（卡片按 uuid 去重只回一次）；需要按 message_id 记已处理
        threading.Thread(target=commands.on_message, args=(m,), name="chatops-message").start()

    return on_message


def serve(handler: ApprovalHandler, commands: CommandHandler, notifier: Notifier) -> None:
    """阻塞运行：notifier 在后台线程按游标推送，主线程跑长连接。"""
    import lark_oapi as lark

    threading.Thread(target=notifier.run_forever, name="chatops-notifier", daemon=True).start()
    events = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(_on_card(handler, commands))
        .register_p2_im_message_receive_v1(_on_message(commands))
        .build()
    )
    # TODO(edge)：长连接断开的重连交给 SDK（它自带重连）；notifier 线程异常退出时整个进程不会跟着退出
    lark.ws.Client(
        os.environ["FEISHU_APP_ID"],
        os.environ["FEISHU_APP_SECRET"],
        event_handler=events,
        domain=handler.cfg.domain,
        log_level=lark.LogLevel.INFO,
    ).start()
