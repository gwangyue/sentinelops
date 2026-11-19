"""上下文包 ContextBundle（03 §4.5 登记，定义在 04 篇 §3.9）。

被删掉的条目也记录下来（included=False + drop_reason），这就是"上下文报告"。

例子取自本机 Postgres investigations.context_bundle
（调查 inv_01M74E2QD8GN52NJHBDXX1JMYR 与 inv_01M74H8GFR508XMFKZYT1DYVFC）。
"""

from typing import Literal

from sentinel.contracts._base import Contract


# 预组装上下文里的一个条目（一条问题事件、一条事实来源、一条记忆……）。
class ContextItem(Contract):
    layer: Literal["L0", "L1", "L3", "L4", "L5"]  # L2 是诊断中按需拉取的，不在预组装的包里。例："L5"
    # fnd_、obs_、mem_ 或知识文件路径。例："mem_01M74E05SVWD7FTG27A6NMEPN9"、"fnd_01M74H8GR4MZND7YAJ61DPT4TE"
    ref: str
    tokens: int  # 这一条渲染后的 token 数。例：213
    included: bool  # False = 被预算或阈值挡掉。例：True
    # "budget"（超预算）、"degrade:<层>"（总预算超出时按 degrade_order 整层降级，例如 "degrade:L4"）、
    # "below_threshold"、"duplicate" 等。included 为 True 时为 None。例：None、"budget"
    drop_reason: str | None


# 一次调查交给诊断模型的预组装上下文，以及每一条的去留记录。
class ContextBundle(Contract):
    schema_version: int = 1  # 例：1
    investigation_id: str  # 例："inv_01M74E2QD8GN52NJHBDXX1JMYR"
    # 组装策略配置的哈希，也计入 AgentVersion。
    # 例："sha256:70367ac32a6fa210264df15834a6a5fc669798aca5260064021f1b0c0226f4d8"
    policy_hash: str
    items: list[ContextItem]  # 例：L0 1 条 + L1 若干条 + L5 1 条
    tokens_by_layer: dict[str, int]  # 只统计 included 的条目。例：{"L0": 153, "L1": 251, "L5": 213}
    # 最终交给模型的文本，按层分节，外部数据包在 <untrusted> 标签里。
    # 例："## L0 问题事件\n<untrusted source=\"findings\">\nfnd_01M74H8GR4MZND7YAJ61DPT4TE [warn] ..."
    rendered: str
