"""专家包 SDK（01 §4）：专家包只能依赖 contracts 和 sdk。

ReadTool / ToolCaller / ToolResult：工具的形状和调用接口；ReadOnlyHttp：只有 GET 的连接；
DetectContext / make_finding / assemble_fact_sheet：检测器和 fact builder 用的帮助函数。
"""

from sentinel.sdk.budget import estimate_tokens, fit_budget
from sentinel.sdk.detector import (
    DetectContext,
    Detector,
    FactBuilder,
    Severity,
    assemble_fact_sheet,
    fingerprint,
    make_finding,
)
from sentinel.sdk.http import HttpError, ReadOnlyHttp
from sentinel.sdk.tool import Args, ReadTool, ToolCaller, ToolResult

__all__ = [
    "Args",
    "DetectContext",
    "Detector",
    "FactBuilder",
    "HttpError",
    "ReadOnlyHttp",
    "ReadTool",
    "Severity",
    "ToolCaller",
    "ToolResult",
    "assemble_fact_sheet",
    "estimate_tokens",
    "fingerprint",
    "fit_budget",
    "make_finding",
]
