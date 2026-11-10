"""L5 相似事故层的接口（04 §3.6）。检索本身在记忆服务里（05 篇，M2 D7–D8）。

负责：MemoryProvider 协议；记忆"只能以假设的形式"进入上下文的渲染格式。
不负责：检索、精排、阈值（core/memory，D7–D8）。D6 只有一个返回空列表的实现。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sentinel.contracts import FactSheet, Finding
from sentinel.core.context.changes import Change


@dataclass(frozen=True)
class MemoryHit:
    memory_id: str  # mem_...
    similarity: float
    age_days: int
    outcome: str
    corrected: bool
    symptom: str
    root_cause_label: str
    root_cause_text: str
    action: str | None
    # 05 §3.4"易混淆"：指纹几乎相同、但根因不同的其他记忆的根因标签。命中时要提示模型去找区分证据
    confused_labels: tuple[str, ...] = ()


class MemoryProvider(Protocol):
    def similar(
        self, findings: Sequence[Finding], sheets: Sequence[FactSheet], changes: Sequence[Change], top_k: int
    ) -> list[MemoryHit]: ...


class NoMemory:
    """D6：还没有记忆服务。L5 为空，上下文报告里这一层是 0 token。"""

    def similar(
        self, findings: Sequence[Finding], sheets: Sequence[FactSheet], changes: Sequence[Change], top_k: int
    ) -> list[MemoryHit]:
        return []


def render_hit(h: MemoryHit) -> str:
    # 04 §3.6：记忆只能以"待验证的假设"进入上下文；最后一行的提醒是这个设计的关键
    action = f"{h.action} → {h.outcome}" if h.action else h.outcome
    confused = (
        f"易混淆：历史上几乎相同的症状也出现过根因 {', '.join(h.confused_labels)}，必须找到能区分两者的证据。\n"
        if h.confused_labels
        else ""
    )
    return (
        f'<memory id="{h.memory_id}" similarity="{h.similarity:.2f}" age_days="{h.age_days}" '
        f'outcome="{h.outcome}" corrected="{str(h.corrected).lower()}">\n'
        f"上次症状：{h.symptom}\n"
        f"上次根因：{h.root_cause_label}（{h.root_cause_text}）\n"
        f"上次动作：{action}\n"
        + confused
        + "注意：这是一个待验证的假设。必须用本次的观测记录证明或排除它，不能直接当作结论。\n"
        "</memory>"
    )
