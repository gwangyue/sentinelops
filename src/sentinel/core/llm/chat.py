"""按用途创建聊天模型（ADR-008；01 §3.4）。整个代码库里只有这里创建真实模型客户端。

负责：从 config/models.yaml 读型号和参数；测试环境下拒绝创建（guard_real_call）；
      给每次调用打上 agent_version、investigation_id、purpose 标签，方便在 LangSmith 里切片。
不负责：预算统计（诊断节点按 usage_metadata 汇总）。
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI

from sentinel.core.llm.config import guard_real_call, load_models_config


def chat_model(purpose: str) -> BaseChatModel:
    guard_real_call(purpose)
    spec = load_models_config().purposes[purpose]
    if spec.provider != "google":
        raise NotImplementedError(f"provider {spec.provider}")  # TODO(edge): 企业 AI 网关等其他出口（01 §3.4）
    params: dict[str, Any] = spec.params()
    return ChatGoogleGenerativeAI(model=spec.model, **params)


def run_tags(purpose: str, agent_version: str, investigation_id: str) -> dict[str, Any]:
    """LangChain 的 RunnableConfig 片段：标签和 metadata 会进入 LangSmith trace。"""
    return {
        "tags": [f"purpose:{purpose}", f"agent_version:{agent_version}"],
        "metadata": {"purpose": purpose, "agent_version": agent_version, "investigation_id": investigation_id},
    }
