"""S3 技术验证：最小调查图（08 §1 S3；01 §3.2；04 §3.3）。

负责：验证四件事——
  1. PostgresSaver + interrupt()，进程重启后能用 Command(resume=...) 从审批处继续，且 diagnose 不重跑；
  2. Deep Agent（deepagents）作为外层 LangGraph 的一个节点；
  3. 子 Agent 的中间输出不进入主 Agent 的 messages；
  4. CompositeBackend 把 /obs/ 映射到自定义后端，Agent 能用 read_file 读观测记录。
不负责：真实工具、契约模型、审批记录表、幂等执行——这些在 M1/M3 按设计实现。用完即弃。
"""

import os
from contextlib import asynccontextmanager
from typing import Any, TypedDict

import psycopg
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.protocol import (
    BackendProtocol,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
)
from deepagents.backends.utils import normalize_read_bounds
from fastapi import FastAPI
from langchain_core.messages import messages_to_dict
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

PG_URL = os.environ.get("SPIKE_PG_URL", "postgresql://postgres:spike@localhost:5432/postgres")
MODEL = "gemini-3.7-flash"  # ADR-008：2026-11-09 选定的固定型号

# 用来判断“子 Agent 的原始输出有没有回流到主 Agent”的标记
SUBAGENT_MARKER = "SUBAGENT_RAW_7f3a"
# 用来判断“read_file 是否真的读到了自定义后端”的标记
OBS_MARKER = "OBS_CONTENT_9c1e"


class ObsBackend(BackendProtocol):
    """把观测记录挂成只读虚拟文件（04 §3.3）。spike 里用内存 dict 代替 Postgres/文件存储。"""

    FILES = {
        "obs_1.json": '{\n  "obs_id": "obs_1",\n  "tool": "es.cluster_health",\n'
        f'  "marker": "{OBS_MARKER}",\n  "status": "yellow",\n  "unassigned_shards": 3\n}}\n',
    }

    def _key(self, path: str) -> str:
        # CompositeBackend 会去掉路由前缀再传进来，这里两种写法都接受
        return path.removeprefix("/obs/").lstrip("/")

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        content = self.FILES.get(self._key(file_path))
        if content is None:
            return ReadResult(error=f"not found: {file_path}")
        offset, limit = normalize_read_bounds(offset, limit)
        lines = content.splitlines(keepends=True)
        window = lines[offset : offset + limit]
        return ReadResult(
            file_data={"content": "".join(window), "encoding": "utf-8"},
            total_lines=len(lines),
            start_line=offset + 1,
            end_line=offset + len(window),
        )

    def ls(self, path: str) -> LsResult:
        return LsResult(entries=[{"path": f"/obs/{k}", "is_dir": False} for k in self.FILES])

    def grep(self, pattern, path=None, glob=None, *, max_count=None) -> GrepResult:
        return GrepResult(matches=[])

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return GlobResult(matches=[{"path": f"/obs/{k}"} for k in self.FILES])

    # 只读：写操作一律拒绝。观测记录由确定性代码写入，Agent 不能改。
    def write(self, *a: Any, **kw: Any) -> Any:
        raise PermissionError("/obs/ is read-only")

    def edit(self, *a: Any, **kw: Any) -> Any:
        raise PermissionError("/obs/ is read-only")


@tool
def es_cluster_health() -> str:
    """查询 ES 集群健康状态。返回摘要和观测 ID。"""
    return "obs_1：status=yellow，unassigned_shards=3。完整内容见 /obs/obs_1.json"


@tool
def node_disk_detail(node: str) -> str:
    """查询某个节点的磁盘明细（输出很长）。"""
    # 模拟一大段原始输出；里面的标记不应该出现在主 Agent 的 messages 里
    raw = "\n".join(f"{SUBAGENT_MARKER} path=/data/{i} used=93%" for i in range(50))
    return f"obs_42（节点 {node} 磁盘明细）：\n{raw}"


SYSTEM_PROMPT = f"""你是 ES 诊断 Agent，只有只读工具。严格按顺序完成：
1. 调用 es_cluster_health。
2. 用 read_file 读取 /obs/obs_1.json，并在最终回答里原样写出其中 marker 字段的值。
3. 用 task 工具把“检查 data-2 节点磁盘”委派给 node-inspector 子 Agent。
4. 给出最终结论：一句话根因，并引用所有用到的观测 ID（obs_1、obs_42）。"""

NODE_INSPECTOR = {
    "name": "node-inspector",
    "description": "节点级排查：检查指定节点的磁盘等资源。",
    "system_prompt": "调用 node_disk_detail 查看节点磁盘。只返回一句结论和观测 ID（例如 obs_42），不要复述原始输出。",
    "tools": [node_disk_detail],
}


class InvState(TypedDict, total=False):
    inv_id: str
    diagnosis: str
    agent_messages: list[dict]
    decision: dict


def diagnose(state: InvState) -> InvState:
    # 计数：恢复之后如果这个节点被重跑，这里会再插一行（01 §3.2 提到的坑）
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute("insert into diagnose_runs(inv) values (%s)", (state["inv_id"],))
    agent = create_deep_agent(
        model=ChatGoogleGenerativeAI(model=MODEL, temperature=0),
        tools=[es_cluster_health],
        system_prompt=SYSTEM_PROMPT,
        subagents=[NODE_INSPECTOR],
        backend=CompositeBackend(default=StateBackend(), routes={"/obs/": ObsBackend()}),
    )
    out = agent.invoke({"messages": [{"role": "user", "content": "集群告警：es-target 状态 yellow，请诊断。"}]})
    msgs = out["messages"]
    return {"diagnosis": msgs[-1].text, "agent_messages": messages_to_dict(msgs)}


def approve(state: InvState) -> InvState:
    # 审批节点里只有 interrupt()：恢复时节点从第一行重跑，前面不能有副作用（01 §3.2）
    decision = interrupt({"inv_id": state["inv_id"], "diagnosis": state["diagnosis"]})
    return {"decision": decision}


def build_graph(checkpointer: PostgresSaver):
    g = StateGraph(InvState)
    g.add_node("diagnose", diagnose)
    g.add_node("approve", approve)
    g.add_edge(START, "diagnose")
    g.add_edge("diagnose", "approve")
    g.add_edge("approve", END)
    return g.compile(checkpointer=checkpointer)


_state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute("create table if not exists diagnose_runs(inv text, at timestamptz default now())")
    with PostgresSaver.from_conn_string(PG_URL) as saver:
        saver.setup()
        _state["graph"] = build_graph(saver)
        yield


app = FastAPI(lifespan=lifespan)


class StartReq(BaseModel):
    id: str


def _summarize(result: dict) -> dict:
    if "__interrupt__" in result:
        return {"status": "interrupted", "payload": result["__interrupt__"][0].value}
    return {"status": "done", "decision": result.get("decision")}


@app.post("/inv")
def start(req: StartReq):
    cfg = {"configurable": {"thread_id": req.id}}  # thread_id = 调查 ID（01 §3.2）
    return _summarize(_state["graph"].invoke({"inv_id": req.id}, cfg))


@app.post("/inv/{inv_id}/resume")
def resume(inv_id: str, body: dict):
    cfg = {"configurable": {"thread_id": inv_id}}
    return _summarize(_state["graph"].invoke(Command(resume=body), cfg))
