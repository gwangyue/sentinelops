"""检测器和 fact builder 的签名，以及它们共用的帮助函数（03 §3.2、§3.4；04 §3.2；02 §2）。

负责：DetectContext（检测器能拿到的全部东西）、Finding 的 ID 和指纹生成、事实卡片的筛选。
不负责：具体领域的判断规则（在各专家包里）。
"""

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from ulid import ULID

from sentinel.contracts import Fact, FactSheet, Finding
from sentinel.sdk.tool import ToolCaller

Severity = Literal["info", "warn", "critical"]


@dataclass(frozen=True)
class DetectContext:
    pack: str
    target: str  # 集群标识，例如 "es-target"
    tools: ToolCaller
    env: Mapping[str, Any]  # 环境记忆 env/<target>.yaml（05 §2），由人维护


Detector = Callable[[DetectContext], list[Finding]]
FactBuilder = Callable[[DetectContext], FactSheet]


def fingerprint(target: str, detector: str, object_ref: str | None, severity: str) -> str:
    """02 §2：指纹 = hash(集群, 检测器, 对象, 严重程度)，巡检去重用。不含数值，91% 和 93% 是同一个问题。"""
    return hashlib.sha256(f"{target}|{detector}|{object_ref or ''}|{severity}".encode()).hexdigest()[:32]


def make_finding(
    ctx: DetectContext,
    detector: str,
    *,
    severity: Severity,
    summary: str,
    metrics: Mapping[str, float],
    observation_ids: Iterable[str],
    object_ref: str | None = None,
) -> Finding:
    return Finding(
        id=f"fnd_{ULID()}",
        fingerprint=fingerprint(ctx.target, detector, object_ref, severity),
        pack=ctx.pack,
        detector=detector,
        target=ctx.target,
        object_ref=object_ref,
        severity=severity,
        summary=summary,
        metrics=dict(metrics),
        observed_at=datetime.now(UTC),
        observation_ids=list(observation_ids),
    )


def assemble_fact_sheet(target: str, facts: Iterable[Fact], always_keep: Callable[[Fact], bool]) -> FactSheet:
    """04 §3.2 的筛选：保留异常项、always_keep 选中的关键项、以及和异常对象相关的未知项；正常项只计数。"""
    facts = list(facts)
    abnormal_subjects = {f.subject for f in facts if f.status == "abnormal"}
    kept: list[Fact] = []
    omitted_normal = 0
    for f in facts:
        if f.status == "abnormal" or always_keep(f) or (f.status == "unknown" and f.subject in abnormal_subjects):
            kept.append(f)
        elif f.status == "normal":
            omitted_normal += 1
        # TODO(edge): 和异常对象无关的 unknown 项直接丢弃、不计数；M2 有了滚动基线后 unknown 会变少
    return FactSheet(target=target, generated_at=datetime.now(UTC), facts=kept, omitted_normal_count=omitted_normal)
