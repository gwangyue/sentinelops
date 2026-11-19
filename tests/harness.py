"""调查图的测试夹具（tests/flows、tests/unit 和 demo-check 共用）。

内存存储、回放录制、假模型、InMemorySaver：不访问网络，不调用真实 LLM。
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from ulid import ULID

from sentinel.contracts import Diagnosis
from sentinel.core.detect import load_env
from sentinel.core.graph.confirm import Memorizer, record_confirmation
from sentinel.core.graph.investigation import Deps, InvState, build_graph
from sentinel.core.graph.remediate import record_decision
from sentinel.core.llm.fake import Script, ScriptedChatModel
from sentinel.core.memory.policy import load_memory_policy
from sentinel.core.memory.store import LocalMemoryStore
from sentinel.core.observations.store import ObservationStore
from sentinel.core.packs_registry import LoadedPack, load_pack
from sentinel.core.store import MemoryStore
from sentinel.core.toolbackend import Mode, ToolBackend
from sentinel.core.versioning.components import collect_components, load_context_policy
from sentinel.core.versioning.freeze import freeze, require_frozen
from sentinel.eval.replay_exec import replay_remediation
from sentinel.governance.kill_switch import MemoryFreezeState
from sentinel.governance.remediation_store import MemoryRemediationStore
from tests.unit.test_memory import fake_embed

FIX = Path(__file__).resolve().parent / "fixtures" / "es"
PACK = load_pack("es")


def _must_not_be_called(messages: object) -> Any:
    raise AssertionError("这条路径不该调用模型")


class Harness:
    def __init__(
        self,
        tmp: Path,
        fixture: str | Path,
        script: Script | None,
        policy: dict[str, Any] | None = None,
        remediate: bool = False,
    ) -> None:
        self.store = MemoryStore()
        self.obs = ObservationStore(tmp / "obs", index=self.store)
        self.model = ScriptedChatModel(script=script or _must_not_be_called)
        comps = collect_components([PACK], code_sha="test")
        freeze(self.store, comps, "test version")
        packs = {"es": PACK}

        fx = fixture if isinstance(fixture, Path) else FIX / f"{fixture}.jsonl"

        def make_backend(pack: LoadedPack, inv: str) -> ToolBackend:
            return ToolBackend(pack.tools, self.obs, inv, Mode.REPLAY, fixture=fx)

        # 修复段：内存里的修复存储 + 回放执行器（和评测同一套组装，eval/replay_exec.py）
        # 和线上一样，同一条审计链；读也读这条链（同一集群的修复互斥要看调查写的 verify.* 和 investigation.closed）
        self.rem_store = MemoryRemediationStore(
            audit_sink=self.store.append_audit, audit_reader=lambda: self.store.audit_events()
        )
        self.freeze_state = MemoryFreezeState()  # 紧急停止（07 §7）
        self.saver = InMemorySaver()  # run 和 resume 共用：interrupt 之后要能找回同一个线程的状态

        self.deps = Deps(
            store=self.store,
            obs_store=self.obs,
            packs=packs,
            make_backend=make_backend,
            make_model=lambda purpose: self.model,
            current_version=lambda: require_frozen(self.store, collect_components([PACK], code_sha="test")),
            load_env=load_env,
            policy=policy or load_context_policy(),
        )
        if remediate:
            self.deps.remediation = replay_remediation(
                self.rem_store, PACK.actions, lambda inv: self.deps.backend(PACK, inv), self.freeze_state.is_frozen
            )
            # 确认段（M3 D12）：本地记忆索引 + 假向量（测试不访问网络）
            self.mem_index = LocalMemoryStore()
            self.deps.memorizer = Memorizer(store=self.mem_index, embed=fake_embed, policy=load_memory_policy())

    def run(self, mode: str = "report_only") -> InvState:
        inv = f"inv_{ULID()}"
        g = build_graph(self.deps, self.saver)
        out: InvState = g.invoke(
            {"inv_id": inv, "target": "es-target", "pack": "es", "mode": mode},  # type: ignore[typeddict-item]
            {"configurable": {"thread_id": inv}},
        )
        return {**out, "inv_id": inv}

    def decide(self, decision: Literal["approved", "rejected"] = "approved", who: str = "human:t") -> None:
        """模拟 CLI 的 approvals approve / reject：同一个 record_decision。"""
        for req in self.rem_store.list_approval_requests():
            if self.rem_store.get_decision(req.id) is None:
                record_decision(self.rem_store, self.store.append_audit, req.id, decision, who, None)

    def confirm(self, inv: str, *, hypothesis: str | None = "H1", label: str | None = None) -> None:
        """模拟 CLI 的 investigations confirm：同一个 record_confirmation。"""
        rec = self.store.get_investigation(inv)
        assert rec is not None and rec.diagnosis is not None
        record_confirmation(
            self.store.append_audit,
            inv_id=inv,
            diagnosis=Diagnosis.model_validate(rec.diagnosis),
            hypothesis=None if label else hypothesis,
            label=label,
            who="human:t",
            valid_labels=[r.label for r in PACK.manifest.root_causes],
        )

    def resume(self, inv: str) -> InvState:
        g = build_graph(self.deps, self.saver)
        out: InvState = g.invoke(Command(resume={"resumed": True}), {"configurable": {"thread_id": inv}})
        return out

    def audit_types(self, inv: str) -> list[str]:
        return [e.type for e in self.store.audit_events(inv)]


@pytest.fixture
def harness(tmp_path: Path) -> Callable[..., Harness]:
    return lambda fixture, script=None, policy=None, remediate=False: Harness(
        tmp_path, fixture, script, policy, remediate
    )
