"""专家包 SDK（01 §4）：专家包只能依赖 contracts 和 sdk。

ActionContext / ExecContext / CheckResult / VerifyResult / WriteHttp：动作函数的形状（03 §4.2）；
load_action_specs / resolve_entry：动作规格和函数入口的加载（core 和 exec 共用，exec 不能依赖 core）。

ReadTool / ToolCaller / ToolResult：工具的形状和调用接口；connect：按 connection.kind 取连接；
ReadOnlyHttp：只有 GET 的连接；
DetectContext / make_finding / assemble_fact_sheet：检测器和 fact builder 用的帮助函数。
"""

from sentinel.sdk.action import ActionContext, CheckResult, ExecContext, VerifyResult, WriteHttp
from sentinel.sdk.actions_loader import load_action_specs, resolve_entry
from sentinel.sdk.budget import estimate_tokens, fit_budget
from sentinel.sdk.connect import CONNECTORS, domain_name, open_read, open_write
from sentinel.sdk.detector import (
    DetectContext,
    Detector,
    FactBuilder,
    Severity,
    assemble_fact_sheet,
    fingerprint,
    make_finding,
)
from sentinel.sdk.http import HttpError, ReadOnlyHttp, ToolCallError, WriteHttpClient
from sentinel.sdk.tool import Args, ReadTool, ToolCaller, ToolResult

__all__ = [
    "CONNECTORS",
    "ActionContext",
    "Args",
    "CheckResult",
    "DetectContext",
    "Detector",
    "ExecContext",
    "FactBuilder",
    "HttpError",
    "ReadOnlyHttp",
    "ReadTool",
    "Severity",
    "ToolCallError",
    "ToolCaller",
    "ToolResult",
    "VerifyResult",
    "WriteHttp",
    "WriteHttpClient",
    "assemble_fact_sheet",
    "domain_name",
    "estimate_tokens",
    "fingerprint",
    "fit_budget",
    "load_action_specs",
    "make_finding",
    "open_read",
    "open_write",
    "resolve_entry",
]
