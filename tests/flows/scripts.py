"""假模型的剧本：模拟"调一次 allocation_explain，再给出诊断"的过程。剧本从工具返回里读出真实的观测 ID。"""

import re
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

OBS = re.compile(r'source="(obs_[0-9A-Z]+)"')


def _last_tool_result(messages: Sequence[BaseMessage]) -> tuple[str, str]:
    t = next(m for m in reversed(messages) if isinstance(m, ToolMessage))
    text = str(t.content)
    m = OBS.search(text)
    assert m, text
    return m.group(1), text


def diagnose_script(
    *,
    label: str = "es.disk_high_watermark",
    confidence: float = 0.85,
    evidence: str | None = None,
    quote: str | None = "no [disk_threshold]",
):  # type: ignore[no-untyped-def]
    """第 1 次调用：调 allocation_explain；第 2 次调用：用它的观测 ID 作为证据，给出结构化诊断。"""

    def script(messages: Sequence[BaseMessage]) -> AIMessage:
        if not any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage("", tool_calls=[{"name": "es_allocation_explain", "args": {}, "id": "call_1"}])
        obs_id, _ = _last_tool_result(messages)
        out: dict[str, Any] = {
            "hypotheses": [
                {
                    "id": "H1",
                    "root_cause": "data-2 磁盘超过高水位，副本无法分配",
                    "root_cause_label": label,
                    "confidence": confidence,
                    "claims": [
                        {
                            "id": "C1",
                            "text": "data-2 被 disk_threshold 拒绝",
                            "evidence": [evidence or obs_id],
                            "quote": quote,
                        }
                    ],
                }
            ],
            "escalate": False,
        }
        return AIMessage("", tool_calls=[{"name": "DiagnosisOutput", "args": out, "id": "call_2"}])

    return script
