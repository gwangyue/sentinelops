"""调查图的流程测试夹具：内存存储、回放录制、假模型、InMemorySaver。不访问网络，不调用真实 LLM。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from ulid import ULID

from sentinel.core.detect import load_env
from sentinel.core.graph.investigation import Deps, InvState, build_graph
from sentinel.core.llm.fake import Script, ScriptedChatModel
from sentinel.core.observations.store import ObservationStore
from sentinel.core.packs_registry import LoadedPack, load_pack
from sentinel.core.store import MemoryStore
from sentinel.core.toolbackend import Mode, ToolBackend
from sentinel.core.versioning.components import collect_components, load_context_policy
from sentinel.core.versioning.freeze import freeze, require_frozen

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "es"
PACK = load_pack("es")


def _must_not_be_called(messages: object) -> Any:
    raise AssertionError("这条路径不该调用模型")


class Harness:
    def __init__(self, tmp: Path, fixture: str, script: Script | None, policy: dict[str, Any] | None = None) -> None:
        self.store = MemoryStore()
        self.obs = ObservationStore(tmp / "obs", index=self.store)
        self.model = ScriptedChatModel(script=script or _must_not_be_called)
        comps = collect_components([PACK], code_sha="test")
        freeze(self.store, comps, "test version")
        packs = {"es": PACK}

        def make_backend(pack: LoadedPack, inv: str) -> ToolBackend:
            return ToolBackend(pack.tools, self.obs, inv, Mode.REPLAY, fixture=FIX / f"{fixture}.jsonl")

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

    def run(self) -> InvState:
        inv = f"inv_{ULID()}"
        g = build_graph(self.deps, InMemorySaver())
        out: InvState = g.invoke(
            {"inv_id": inv, "target": "es-target", "pack": "es", "mode": "report_only"},
            {"configurable": {"thread_id": inv}},
        )
        return out

    def audit_types(self, inv: str) -> list[str]:
        return [e.type for e in self.store.audit_events(inv)]


@pytest.fixture
def harness(tmp_path: Path) -> Callable[..., Harness]:
    return lambda fixture, script=None, policy=None: Harness(tmp_path, fixture, script, policy)
