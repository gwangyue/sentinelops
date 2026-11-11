"""调查图的确认段（02 §1、§3.4；05 §3.1；08 M3 D12）：confirm（interrupt，人确认根因）→ memorize（写记忆）。

负责：
- confirm：只有 interrupt()。人在 CLI 里确认或修正根因（`sentinel investigations confirm`），审计里写
  rootcause.confirmed / rootcause.corrected，然后恢复；
- memorize：从审计里读人的决定（只信存储，不信 resume 带来的内容），调用 write_confirmed 写记忆，
  并更新这次 L5 里出现过的记忆的反馈（05 §3.7）。没有确认（拒绝确认、超时）就不写——宁可少记，也不要记错。
不负责：记忆的检索、合并规则（core/memory/）；确认命令本身（CLI）。
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langgraph.types import interrupt

from sentinel.contracts import AuditEvent, Diagnosis, FactSheet, Finding
from sentinel.core.context.changes import Change
from sentinel.core.memory import feedback
from sentinel.core.memory.fingerprint import build as build_fingerprint
from sentinel.core.memory.store import MemoryStore
from sentinel.core.memory.write import write_confirmed

CONFIRM_EVENTS = ("rootcause.confirmed", "rootcause.corrected")


@dataclass
class Memorizer:
    """确认段的依赖。没有它（评测、只报告模式）时确认段不会被接进图里。"""

    store: MemoryStore  # 记忆索引（线上 incidents-<pack>）
    embed: Callable[[Sequence[str]], list[list[float]]]
    policy: dict[str, Any]


def wait_confirmation(inv_id: str) -> Any:
    """confirm 节点的全部内容。和 approve 一样不能包 try/except（interrupt 靠抛异常暂停图）。"""
    return interrupt({"confirm": inv_id})


def latest_confirmation(events: Sequence[AuditEvent]) -> AuditEvent | None:
    return next((e for e in reversed(events) if e.type in CONFIRM_EVENTS), None)


def memorize(
    m: Memorizer,
    *,
    inv_id: str,
    state: dict[str, Any],
    confirmation: AuditEvent,
    descriptions: dict[str, str],
    target_version: str,
    audit: Callable[[str, dict[str, Any]], None],
) -> str:
    """写入一条确认过的记忆，返回记忆 ID。指纹从这次调查的问题事件、事实和变更算出来（05 §3.2）。"""
    findings = [Finding.model_validate(f) for f in state["findings"]]
    sheets = [FactSheet.model_validate(s) for s in state["fact_sheets"]]
    from datetime import datetime

    changes = [Change(**{**c, "since": datetime.fromisoformat(c["since"])}) for c in state.get("changes", [])]
    label = str(confirmation.payload["label"])
    corrected = confirmation.type == "rootcause.corrected"
    plan = state.get("plan") or {}
    rec = write_confirmed(
        m.store,
        namespace=f"{findings[0].pack}/{findings[0].target}",
        investigation_id=inv_id,
        fingerprint=build_fingerprint(findings, sheets, changes),
        root_cause_label=label,
        root_cause_description=descriptions.get(label, ""),
        corrected=corrected,
        confirmed_by=confirmation.actor,
        outcome="resolved" if state.get("remediation") == "resolved" else "no_effect",
        action_taken=plan.get("action_id"),
        target_version=target_version,
        embed=m.embed,
        policy=m.policy,
        audit=audit,
    )
    # 05 §3.7：这次 L5 里出现过的记忆，按确认的根因更新命中、有用和伤害
    shown = [i["ref"] for i in (state.get("bundle") or {}).get("items", []) if i["layer"] == "L5" and i["included"]]
    d = Diagnosis.model_validate(state["diagnosis"]) if state.get("diagnosis") else None
    h1 = d.hypotheses[0] if d and d.hypotheses else None
    harmed = list(h1.from_memory) if h1 and corrected else []
    if shown:
        feedback.update(m.store, shown=shown, confirmed_label=label, harmed=harmed, policy=m.policy, audit=audit)
    return rec.id


def record_confirmation(
    audit: Callable[..., Any],
    *,
    inv_id: str,
    diagnosis: Diagnosis,
    hypothesis: str | None,
    label: str | None,
    who: str,
    valid_labels: Sequence[str],
) -> AuditEvent:
    """人确认根因（CLI `investigations confirm`）。--root-cause H1：确认某个假设；--label：直接给出根因。
    给出的根因和 H1 不同就是"修正"（rootcause.corrected，记忆里 corrected=true，05 §3.1"这类记忆最有价值"）。"""
    # 假设 ID 不区分大小写：模型有时写成 h1（M3 D12 在 kind 上实测）
    by_id = {h.id.upper(): h for h in diagnosis.hypotheses}
    if hypothesis is not None:
        hypothesis = hypothesis.upper()
        if hypothesis not in by_id:
            raise ValueError(f"诊断里没有假设 {hypothesis}（有 {', '.join(by_id)}）")
        label = by_id[hypothesis].root_cause_label
    if not label or label not in valid_labels:
        raise ValueError(f"根因标签必须是专家包声明过的（ADR-011）：{', '.join(valid_labels)}")
    h1 = diagnosis.hypotheses[0].root_cause_label if diagnosis.hypotheses else None
    corrected = label != h1
    e: AuditEvent = audit(
        investigation_id=inv_id,
        agent_version=None,
        actor=who,
        type="rootcause.corrected" if corrected else "rootcause.confirmed",
        payload={"label": label, "hypothesis": hypothesis, "h1_label": h1},
    )
    return e
