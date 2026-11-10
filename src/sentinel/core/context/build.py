"""上下文组装（04 §2.2、§3.7–3.9）：L0 问题事件、L1 事实卡片、L5 相似事故、L3 变更、L4 知识。

负责：每层按自己的预算放入条目；总量超出 preassembled_total 时按 degrade_order 降级；
      记录每个被删掉的条目和原因（上下文报告）；来自被管系统的文本包进 <untrusted> 并转义；产出 ContextBundle。
不负责：各层的数据从哪里来（变更层 changes.py、知识层 knowledge_layer.py、记忆层 memory_layer.py）；
        L2 下钻在诊断过程中由工具调用产生，压缩见 core/diagnosis/compaction.py。
整条流水线没有 LLM 调用（P1）。
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sentinel.contracts import ContextBundle, ContextItem, Fact, FactSheet, Finding
from sentinel.core.context.changes import Change
from sentinel.core.context.knowledge_layer import render_hit as render_knowledge
from sentinel.core.context.memory_layer import MemoryHit
from sentinel.core.context.memory_layer import render_hit as render_memory
from sentinel.core.knowledge.es_index import KnowledgeHit
from sentinel.sdk import estimate_tokens


def escape_untrusted(text: str) -> str:
    # 04 §3.8：防止被管系统里的内容伪造闭合标签、提前结束不可信区块
    return text.replace("<untrusted", "&lt;untrusted").replace("</untrusted", "&lt;/untrusted")


def wrap(source: str, text: str) -> str:
    return f'<untrusted source="{source}">\n{escape_untrusted(text)}\n</untrusted>'


def _finding_line(f: Finding) -> str:
    metrics = " ".join(f"{k}={v:g}" for k, v in sorted(f.metrics.items()))
    return (
        f"{f.id} [{f.severity}] {f.detector} object={f.object_ref or '-'}: {f.summary} ({metrics}) "
        f"evidence={','.join(f.observation_ids)}"
    )


def _fact_line(x: Fact) -> str:
    return f"{x.status:<8} {x.subject} {x.key}={x.value} baseline={x.baseline} ({x.observation_id})"


def _truncate(text: str, tokens: int) -> str:
    while estimate_tokens(text) > tokens and len(text) > 1:
        text = text[: int(len(text) * 0.9)]
    return text


@dataclass
class _Entry:
    layer: str  # L0 / L1 / L3 / L4 / L5
    ref: str
    text: str
    protected: bool  # 不可删（04 §3.7）：L0 全部、L1 的异常项、L5 的 top-1
    included: bool = True
    drop_reason: str | None = None

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


def _fit_layer(entries: list[_Entry], budget: int) -> None:
    """单层预算：按顺序放，受保护的条目总是放；放不下的标记为 budget。"""
    used = 0
    for e in entries:
        if e.protected or used + e.tokens <= budget:
            used += e.tokens
        else:
            e.included, e.drop_reason = False, "budget"


def _degrade(layers: dict[str, list[_Entry]], total: int, order: Sequence[str]) -> None:
    """04 §3.7：总量超出时，按降级顺序从各层的末尾往前删（L3 末尾是最早的变更；L5 保留 top-1）。"""

    def used() -> int:
        return sum(e.tokens for es in layers.values() for e in es if e.included)

    for step in order:
        layer = "L1" if step == "L1_related" else step
        for e in reversed(layers.get(layer, [])):
            if used() <= total:
                return
            if e.included and not e.protected:
                e.included, e.drop_reason = False, f"degrade:{step}"


def build_context(
    investigation_id: str,
    findings: list[Finding],
    sheets: list[FactSheet],
    env: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    changes: Sequence[Change] = (),
    knowledge: Sequence[KnowledgeHit] = (),
    memories: Sequence[MemoryHit] = (),
    raw: Mapping[str, tuple[str, Any]] | None = None,
) -> ContextBundle:
    """raw 不为空时是 E1 实验的对照组（04 §5）：L1 不放事实卡片，改放事实来源观测的原始 JSON（截断）。"""
    budgets = policy["budgets"]
    layers: dict[str, list[_Entry]] = {}

    # L0：问题事件，全部保留
    layers["L0"] = [_Entry("L0", f.id, _finding_line(f), protected=True) for f in findings]
    # L1：异常项排在前面先占预算，并且不可删；其余是"和问题相关的关键项"，降级时最后才删
    facts = sorted((x for s in sheets for x in s.facts), key=lambda x: x.status != "abnormal")
    if raw is None:
        layers["L1"] = [_Entry("L1", x.observation_id, _fact_line(x), protected=x.status == "abnormal") for x in facts]
    else:
        # 每条观测平分 L1 预算，用和 _fit_layer 相同的 estimate_tokens 截断；原始数据没有"异常项"，都不保护
        per = budgets["L1"] // max(1, len(raw))
        layers["L1"] = [
            _Entry(
                "L1", oid, _truncate(f"{oid} {tool}: {json.dumps(r, ensure_ascii=False, sort_keys=True)}", per), False
            )
            for oid, (tool, r) in raw.items()
        ]
    # L5：按相似度排好序；top-1 不可删（04 §3.7）
    layers["L5"] = [_Entry("L5", m.memory_id, render_memory(m), protected=i == 0) for i, m in enumerate(memories)]
    # L3：最新的变更在前，降级时从末尾（最早的）删
    layers["L3"] = [
        _Entry("L3", c.observation_id, c.line(), protected=False)
        for c in sorted(changes, key=lambda c: c.since, reverse=True)
    ]
    # L4：整层可以删
    layers["L4"] = [_Entry("L4", f"knowledge/{h.doc_id}", render_knowledge(h), protected=False) for h in knowledge]

    for name, entries in layers.items():
        _fit_layer(entries, budgets[name])
    _degrade(layers, budgets.get("preassembled_total", 10**9), policy.get("degrade_order", []))
    # TODO(edge): L1 的单条异常项本身超出预算时仍然保留（异常项不可删），预算会被突破；上下文报告里能看到

    def kept(name: str) -> list[str]:
        return [e.text for e in layers[name] if e.included]

    # 顺序（04 §2.1）：最确定的在前，记忆紧跟事实，知识最可替代、放在最后
    sections = ["## L0 问题事件\n" + wrap("findings", "\n".join(kept("L0")))]
    omitted = sum(s.omitted_normal_count for s in sheets)
    env_line = " · ".join(f"{k}={v}" for k, v in sorted(env.items()) if v not in (None, [], {}))
    l1_title = (
        f"## L1 事实卡片（省略正常项 {omitted} 条，表示这些指标都正常）\n"
        if raw is None
        else "## L1 原始数据（工具的原始输出，按预算截断）\n"
    )
    sections.append(
        l1_title + wrap("facts", "\n".join(kept("L1"))) + (f"\n环境（人工维护）：{env_line}" if env_line else "")
    )
    if kept("L5"):
        # 记忆的文本由系统模板生成（05 §3.1），不是被管系统的原文
        sections.append("## L5 相似事故（待验证的假设，不是结论）\n" + "\n".join(kept("L5")))
    if kept("L3"):
        window = policy.get("changes", {}).get("window_hours", 24)
        sections.append(
            f"## L3 最近的变更（{window} 小时内，和上一次快照比较）\n" + wrap("changes", "\n".join(kept("L3")))
        )
    if kept("L4"):
        # runbook 来自专家包（git），不是被管系统的内容，不需要不可信包装
        sections.append("## L4 知识（runbook，按检测器检索）\n" + "\n\n".join(kept("L4")))

    items = [
        ContextItem(layer=e.layer, ref=e.ref, tokens=e.tokens, included=e.included, drop_reason=e.drop_reason)
        for name in ("L0", "L1", "L5", "L3", "L4")
        for e in layers[name]
    ]
    tokens_by_layer = {
        name: sum(e.tokens for e in es if e.included) for name, es in layers.items() if es or name in ("L0", "L1")
    }
    policy_hash = "sha256:" + hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
    return ContextBundle(
        investigation_id=investigation_id,
        policy_hash=policy_hash,
        items=items,
        tokens_by_layer=tokens_by_layer,
        rendered="\n\n".join(sections),
    )
