"""记录调用的假飞书客户端：只给测试和 demo-check 用（tests/ 不能访问网络）。"""

from dataclasses import dataclass, field
from typing import Any

from sentinel.chatops.client import FeishuError


@dataclass
class FakeFeishuClient:
    sent: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)  # (chat_id, card, uuid)
    patched: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail_next: int = 0  # 接下来几次发送直接失败，用来测"失败不推进游标"

    def send_card(self, chat_id: str, card: dict[str, Any], uuid: str) -> str:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise FeishuError("假的发送失败")
        # 和飞书一样按 uuid 去重：同一个 uuid 返回同一条消息
        for i, (_, _, u) in enumerate(self.sent):
            if u == uuid:
                return f"om_{i}"
        self.sent.append((chat_id, card, uuid))
        return f"om_{len(self.sent) - 1}"

    def patch_card(self, message_id: str, card: dict[str, Any]) -> None:
        self.patched.append((message_id, card))
