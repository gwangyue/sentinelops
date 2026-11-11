"""动作规格的加载（03 §4.2）：专家包目录下 actions/<动作 ID>.yaml → ActionSpec；"模块:函数"入口 → 函数。

调查引擎（core）和执行器（exec）都用这一份：exec 不能依赖 core（ADR-005），所以放在 sdk。
负责：读规格、校验 pack.yaml 里声明的每个动作都有规格文件、按入口导入函数。
不负责：决定调用哪个函数（plan 节点、执行器）。
"""

import importlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from sentinel.contracts import ActionSpec


def load_action_specs(pack_dir: Path, action_ids: Sequence[str]) -> dict[str, ActionSpec]:
    out = {}
    for aid in action_ids:
        p = pack_dir / "actions" / f"{aid}.yaml"
        if not p.exists():
            raise FileNotFoundError(f"pack.yaml 声明了动作 {aid}，但没有规格文件 {p}")
        spec = ActionSpec.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))
        if spec.id != aid:
            raise ValueError(f"{p} 里的 id 是 {spec.id}，和文件名不一致")
        out[aid] = spec
    return out


def resolve_entry(entry: str) -> Callable[..., Any]:
    mod, _, fn = entry.partition(":")
    return getattr(importlib.import_module(mod), fn)  # type: ignore[no-any-return]
