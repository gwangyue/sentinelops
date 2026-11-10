"""L2 下钻的上下文压缩（04 §3.11，G3）。

负责：在每次调用模型之前，如果工具返回内容的累计 token 达到 L2_max × trigger_ratio，
      保留最近 keep_recent 次的完整内容，更早的工具返回压成一行"工具 → 结论 + 观测 ID"。
      只改发给模型的消息，不改 Agent 的状态：观测记录完整保留，模型可以用 /obs/<id>.json 找回原文。
不负责：用模型写小结。04 §3.11 优先用"诊断 Agent 每次下钻后写的一句小结"，但 Gemini 调工具时通常不写文字，
        第一版用确定性规则（摘要的第一行）。TODO(edge)：有小结时优先用小结，没有时才用 LLM 压缩并标记。
为什么不用框架自带的摘要中间件：它不知道观测 ID 是证据链的关键，可能把 ID 摘掉（04 §3.11）。
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AnyMessage, ToolMessage

from sentinel.sdk import estimate_tokens

_OBS = re.compile(r'<untrusted source="(obs_[0-9A-Z]+)">\n(.*?)(?:\n|</untrusted>)', re.S)


@dataclass
class CompactionRecord:
    """写进上下文报告：压缩了哪几条、压缩前后各多少 token。"""

    compacted: list[str] = field(default_factory=list)  # 被压缩的观测 ID
    tokens_before: int = 0
    tokens_after: int = 0


def one_line(msg: ToolMessage) -> str:
    text = str(msg.content)
    m = _OBS.search(text)
    if not m:
        return f"{msg.name} →（已压缩）"
    return f"{msg.name} → {m.group(2).strip()[:160]}（{m.group(1)}；原文见 /obs/{m.group(1)}.json）"


def compact(
    messages: list[AnyMessage], l2_max: int, trigger_ratio: float, keep_recent: int, record: CompactionRecord
) -> list[AnyMessage]:
    tools = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    total = sum(estimate_tokens(str(messages[i].content)) for i in tools)
    if total < l2_max * trigger_ratio or len(tools) <= keep_recent:
        return messages
    out = list(messages)
    for i in tools[:-keep_recent]:
        m = out[i]
        assert isinstance(m, ToolMessage)
        short = one_line(m)
        if str(m.content) == short:
            continue
        found = _OBS.search(str(m.content))
        if found and found.group(1) not in record.compacted:
            record.compacted.append(found.group(1))
        out[i] = m.model_copy(update={"content": short})
    record.tokens_before = total
    record.tokens_after = sum(estimate_tokens(str(out[i].content)) for i in tools)
    return out


class L2Compaction(AgentMiddleware):
    def __init__(self, l2_max: int, trigger_ratio: float, keep_recent: int) -> None:
        super().__init__()
        self.l2_max, self.trigger_ratio, self.keep_recent = l2_max, trigger_ratio, keep_recent
        self.record = CompactionRecord()

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> Any:
        msgs = compact(list(request.messages), self.l2_max, self.trigger_ratio, self.keep_recent, self.record)
        return handler(request.override(messages=msgs) if msgs is not request.messages else request)
