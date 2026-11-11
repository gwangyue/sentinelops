"""方案哈希和幂等键（03 §4.3–4.4；02 §3.4）。

负责：用确定性代码算出 plan_hash = sha256(规范化 JSON(action_id, target, params, precheck_snapshot))，
      以及 idempotency_key = sha256(investigation_id, plan_hash)。
      调查引擎（plan 节点）和执行器都用这一份：执行器自己重算一遍，和审批记录里的比对。
不负责：哈希存在哪里、谁来比对（plan 节点、执行器）。
安全规则（CLAUDE.md）：这两个值永远不让 LLM 生成，也不从 LLM 的输出里读。
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def _canonical(obj: Any) -> str:
    # 键排序、无多余空白：同一个方案无论参数的书写顺序如何，哈希都一样
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def plan_hash(action_id: str, target: str, params: Mapping[str, Any], precheck_snapshot: Mapping[str, Any]) -> str:
    body = {"action_id": action_id, "target": target, "params": params, "precheck_snapshot": precheck_snapshot}
    return "sha256:" + hashlib.sha256(_canonical(body).encode()).hexdigest()


def idempotency_key(investigation_id: str, plan_hash_: str) -> str:
    return "sha256:" + hashlib.sha256(_canonical([investigation_id, plan_hash_]).encode()).hexdigest()
