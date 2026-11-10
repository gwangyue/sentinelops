"""读取 config/memory_policy.yaml（进入 AgentVersion.components.memory_policy，03 §5.2）。"""

from pathlib import Path
from typing import Any

import yaml

PATH = Path(__file__).resolve().parents[4] / "config" / "memory_policy.yaml"


def load_memory_policy() -> dict[str, Any]:
    return dict(yaml.safe_load(PATH.read_text(encoding="utf-8")))
