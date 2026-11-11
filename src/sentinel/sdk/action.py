"""动作函数的接口（03 §4.2）：专家包按这个形状写前置检查、预演、执行、回滚、验证。

负责：定义动作函数拿到什么、返回什么。
- 前置检查、预演、验证只拿到只读的 ToolCaller：它们在调查引擎里跑（回放模式下也能跑），不能有写权限；
- 执行、回滚额外拿到 WriteHttp：只有执行器进程会构造它（写凭证只在 exec/，ADR-005）。
不负责：谁来调用、按什么顺序调用（plan 节点、执行器）。
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from sentinel.sdk.tool import ToolCaller


class WriteHttp(Protocol):
    """执行器传给 execute / rollback 的写连接。实现只在 exec/ 里。"""

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any: ...
    def put(self, path: str, body: Mapping[str, Any]) -> Any: ...
    def post(self, path: str, body: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True)
class ActionContext:
    target: str
    params: Mapping[str, Any]
    tools: ToolCaller  # 只读


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    reason: str
    # 前置检查当时看到的状态：进入 plan_hash（03 §4.3），回滚时用它恢复原值
    snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecContext:
    target: str
    params: Mapping[str, Any]
    precheck_snapshot: Mapping[str, Any]
    http: WriteHttp


@dataclass(frozen=True)
class VerifyResult:
    passed: bool
    detail: str
