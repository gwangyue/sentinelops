"""读取模型配置 config/models.yaml（ADR-008），并守住"测试里不调用真实 LLM"（08 §6.4）。

负责：按用途取型号和参数；加载时校验型号不是别名；测试环境下拒绝创建真实模型客户端。
不负责：创建模型客户端本身（D3 接入 langchain-google-genai 时实现，创建前必须先调用 guard_real_call）。
"""

import os
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from sentinel.core.versioning.models import assert_pinned

# 仓库根目录下的 config/models.yaml；部署时可以用环境变量指向别的文件
_DEFAULT_PATH = Path(__file__).resolve().parents[4] / "config" / "models.yaml"
TEST_ENV_VAR = "SENTINEL_ENV"


class ModelSpec(BaseModel):
    # 除了 provider 和 model，其他参数（temperature、thinking_budget……）原样保留，
    # 因为它们整体进入 AgentVersion.components.models，不在这里逐个定义
    model_config = ConfigDict(extra="allow", frozen=True)

    provider: str
    model: str

    @field_validator("model")
    @classmethod
    def _pinned(cls, v: str) -> str:
        return assert_pinned(v)

    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class ModelsConfig(BaseModel):
    schema_version: int
    purposes: dict[str, ModelSpec]


@cache
def load_models_config(path: str | None = None) -> ModelsConfig:
    p = Path(path or os.environ.get("SENTINEL_MODELS_CONFIG") or _DEFAULT_PATH)
    return ModelsConfig.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))


class RealModelInTestError(RuntimeError):
    pass


def guard_real_call(purpose: str) -> None:
    """创建任何真实模型客户端之前必须调用。tests/conftest.py 会设置 SENTINEL_ENV=test。"""
    if os.environ.get(TEST_ENV_VAR) == "test":
        raise RealModelInTestError(f"测试环境不允许调用真实模型（用途：{purpose}），请用假模型")
