"""动作规格 ActionSpec（03 §4.2）：执行器不理解动作含义，只按这份声明执行。

例子取自 packs/es/actions/es.relocate_shards.yaml。
"""

from typing import Any, Literal

from sentinel.contracts._base import Contract


# 执行后怎么确认生效：每隔 poll_interval_s 调一次谓词函数，直到为真或超时。
class VerifySpec(Contract):
    predicate: str  # 验证函数入口。例："sentinel.packs.es.actions.es_actions:relocate_verify"
    timeout_s: int  # 例：600
    poll_interval_s: int = 15  # 例：15


# 一个修复动作的完整声明：参数格式、风险、前置检查、预演、执行、回滚、验证各用哪个函数。
class ActionSpec(Contract):
    schema_version: int = 1  # 例：1
    id: str  # "es.relocate_shards"
    pack: str  # 例："es"
    risk: Literal["low", "medium", "high"]  # 例："medium"
    reversible: bool  # 例：True
    # JSON Schema；LLM 给的 params 必须通过它。例：
    # {"type": "object", "properties": {"from_node": {"type": "string", ...}, "max_shards": {"type": "integer", ...}},
    #  "required": ["from_node", "max_shards"], "additionalProperties": false}
    params_schema: dict[str, Any]
    preconditions: list[str]  # 前置检查函数入口。例：["sentinel.packs.es.actions.es_actions:relocate_precheck"]
    dry_run: str  # 例："sentinel.packs.es.actions.es_actions:relocate_dry_run"
    execute: str  # 只在执行器进程里加载。例："sentinel.packs.es.actions.es_actions:relocate_execute"
    # 不可逆动作可以为空，风险按 high 处理（M3 执行器负责）。
    # 例："sentinel.packs.es.actions.es_actions:relocate_rollback"
    rollback: str | None
    verify: VerifySpec  # 例：见 VerifySpec
    requires_approval: bool = True  # 第一版所有动作都要审批
    max_blast_radius: dict[str, int] | None  # 一次最多影响多少对象。例：{"shards": 20}
