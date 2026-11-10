"""组装依赖并跑一次调查。线上（CLI 的 `sentinel patrol`、API 的 `POST /patrol`，02 §2）和评测运行器都调用这里，
保证"评测测的系统"和"线上跑的系统"是同一套组装（M1 独立审阅发现 3）。

负责：make_deps（组装 Deps 的唯一位置）、investigate（跑图）、patrol（线上入口：Postgres 存储和 checkpointer）。
不负责：图的逻辑（investigation.py）。测试仍然可以自己组装 Deps（内存存储、回放、假模型）。
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langchain_core.tracers.langchain import wait_for_all_tracers
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from ulid import ULID

from sentinel.contracts import AgentVersion
from sentinel.core.context.knowledge_layer import KnowledgeRetriever, NoKnowledge
from sentinel.core.detect import load_env
from sentinel.core.graph.investigation import Deps, InvState, build_graph
from sentinel.core.knowledge.corpus import knowledge_hash
from sentinel.core.knowledge.es_index import EsKnowledge
from sentinel.core.llm.chat import chat_model
from sentinel.core.llm.embed import embed
from sentinel.core.observations.store import ObservationStore
from sentinel.core.packs_registry import LoadedPack, load_pack
from sentinel.core.store import Store, open_store
from sentinel.core.toolbackend import Mode, ToolBackend, open_backend
from sentinel.core.versioning.components import behavior_dirty, collect_components, load_context_policy
from sentinel.core.versioning.freeze import require_frozen

OBS_ROOT = Path(os.environ.get("SENTINEL_OBS_ROOT", "var/observations"))
PACKS = ("es",)  # TODO(edge): 从配置或 entry point 发现专家包（M4 加 mysql 时）


def load_packs() -> dict[str, LoadedPack]:
    return {name: load_pack(name) for name in PACKS}


def current_version(store: Store) -> AgentVersion:
    """当前检出的代码对应的已冻结版本；没冻结或有未提交的改动就抛 FreezeError（ADR-012）。"""
    return require_frozen(store, collect_components(list(load_packs().values())), dirty=behavior_dirty())


def make_deps(
    store: Store,
    obs_store: ObservationStore,
    mode: Mode,
    fixture: Path | None,
    *,
    version: Callable[[], AgentVersion],
    make_model: Callable[[str], BaseChatModel] = chat_model,
) -> Deps:
    """组装 Deps 的唯一位置。M2 加上下文层和记忆服务时只改这里，线上和评测一起生效。"""
    packs = load_packs()
    policy = load_context_policy()

    def make_backend(pack: LoadedPack, inv_id: str) -> ToolBackend:
        return open_backend(pack, inv_id, mode, obs_store, fixture)

    knowledge: KnowledgeRetriever = NoKnowledge()
    if policy.get("layers", {}).get("L4"):
        # 索引和当前 runbook 不一致就直接报错，不悄悄用旧知识（EsKnowledge 构造时校验）
        # TODO(edge): 多个专家包时按问题事件的领域选知识库
        knowledge = EsKnowledge("es", knowledge_hash(packs["es"]), embed)
    return Deps(
        store=store,
        obs_store=obs_store,
        packs=packs,
        make_backend=make_backend,
        make_model=make_model,
        current_version=version,
        load_env=load_env,
        policy=policy,
        knowledge=knowledge,
    )


def investigate(
    deps: Deps,
    target: str,
    pack: str,
    checkpointer: BaseCheckpointSaver[Any],
    run_config: dict[str, Any] | None = None,
) -> InvState:
    inv_id = f"inv_{ULID()}"
    graph = build_graph(deps, checkpointer)
    try:
        # thread_id = 调查 ID（01 §3.2）：以后 approve / confirm 的 interrupt 恢复时用它找回状态
        config: RunnableConfig = {**(run_config or {}), "configurable": {"thread_id": inv_id}}  # type: ignore[typeddict-item]
        final: InvState = graph.invoke(  # type: ignore[assignment]
            {"inv_id": inv_id, "target": target, "pack": pack, "mode": "report_only"}, config
        )
    finally:
        for b in deps._backends.values():
            b.close()
    return {**final, "inv_id": inv_id}


def patrol(target: str, pack: str = "es", mode: Mode = Mode.LIVE, fixture: Path | None = None) -> InvState:
    store = open_store()
    deps = make_deps(
        store, ObservationStore(OBS_ROOT, index=store), mode, fixture, version=lambda: current_version(store)
    )
    url = os.environ.get("SENTINEL_PG_URL", "postgresql://postgres:sentinel-local@localhost:5432/sentinel")
    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()
        try:
            return investigate(
                deps,
                target,
                pack,
                saver,
                {
                    # 外层图的 trace 也带上调查信息，才能在 LangSmith 里按调查找到整棵调用树（01 §3.4）
                    "run_name": "investigation",
                    "tags": [f"target:{target}", f"backend:{mode.value}"],
                    "metadata": {"target": target, "backend": mode.value},
                },
            )
        finally:
            # CLI 进程很快就退出，后台线程里还没上传的 trace 会丢（D3 实测丢了 3 次调查的 trace）
            wait_for_all_tracers()
