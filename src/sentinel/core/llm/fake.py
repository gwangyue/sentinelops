"""按剧本回复的假聊天模型：测试和 demo-check 用（08 §6.4"测试环境下只能使用假模型"）。

负责：支持 bind_tools（Deep Agent 需要），每次调用把当前消息交给剧本函数，返回剧本给出的 AIMessage；记录调用次数。
不负责：模拟真实模型的质量。剧本是测试作者写的，测的是"流程和契约"，不是"诊断好不好"（那是评测的事）。
"""

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

Script = Callable[[Sequence[BaseMessage]], AIMessage]


class ScriptedChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    script: Script
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        msg = self.script(messages)
        # 给一个固定的用量，让 tokens_in / tokens_out 的汇总逻辑也被测到
        msg.usage_metadata = {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}
        return ChatResult(generations=[ChatGeneration(message=msg)])
