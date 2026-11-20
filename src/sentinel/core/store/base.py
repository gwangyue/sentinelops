"""存储接口（01 §2 第⑤层 Postgres：checkpoint 之外的调查、审计、版本、观测索引）。

负责：Store 协议、调查记录的行结构。审计哈希链的计算规则在 governance/audit.py。
不负责：checkpoint（LangGraph 的 PostgresSaver 自己管）、原始观测输出（文件，见 core/observations）。
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from sentinel.contracts import AgentVersion, AuditEvent, Observation

# awaiting_approval / resolved：修复段（M3）。resolved = 执行并验证通过
InvestigationStatus = Literal[
    "running", "healthy", "deduplicated", "diagnosed", "escalated", "failed", "awaiting_approval", "resolved"
]


@dataclass
class InvestigationRecord:
    """一次调查的汇总行。字段都是契约模型的 JSON，本身不是契约（03 篇没有定义调查实体的结构）。"""

    id: str
    target: str
    mode: str  # report_only（巡检）| remediate（M3）
    agent_version: str
    status: InvestigationStatus
    created_at: datetime
    findings: list[dict[str, Any]] = field(default_factory=list)
    fact_sheets: list[dict[str, Any]] = field(default_factory=list)
    context_bundle: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None
    escalate_reason: str | None = None


class Store(Protocol):
    def init(self) -> None: ...

    # 调查
    def create_investigation(self, rec: InvestigationRecord) -> None: ...
    def update_investigation(self, inv_id: str, **fields: Any) -> None: ...
    def get_investigation(self, inv_id: str) -> InvestigationRecord | None: ...
    def list_investigations(self, limit: int = 20) -> list[InvestigationRecord]: ...
    # 去重窗口（02 §2）：since 之后、这个目标上的调查出现过的问题事件指纹。不算被判为重复（deduplicated）的调查：
    # 它们也存了问题事件，算进来的话，巡检间隔 < 窗口时窗口会一路往后滑，持续存在的问题只诊断一次
    # （教程 03 §3.3 实验 E，2026-11-18 修）。现在的语义：同一个问题最多每个窗口诊断一次。
    def recent_fingerprints(self, target: str, since: datetime, exclude: str) -> set[str]: ...

    # 审计：只追加。prev_hash 由存储层在同一个事务里计算，调用方不能自己指定
    def append_audit(
        self,
        *,
        investigation_id: str | None,
        agent_version: str | None,
        actor: str,
        type: str,
        payload: dict[str, Any],
    ) -> AuditEvent: ...
    def audit_events(self, investigation_id: str | None = None) -> list[AuditEvent]: ...
    # 推送用的增量读取（09 §4.2）。AuditEvent 契约里没有 seq，所以返回 (seq, 事件)，不改契约。
    # seq 单调递增、追加时整表加锁，按 seq 读不会漏、不会乱序
    def audit_events_since(
        self, after_seq: int, types: Collection[str], limit: int = 100
    ) -> list[tuple[int, AuditEvent]]: ...
    def latest_audit_seq(self) -> int: ...

    # 推送游标：每个环境一行（09 §4.2）。None 表示这个环境还没推过
    def get_notify_cursor(self, env: str) -> int | None: ...
    def set_notify_cursor(self, env: str, seq: int) -> None: ...

    # Agent 版本
    def get_version(self, hash_or_seq: str) -> AgentVersion | None: ...
    def latest_version(self) -> AgentVersion | None: ...
    def insert_version(self, v: AgentVersion) -> None: ...
    def list_versions(self) -> list[AgentVersion]: ...

    # 配置快照（L3 变更层的"上一次快照"，04 §3.4；05 §3.5）
    def latest_snapshot(self, target: str, tool: str) -> tuple[Any, datetime] | None: ...
    def save_snapshot(self, target: str, tool: str, raw: Any, ts: datetime) -> None: ...

    # 观测索引（原始输出是文件）
    def add_observation(self, obs: Observation) -> None: ...
    def observations(self, investigation_id: str) -> list[Observation]: ...
