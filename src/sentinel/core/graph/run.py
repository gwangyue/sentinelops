"""组装依赖并跑一次调查。线上（CLI 的 `sentinel patrol`、API 的 `POST /patrol`，02 §2）和评测运行器都调用这里，
保证"评测测的系统"和"线上跑的系统"是同一套组装（M1 独立审阅发现 3）。

负责：make_deps（组装 Deps 的唯一位置）、investigate（跑图）、resume（审批之后从 interrupt 恢复）、
      patrol（线上入口：Postgres 存储和 checkpointer）、queue_executor（线上：执行请求写进队列，等独立的执行器进程）。
不负责：图的逻辑（investigation.py）。测试仍然可以自己组装 Deps（内存存储、回放、假模型）。
"""

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langchain_core.tracers.langchain import wait_for_all_tracers
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from ulid import ULID

from sentinel.contracts import AgentVersion, ExecutionResult
from sentinel.core.context.knowledge_layer import KnowledgeRetriever, NoKnowledge
from sentinel.core.context.memory_layer import MemoryProvider, NoMemory
from sentinel.core.detect import load_env
from sentinel.core.graph.investigation import Deps, InvState, build_graph
from sentinel.core.graph.remediate import Remediation
from sentinel.core.knowledge.corpus import knowledge_hash
from sentinel.core.knowledge.es_index import EsKnowledge
from sentinel.core.llm.chat import chat_model
from sentinel.core.llm.embed import embed
from sentinel.core.memory.policy import load_memory_policy
from sentinel.core.memory.provider import StoreMemoryProvider
from sentinel.core.memory.store import EsMemoryStore
from sentinel.core.observations.store import ObservationStore
from sentinel.core.packs_registry import LoadedPack, load_pack
from sentinel.core.store import Store, open_store
from sentinel.core.toolbackend import Mode, ToolBackend, open_backend
from sentinel.core.versioning.components import behavior_dirty, collect_components, load_context_policy
from sentinel.core.versioning.freeze import require_frozen
from sentinel.governance.kill_switch import PgFreezeState
from sentinel.governance.remediation_store import ExecKind, PgRemediationStore, RemediationStore

OBS_ROOT = Path(os.environ.get("SENTINEL_OBS_ROOT", "var/observations"))
PG_URL = os.environ.get("SENTINEL_PG_URL", "postgresql://postgres:sentinel-local@localhost:5432/sentinel")
EXEC_WAIT_S = 300  # 等执行器的上限：执行器进程没起来时，调查不会永远卡住
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
    memory: MemoryProvider | None = None,
    remediation: Remediation | None = None,
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
    if memory is None:
        memory = NoMemory()
        if policy.get("layers", {}).get("L5"):
            # 线上：每个领域一个记忆索引，命名空间（领域/集群）在检索时按问题事件区分
            memory = StoreMemoryProvider(EsMemoryStore("incidents-es"), None, load_memory_policy(), embed)
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
        memory=memory,
        remediation=remediation,
    )


def investigate(
    deps: Deps,
    target: str,
    pack: str,
    checkpointer: BaseCheckpointSaver[Any],
    run_config: dict[str, Any] | None = None,
    mode: str = "report_only",
) -> InvState:
    """跑一次调查。修复模式下会停在 approve 节点（返回的状态里有 __interrupt__），用 resume 继续。"""
    inv_id = f"inv_{ULID()}"
    return _run(
        deps, inv_id, checkpointer, run_config, {"inv_id": inv_id, "target": target, "pack": pack, "mode": mode}
    )


def resume(
    deps: Deps, inv_id: str, checkpointer: BaseCheckpointSaver[Any], run_config: dict[str, Any] | None = None
) -> InvState:
    """审批之后继续（02 §3.2 Command(resume=...)）。只带审批这件事发生了，不带任何内容：
    审批结果由 execute 节点自己从存储里读（02 §3.4"审批不经过 LLM"）。"""
    return _run(deps, inv_id, checkpointer, run_config, Command(resume={"resumed": True}))


def _run(
    deps: Deps, inv_id: str, checkpointer: BaseCheckpointSaver[Any], run_config: dict[str, Any] | None, inp: Any
) -> InvState:
    graph = build_graph(deps, checkpointer)
    try:
        # thread_id = 调查 ID（01 §3.2）：approve 的 interrupt 恢复时用它找回状态
        config: RunnableConfig = {**(run_config or {}), "configurable": {"thread_id": inv_id}}  # type: ignore[typeddict-item]
        final: InvState = graph.invoke(inp, config)  # type: ignore[assignment]
    finally:
        for b in deps._backends.values():
            b.close()
    return {**final, "inv_id": inv_id}


def queue_executor(
    store: RemediationStore, wait_s: float = EXEC_WAIT_S
) -> Callable[[ExecKind, str, str, str], ExecutionResult]:
    """线上的执行器接口：只把三个 ID 写进队列（ADR-005），然后等独立的执行器进程写回结果。"""

    def run(kind: ExecKind, plan_id: str, approval_id: str, key: str) -> ExecutionResult:
        if (prev := store.get_execution(key)) is not None:
            return prev  # 恢复、重试：同一个键已经执行过，直接拿上次的结果
        store.request_execution(kind, plan_id, approval_id, key)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if (r := store.get_execution(key)) is not None:
                return r
            time.sleep(1)
        # TODO(edge)：超时之后执行器可能还会执行；现在升级给人，由人去看执行器日志
        return ExecutionResult(
            plan_id=plan_id,
            approval_id=approval_id,
            idempotency_key=key,
            status="failed",
            output={"error": f"{wait_s:.0f} 秒内执行器没有返回结果（执行器进程在运行吗？）"},
            verify_passed=None,
        )

    return run


def _online_deps(mode: Mode, fixture: Path | None) -> Deps:
    store = open_store()
    rem = PgRemediationStore(PG_URL)
    return make_deps(
        store,
        ObservationStore(OBS_ROOT, index=store),
        mode,
        fixture,
        version=lambda: current_version(store),
        remediation=Remediation(store=rem, executor=queue_executor(rem), frozen=PgFreezeState(PG_URL).is_frozen),
    )


def _trace(target: str, backend: str) -> dict[str, Any]:
    # 外层图的 trace 也带上调查信息，才能在 LangSmith 里按调查找到整棵调用树（01 §3.4）
    return {
        "run_name": "investigation",
        "tags": [f"target:{target}", f"backend:{backend}"],
        "metadata": {"target": target, "backend": backend},
    }


def patrol(
    target: str, pack: str = "es", mode: Mode = Mode.LIVE, fixture: Path | None = None, inv_mode: str = "report_only"
) -> InvState:
    deps = _online_deps(mode, fixture)
    with PostgresSaver.from_conn_string(PG_URL) as saver:
        saver.setup()
        try:
            return investigate(deps, target, pack, saver, _trace(target, mode.value), mode=inv_mode)
        finally:
            # CLI 进程很快就退出，后台线程里还没上传的 trace 会丢（D3 实测丢了 3 次调查的 trace）
            wait_for_all_tracers()


def resume_online(inv_id: str, mode: Mode = Mode.LIVE, fixture: Path | None = None) -> InvState:
    """`sentinel approvals approve` 之后调用：用同一个 thread_id 从 Postgres 的 checkpoint 继续。"""
    deps = _online_deps(mode, fixture)
    rec = deps.store.get_investigation(inv_id)
    with PostgresSaver.from_conn_string(PG_URL) as saver:
        saver.setup()
        try:
            return resume(deps, inv_id, saver, _trace(rec.target if rec else "?", mode.value))
        finally:
            wait_for_all_tracers()
