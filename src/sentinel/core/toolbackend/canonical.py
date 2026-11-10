"""参数规范化（ADR-003）：同一个调用无论参数顺序、是否写出默认值，都得到同一个回放键。"""

import json
from collections.abc import Mapping
from typing import Any


def canonical_args(args: Mapping[str, Any] | None, defaults: Mapping[str, Any]) -> dict[str, Any]:
    out = {}
    for k in sorted(args or {}):
        v = (args or {})[k]
        if k in defaults and defaults[k] == v:
            continue  # 和默认值相同就去掉
        out[k] = v
    return out


def replay_key(tool: str, args: Mapping[str, Any]) -> str:
    return tool + " " + json.dumps(args, sort_keys=True, ensure_ascii=False)
