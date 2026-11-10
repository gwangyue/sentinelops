"""组装真实依赖并跑一次调查（CLI 的 `sentinel patrol` 和 API 的 `POST /patrol` 都调用这里，02 §2）。

负责：Postgres 存储和 checkpointer、观测存储、工具后端、真实模型、当前版本检查。
不负责：图的逻辑（investigation.py）。测试不走这里，而是自己组装 Deps（内存存储、回放、假模型）。
"""

import os
from pathlib import Path

from langchain_core.tracers.langchain import wait_for_all_tracers
from langgraph.checkpoint.postgres import PostgresSaver
from ulid import ULID

from sentinel.core.detect import load_env
from sentinel.core.graph.investigation import Deps, InvState, build_graph
from sentinel.core.llm.chat import chat_model
from sentinel.core.observations.store import ObservationStore
from sentinel.core.packs_registry import LoadedPack, load_pack
from sentinel.core.store import Store, open_store
from sentinel.core.toolbackend import Mode, ToolBackend, open_backend
from sentinel.core.versioning.components import collect_components, load_context_policy
from sentinel.core.versioning.freeze import require_frozen

OBS_ROOT = Path(os.environ.get("SENTINEL_OBS_ROOT", "var/observations"))
PACKS = ("es",)  # TODO(edge): 从配置或 entry point 发现专家包（M4 加 mysql 时）


def load_packs() -> dict[str, LoadedPack]:
    return {name: load_pack(name) for name in PACKS}


def real_deps(store: Store, mode: Mode, fixture: Path | None) -> Deps:
    packs = load_packs()
    obs_store = ObservationStore(OBS_ROOT, index=store)

    def make_backend(pack: LoadedPack, inv_id: str) -> ToolBackend:
        return open_backend(pack, inv_id, mode, obs_store, fixture)

    return Deps(
        store=store,
        obs_store=obs_store,
        packs=packs,
        make_backend=make_backend,
        make_model=chat_model,
        current_version=lambda: require_frozen(store, collect_components(list(packs.values()))),
        load_env=load_env,
        policy=load_context_policy(),
    )


def patrol(target: str, pack: str = "es", mode: Mode = Mode.LIVE, fixture: Path | None = None) -> InvState:
    store = open_store()
    deps = real_deps(store, mode, fixture)
    inv_id = f"inv_{ULID()}"
    url = os.environ.get("SENTINEL_PG_URL", "postgresql://postgres:sentinel-local@localhost:5432/sentinel")
    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()
        graph = build_graph(deps, saver)
        try:
            # thread_id = 调查 ID（01 §3.2）：以后 approve / confirm 的 interrupt 恢复时用它找回状态
            final: InvState = graph.invoke(  # type: ignore[assignment]
                {"inv_id": inv_id, "target": target, "pack": pack, "mode": "report_only"},
                {
                    "configurable": {"thread_id": inv_id},
                    # 外层图的 trace 也带上调查信息，才能在 LangSmith 里按调查找到整棵调用树（01 §3.4）
                    "run_name": "investigation",
                    "tags": [f"target:{target}", f"backend:{mode.value}"],
                    "metadata": {"investigation_id": inv_id, "target": target, "backend": mode.value},
                },
            )
        finally:
            for b in deps._backends.values():
                b.close()
            # CLI 进程很快就退出，后台线程里还没上传的 trace 会丢（D3 实测丢了 3 次调查的 trace）
            wait_for_all_tracers()
    return {**final, "inv_id": inv_id}
