"""证据支持率的确定性检查（03 §4.1）。golden-demo 第 3 步和 D4 的评分器都用它。

一个论断"有证据支持"当且仅当：
1. 它引用的每个观测 ID 都存在，并且属于本次调查；
2. 如果填了 quote，它是某条被引用观测的摘要的子串。
   模型看到的工具返回是 "[状态] 摘要"（agent.lc_tools 的包装），提示词又要求从中逐字复制，
   所以 quote 开头正好是这条观测的状态前缀时，去掉前缀再比对（教程 03 §3.8：v36 的 22 条失败论断里 21 条是这个原因）。
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
    # 检查论断引用的每个观测 ID 都存在，并且属于本次调查
    for oid in c.evidence:
        o = obs.get(oid)
        if o is None:
            return False, f"{oid} 不存在"
        if o.investigation_id != investigation_id:
            return False, f"{oid} 属于另一次调查"
    # 检查 quote 是否是某条被引用观测的摘要的子串
    if c.quote:
        q = _normalize(c.quote)
        if not any(_strip_status(q, obs[oid]) in _normalize(obs[oid].digest) for oid in c.evidence):
            return False, "quote 不是被引用观测摘要的子串"
    return True, ""


def _strip_status(q: str, o: Observation) -> str:
    # 只去掉和这条观测真实状态一致的前缀：[ok] 引到失败的观测上照样不通过
    return q.removeprefix(f"[{o.status}] ")


def check_evidence(d: Diagnosis, observations: list[Observation]) -> list[ClaimCheck]:
    obs = {o.id: o for o in observations}
    return [ClaimCheck(h.id, c.id, *check_claim(c, obs, d.investigation_id)) for h in d.hypotheses for c in h.claims]


def support_rate(checks: list[ClaimCheck]) -> float:
    return sum(c.supported for c in checks) / len(checks) if checks else 0.0
