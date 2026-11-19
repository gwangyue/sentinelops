"""专家包注册表（01 §2"core 不能 import 具体的专家包，专家包通过注册表在运行时加载"）。

负责：读取 sentinel/packs/<名称>/pack.yaml → PackManifest，按 entry 用 importlib 加载工具、检测器和 fact builder；
      读取动作目录的规格（只读规格，不导入执行函数）。
      discover_packs：扫描 sentinel/packs/ 下有 pack.yaml 的子包（M4 D13 通用化：新增领域不改 core）。
不负责：发现第三方专家包（entry point 机制，TODO(edge)），以及 pack.yaml 之外的校验。
"""

import importlib
import importlib.resources
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sentinel.contracts import ActionSpec, PackManifest, ToolDecl
from sentinel.sdk import Detector, FactBuilder, ReadTool, load_action_specs


class PackLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedTool:
    decl: ToolDecl
    impl: ReadTool


@dataclass(frozen=True)
class LoadedPack:
    manifest: PackManifest  # 专家包 manifest
    tools: dict[str, LoadedTool]
    detectors: dict[str, Detector]
    fact_builders: list[FactBuilder]
    # 动作目录只加载规格：execute / rollback 的入口在这里不导入，只有执行器进程会导入（03 §4.2）
    actions: dict[str, ActionSpec] = field(default_factory=dict)


def _resolve(entry: str) -> Any:
    module, _, attr = entry.partition(":")
    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as e:
        raise PackLoadError(f"无法加载 entry {entry!r}：{e}") from e


def load_pack(name: str) -> LoadedPack:
    text = importlib.resources.files(f"sentinel.packs.{name}").joinpath("pack.yaml").read_text(encoding="utf-8")
    manifest = PackManifest.model_validate(yaml.safe_load(text))  # 加载专家包 manifest
    tools = {}
    for t in manifest.tools:
        impl = _resolve(t.entry)
        if not isinstance(impl, ReadTool):
            raise PackLoadError(f"工具 {t.id} 的 entry 不是 ReadTool")
        tools[t.id] = LoadedTool(t, impl)  # 加载工具, 字典：名字 -> LoadedTool
    detectors = {d.id: _resolve(d.entry) for d in manifest.detectors}
    fact_builders = [_resolve(f.entry) for f in manifest.fact_builders]
    pack_dir = Path(str(importlib.resources.files(f"sentinel.packs.{name}")))
    actions = load_action_specs(pack_dir, manifest.actions)  # 加载动作规格 字典：名字 -> ActionSpec
    return LoadedPack(manifest, tools, detectors, fact_builders, actions)


def discover_packs() -> list[str]:
    """sentinel/packs/ 下所有带 pack.yaml 的子包名，按名字排序（排序让 AgentVersion 的组件顺序稳定）。"""
    root = importlib.resources.files("sentinel.packs")  # 专家包根目录
    return sorted(d.name for d in root.iterdir() if d.is_dir() and d.joinpath("pack.yaml").is_file())
