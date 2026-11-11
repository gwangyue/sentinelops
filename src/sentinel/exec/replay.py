"""回放模式下的执行器模拟（01 §3.1；ADR-005 后果"回放模式下需要一个执行器的模拟实现"）。

负责：提供 core.process 用的 run_action——校验（哈希、审批、过期、幂等、执行前前置检查）和真实执行器
      走完全同一段代码，只把"执行"换成"把回放后端切到录制的 post 阶段"，验证节点读到的就是执行之后的录制。
不负责：任何写操作（这里没有写连接）。
录制时执行的不是这个动作时不切换，记为失败（PostNotRecorded）：别的动作执行之后集群会是什么样，录制里没有。
"""

from collections.abc import Callable
from typing import Any

from sentinel.contracts import ActionSpec, RemediationPlan
from sentinel.exec.core import RunAction


class PostNotRecorded(RuntimeError):
    """录制的 post 阶段不是这个动作执行之后的状态：评测里记为 remediation_not_recorded，不算修复成功也不算失败。"""


def replay_runner(enter_post: Callable[[str], bool]) -> RunAction:
    def run(spec: ActionSpec, plan: RemediationPlan, kind: str) -> dict[str, Any]:
        if kind == "rollback":
            # TODO(edge)：录制里没有"回滚之后"的状态；回滚只记一次模拟，不切换阶段
            return {"simulated": True, "kind": "rollback"}
        if not enter_post(spec.id):
            raise PostNotRecorded(f"录制里没有执行 {spec.id} 之后的状态")
        return {"simulated": True, "kind": "execute", "action": spec.id}

    return run
