"""只读工具和工具调用接口（03 §3.1、§3.3；04 §3.3；01 §3.1）。

负责：专家包实现工具的形状（ReadTool），以及检测器、fact builder 调用工具的接口（ToolCaller）。
不负责：工具调用怎样执行、录制、回放和存储——那是 core/toolbackend 的事，专家包看不到。
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from sentinel.sdk.http import ReadOnlyHttp

Args = Mapping[str, Any]


@dataclass(frozen=True)
class ReadTool:
    """一个只读工具 = 取原始数据 + 确定性摘要器。pack.yaml 的 entry 指向这样一个对象。"""

    description: str  # 给模型看的工具描述；它的哈希会进入 AgentVersion（03 §5.2）
    fetch: Callable[[ReadOnlyHttp, Args], Any]
    summarize: Callable[[Any, Args], str]  # 原始输出 → 摘要；只用确定性代码，不用 LLM（04 §3.3）
    defaults: Mapping[str, Any] = field(default_factory=dict)  # 参数规范化时会被去掉，避免同一调用两个回放键
    # 给模型看的参数定义（字段描述会进入工具描述）；没有参数的工具为 None
    args_schema: type[BaseModel] | None = None


@dataclass(frozen=True)
class ToolResult:
    observation_id: str
    status: Literal["ok", "error", "replay_miss"]
    raw: Any  # status 不是 ok 时为 None 或错误信息
    digest: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class ToolCaller(Protocol):
    """检测器、fact builder、诊断 Agent 调用工具的唯一入口。每次调用都会产生一条观测记录。"""

    def call(self, tool: str, args: Args | None = None) -> ToolResult: ...
