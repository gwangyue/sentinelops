"""评测运行器的两条硬规则（08 §6.4；06 篇 §9）。D4 的运行器在启动时调用。

负责：数据集必须指定版本标签；被测 Agent 不开缓存。
不负责：评审模型的缓存（允许开，只要不影响被测 Agent）。
"""

import os
from collections.abc import Mapping


class EvalGuardError(ValueError):
    pass


def require_dataset_tag(tag: str | None) -> str:
    """不带标签或用 latest，两次评测之间的差异可能来自数据集本身的变化（03 §6.2）。"""
    if tag is None or not tag.strip():
        raise EvalGuardError("评测必须指定数据集版本标签，例如 --dataset-tag ds-2026-11-20")
    if tag.strip().lower() == "latest":
        raise EvalGuardError("不允许用 latest 作为数据集版本标签")
    return tag.strip()


def check_no_cache(env: Mapping[str, str] | None = None) -> None:
    """LANGSMITH_TEST_CACHE 会让重复运行直接复用旧输出，num_repetitions 就失去意义。"""
    e = os.environ if env is None else env
    if e.get("LANGSMITH_TEST_CACHE"):
        raise EvalGuardError("检测到 LANGSMITH_TEST_CACHE：被测 Agent 不能开缓存，请先 unset")
