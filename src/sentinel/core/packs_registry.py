"""专家包注册表（01 §2"core 不能 import 具体的专家包，专家包通过注册表在运行时加载"）。

负责：读取 sentinel/packs/<名称>/pack.yaml → PackManifest，按 entry 用 importlib 加载工具、检测器和 fact builder。
不负责：发现第三方专家包（entry point 机制，TODO(edge)），以及 pack.yaml 之外的校验。
"""

import importlib
import importlib.resources
from dataclasses import dataclass
from typing import Any

import yaml

from sentinel.contracts import PackManifest, ToolDecl
from sentinel.sdk import Detector, FactBuilder, ReadTool


class PackLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedTool:
    decl: ToolDecl
    impl: ReadTool


@dataclass(frozen=True)
class LoadedPack:
    manifest: PackManifest
    tools: dict[str, LoadedTool]
    detectors: dict[str, Detector]
    fact_builders: list[FactBuilder]


def _resolve(entry: str) -> Any:
    module, _, attr = entry.partition(":")
    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as e:
        raise PackLoadError(f"无法加载 entry {entry!r}：{e}") from e


def load_pack(name: str) -> LoadedPack:
    text = importlib.resources.files(f"sentinel.packs.{name}").joinpath("pack.yaml").read_text(encoding="utf-8")
    manifest = PackManifest.model_validate(yaml.safe_load(text))
    tools = {}
    for t in manifest.tools:
        impl = _resolve(t.entry)
        if not isinstance(impl, ReadTool):
            raise PackLoadError(f"工具 {t.id} 的 entry 不是 ReadTool")
        tools[t.id] = LoadedTool(t, impl)
    detectors = {d.id: _resolve(d.entry) for d in manifest.detectors}
    fact_builders = [_resolve(f.entry) for f in manifest.fact_builders]
    return LoadedPack(manifest, tools, detectors, fact_builders)
