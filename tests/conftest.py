"""全局测试夹具（08 §6.4"tests/ 不访问网络、不调用真实 LLM"）。

网络：pyproject 的 addopts 默认带 --disable-socket（pytest-socket），任何 TCP 连接都会失败。
LLM：这里设置 SENTINEL_ENV=test，core/llm 在创建真实模型客户端前会检查它并拒绝。
"""

import pytest


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_ENV", "test")
    # 本机 shell 里可能带着真实的 key；测试里清掉，即使有代码绕过 guard 也调不通。
    # 追踪开关也清掉：shell 里开了 LANGSMITH_TRACING，测试里的假模型调用也会尝试上报 trace
    for var in (
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "LANGSMITH_API_KEY",
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING_V2",
    ):
        monkeypatch.delenv(var, raising=False)
