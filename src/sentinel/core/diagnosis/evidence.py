"""证据支持率的确定性检查（03 §4.1）。golden-demo 第 3 步和 D4 的评分器都用它。

一个论断"有证据支持"当且仅当：
1. 它引用的每个观测 ID 都存在，并且属于本次调查；
2. 如果填了 quote，它是某条被引用观测的摘要的子串。
不负责：证据能不能推出结论（语义问题，留给评审模型，06 篇）。
"""

from dataclasses import dataclass

from sentinel.contracts import Claim, Diagnosis, Observation


@dataclass(frozen=True)
class ClaimCheck:
    hypothesis_id: str
    claim_id: str
    supported: bool
    reason: str  # 不通过时的原因


def _normalize(s: str) -> str:
    # 只合并空白：模型摘引文时常把换行和多个空格合并成一个空格。不做其他宽松匹配，避免把编造的引文算作通过
    return " ".join(s.split())


def check_claim(c: Claim, obs: dict[str, Observation], investigation_id: str) -> tuple[bool, str]:
    for oid in c.evidence:
        o = obs.get(oid)
        if o is None:
            return False, f"{oid} 不存在"
        if o.investigation_id != investigation_id:
            return False, f"{oid} 属于另一次调查"
    if c.quote:
        q = _normalize(c.quote)
        if not any(q in _normalize(obs[oid].digest) for oid in c.evidence):
            return False, "quote 不是被引用观测摘要的子串"
    return True, ""


def check_evidence(d: Diagnosis, observations: list[Observation]) -> list[ClaimCheck]:
    obs = {o.id: o for o in observations}
    return [ClaimCheck(h.id, c.id, *check_claim(c, obs, d.investigation_id)) for h in d.hypotheses for c in h.claims]


def support_rate(checks: list[ClaimCheck]) -> float:
    return sum(c.supported for c in checks) / len(checks) if checks else 0.0
