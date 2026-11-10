"""上下文组装（04 §2.2、§3.7–3.9）。M1 只组装 L0（问题事件）和 L1（事实卡片 + 环境）。

负责：按 context_policy 的层预算放入条目、记录被删掉的条目（上下文报告）、
      把来自被管系统的文本包进 <untrusted> 并转义、产出 ContextBundle。
不负责：L3 变更、L4 知识、L5 相似事故（M2 D5–D7）；L2 下钻在诊断过程中由工具调用产生。
整条流水线没有 LLM 调用（P1）。
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sentinel.contracts import ContextBundle, ContextItem, Fact, FactSheet, Finding
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


def build_context(
    investigation_id: str,
    findings: list[Finding],
    sheets: list[FactSheet],
    env: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> ContextBundle:
    budgets = policy["budgets"]
    items: list[ContextItem] = []
    sections: list[str] = []

    # L0：问题事件，全部保留（04 §3.7"不可删"）
    l0 = [_finding_line(f) for f in findings]
    for f, line in zip(findings, l0, strict=True):
        items.append(ContextItem(layer="L0", ref=f.id, tokens=estimate_tokens(line), included=True, drop_reason=None))
    sections.append("## L0 问题事件\n" + wrap("findings", "\n".join(l0)))

    # L1：异常项不可删；其余按预算从后往前删（04 §3.7 降级顺序：先删和问题无关的关键项）
    budget = budgets["L1"]
    kept: list[str] = []
    used = 0
    facts = [x for s in sheets for x in s.facts]
    ordered = sorted(facts, key=lambda x: x.status != "abnormal")  # 异常项排在前面，先占预算
    for x in ordered:
        line = _fact_line(x)
        t = estimate_tokens(line)
        if x.status == "abnormal" or used + t <= budget:
            kept.append(line)
            used += t
            items.append(ContextItem(layer="L1", ref=x.observation_id, tokens=t, included=True, drop_reason=None))
        else:
            items.append(ContextItem(layer="L1", ref=x.observation_id, tokens=t, included=False, drop_reason="budget"))
    omitted = sum(s.omitted_normal_count for s in sheets)
    env_line = " · ".join(f"{k}={v}" for k, v in sorted(env.items()) if v not in (None, [], {}))
    sections.append(
        f"## L1 事实卡片（省略正常项 {omitted} 条，表示这些指标都正常）\n"
        + wrap("facts", "\n".join(kept))
        + (f"\n环境（人工维护）：{env_line}" if env_line else "")
    )
    # TODO(edge): L1 的单条异常项本身超出预算时仍然保留（异常项不可删），预算会被突破；上下文报告里能看到

    rendered = "\n\n".join(sections)
    tokens_by_layer = {layer: sum(i.tokens for i in items if i.layer == layer and i.included) for layer in ("L0", "L1")}
    policy_hash = "sha256:" + hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
    return ContextBundle(
        investigation_id=investigation_id,
        policy_hash=policy_hash,
        items=items,
        tokens_by_layer=tokens_by_layer,
        rendered=rendered,
    )
