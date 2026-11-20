"""飞书客户端（09 §3）：Protocol + 真实实现。测试用 chatops/fake.py 的假实现，通过构造函数注入。

负责：发卡片、更新卡片。
不负责：卡片长什么样（cards.py）、发哪些事件（notifier.py）、长连接收事件（D19–D20 的 gateway）。
"""

import json
import os
from typing import Any, Protocol


class FeishuError(RuntimeError):
    pass


class FeishuClient(Protocol):
    def send_card(self, chat_id: str, card: dict[str, Any], uuid: str) -> str:
        """发一张卡片，返回 message_id。uuid 是幂等键：同一个 uuid 重复发送只出现一条消息（09 §4.2）。"""
        ...

    def patch_card(self, message_id: str, card: dict[str, Any]) -> None: ...


class LarkFeishuClient:
    """用官方 SDK lark-oapi。凭证只从环境变量读（FEISHU_APP_ID / FEISHU_APP_SECRET），不进配置文件。"""

    def __init__(self, domain: str) -> None:
        import lark_oapi as lark

        self._c = (
            lark.Client.builder()
            .app_id(os.environ["FEISHU_APP_ID"])
            .app_secret(os.environ["FEISHU_APP_SECRET"])
            .domain(domain)
            .build()
        )

    def send_card(self, chat_id: str, card: dict[str, Any], uuid: str) -> str:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .uuid(uuid)
            .build()
        )
        r = self._c.im.v1.message.create(
            CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        )
        if not r.success() or r.data is None:
            raise FeishuError(f"发送失败：{r.code} {r.msg}")
        return str(r.data.message_id)

    def patch_card(self, message_id: str, card: dict[str, Any]) -> None:
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        body = PatchMessageRequestBody.builder().content(json.dumps(card, ensure_ascii=False)).build()
        r = self._c.im.v1.message.patch(PatchMessageRequest.builder().message_id(message_id).request_body(body).build())
        if not r.success():
            raise FeishuError(f"更新失败：{r.code} {r.msg}")
