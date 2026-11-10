"""动作规格 ActionSpec（03 §4.2）：执行器不理解动作含义，只按这份声明执行。"""

from typing import Any, Literal

from sentinel.contracts._base import Contract


class VerifySpec(Contract):
    predicate: str  # 验证函数入口
    timeout_s: int
    poll_interval_s: int = 15


class ActionSpec(Contract):
    schema_version: int = 1
    id: str  # "es.relocate_shards"
    pack: str
    risk: Literal["low", "medium", "high"]
    reversible: bool
    params_schema: dict[str, Any]  # JSON Schema
    preconditions: list[str]  # 前置检查函数入口
    dry_run: str
    execute: str  # 只在执行器进程里加载
    rollback: str | None  # 不可逆动作可以为空，风险按 high 处理（M3 执行器负责）
    verify: VerifySpec
    requires_approval: bool = True  # 第一版所有动作都要审批
    max_blast_radius: dict[str, int] | None
