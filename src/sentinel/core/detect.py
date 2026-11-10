"""一次检测：运行专家包的全部检测器和 fact builder（02 §2 巡检的 detect 步骤）。

负责：组装 DetectContext（工具调用接口 + 环境记忆），收集 Finding 和 FactSheet。
不负责：去重、是否启动诊断、审计（D3 的调查图）。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from sentinel.contracts import FactSheet, Finding
from sentinel.core.packs_registry import LoadedPack
from sentinel.sdk import DetectContext, ToolCaller

ENV_DIR = Path(__file__).resolve().parents[3] / "env"


def load_env(target: str, env_dir: Path = ENV_DIR) -> dict[str, Any]:
    p = env_dir / f"{target}.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {} if p.exists() else {}


@dataclass(frozen=True)
class DetectResult:
    findings: list[Finding]
    fact_sheets: list[FactSheet]


def run_detect(pack: LoadedPack, target: str, tools: ToolCaller, env: dict[str, Any]) -> DetectResult:
    ctx = DetectContext(pack=pack.manifest.pack, target=target, tools=tools, env=env)
    findings = [f for det in pack.detectors.values() for f in det(ctx)]
    sheets = [fb(ctx) for fb in pack.fact_builders]
    return DetectResult(findings, sheets)
