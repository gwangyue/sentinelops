"""真实模型客户端只能在 core/llm/chat.py 里创建（ADR-008：所有调用经过统一封装，才能打标签、计预算、换出口）。"""

import ast

from tests.guards.scan import SRC, py_files

ALLOWED = {"core/llm/chat.py", "core/llm/embed.py"}  # 聊天模型和文本向量各一个入口
PROVIDER_MODULES = ("langchain_google_genai", "google.genai", "langchain_openai", "langchain_anthropic")


def test_only_llm_chat_imports_provider_sdks() -> None:
    bad = []
    for p in py_files():
        rel = p.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            )
            if any(n.startswith(PROVIDER_MODULES) for n in names) and rel not in ALLOWED:
                bad.append(f"{rel}:{node.lineno}")
    assert not bad, "模型服务商的 SDK 只能在 core/llm/chat.py 里使用：" + ", ".join(bad)
