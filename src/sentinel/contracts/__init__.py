"""契约：03 篇的全部数据结构（15 个，分 4 组）。

写死：修改要先停下来问人，确认后补一条 ADR（CLAUDE.md）。不依赖任何其他 sentinel 模块（01 §2）。
"""

from sentinel.contracts.action import ActionSpec, VerifySpec
from sentinel.contracts.approval import ApprovalDecision, ApprovalRequest, ExecutionResult
from sentinel.contracts.audit import KNOWN_EVENT_TYPES, AuditEvent
from sentinel.contracts.context import ContextBundle, ContextItem
from sentinel.contracts.diagnosis import Claim, Diagnosis, Hypothesis
from sentinel.contracts.eval import EvalExample, ExampleInputs, ExampleMetadata, ExampleOutputs, RunScore
from sentinel.contracts.facts import Fact, FactSheet
from sentinel.contracts.finding import Finding
from sentinel.contracts.memory import MemoryRecord
from sentinel.contracts.observation import Observation
from sentinel.contracts.pack import (
    ConnectionDecl,
    DetectorDecl,
    FactBuilderDecl,
    KnowledgeDecl,
    PackManifest,
    ToolDecl,
)
from sentinel.contracts.plan import RemediationPlan
from sentinel.contracts.scenario import FaultRef, GroundTruth, ScenarioSpec, SymptomWait
from sentinel.contracts.version import AgentVersion, VersionComponents

__all__ = [
    "KNOWN_EVENT_TYPES",
    "ActionSpec",
    "AgentVersion",
    "ApprovalDecision",
    "ApprovalRequest",
    "AuditEvent",
    "Claim",
    "ConnectionDecl",
    "ContextBundle",
    "ContextItem",
    "DetectorDecl",
    "Diagnosis",
    "EvalExample",
    "ExampleInputs",
    "ExampleMetadata",
    "ExampleOutputs",
    "ExecutionResult",
    "Fact",
    "FactBuilderDecl",
    "FactSheet",
    "FaultRef",
    "Finding",
    "GroundTruth",
    "Hypothesis",
    "KnowledgeDecl",
    "MemoryRecord",
    "Observation",
    "PackManifest",
    "RemediationPlan",
    "RunScore",
    "ScenarioSpec",
    "SymptomWait",
    "ToolDecl",
    "VerifySpec",
    "VersionComponents",
]
