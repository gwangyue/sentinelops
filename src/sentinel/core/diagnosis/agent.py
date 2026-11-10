"""诊断 Deep Agent 的组装和运行（04 篇；ADR-001 外层 LangGraph、内层 Deep Agents）。

负责：把专家包的只读工具包装成 LangChain 工具（经过 ToolBackend，每次调用一条观测记录）；
      渲染系统提示词（含根因标签表）；调用 Agent，拿到结构化输出；
      由代码补齐 Diagnosis 里不该由模型生成的字段（investigation_id、steps、tokens），
      并把根因表之外的标签改为 None。
不负责：是否升级、审计（调查图的 diagnose 节点）；子 Agent 和 L2 压缩（M2 D5，TODO(edge)）。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from sentinel.contracts import ContextBundle, Diagnosis, Hypothesis, RootCauseDecl
from sentinel.core.context.build import wrap
from sentinel.core.diagnosis.obs_backend import ObsBackend
from sentinel.core.observations.store import ObservationStore
from sentinel.core.packs_registry import LoadedPack
from sentinel.core.toolbackend import ToolBackend

PROMPT = Path(__file__).with_name("prompts") / "diagnose.md"
UNKNOWN_LABEL = "es.unknown"


class _NoArgs(BaseModel):
    """没有参数的工具用这个空 schema：不能让 StructuredTool 从函数签名推断，否则内部参数 _tid 会暴露给模型。"""


class DiagnosisOutput(BaseModel):
    """模型的结构化输出：只包含需要推理的部分，用的是契约里的 Hypothesis（含 Claim）。"""

    hypotheses: list[Hypothesis] = Field(max_length=3, description="按置信度从高到低，最多 3 个")
    escalate: bool = Field(description="证据不足以支持任何假设时为 true")
    escalate_reason: str | None = None


@dataclass(frozen=True)
class DiagnoseResult:
    diagnosis: Diagnosis
    model_calls: int


def render_prompt(root_causes: Sequence[RootCauseDecl]) -> str:
    table = "\n".join(f"- `{r.label}`：{r.description}" for r in root_causes)
    unknown = next((r.label for r in root_causes if r.label.endswith(".unknown")), UNKNOWN_LABEL)
    return PROMPT.read_text(encoding="utf-8").replace("{root_causes}", table).replace("{unknown_label}", unknown)


def tool_name(tool_id: str) -> str:
    # 函数名里不用 "."：不同模型服务商对函数名字符集的要求不一样，下划线最稳妥
    return tool_id.replace(".", "_")


def lc_tools(pack: LoadedPack, backend: ToolBackend) -> list[BaseTool]:
    out: list[BaseTool] = []
    for tid, t in pack.tools.items():

        def run(_tid: str = tid, **kwargs: Any) -> str:
            r = backend.call(_tid, {k: v for k, v in kwargs.items() if v is not None})
            return wrap(r.observation_id, f"[{r.status}] {r.digest}")

        out.append(
            StructuredTool.from_function(
                func=run,
                name=tool_name(tid),
                description=t.impl.description,
                args_schema=t.impl.args_schema or _NoArgs,
                infer_schema=False,
            )
        )
    return out


def run_diagnosis(
    *,
    model: BaseChatModel,
    pack: LoadedPack,
    backend: ToolBackend,
    obs_store: ObservationStore,
    bundle: ContextBundle,
    run_config: dict[str, Any],
) -> DiagnoseResult:
    agent = create_deep_agent(
        model=model,
        tools=lc_tools(pack, backend),
        system_prompt=render_prompt(pack.manifest.root_causes),
        backend=CompositeBackend(
            default=StateBackend(), routes={"/obs/": ObsBackend(obs_store, bundle.investigation_id)}
        ),
        response_format=ToolStrategy(DiagnosisOutput),
    )
    question = bundle.rendered + "\n\n请诊断上面的问题。需要更多证据时调用工具。"
    config: RunnableConfig = {**run_config, "recursion_limit": 60}  # type: ignore[typeddict-item]
    out = agent.invoke({"messages": [HumanMessage(question)]}, config=config)
    result: DiagnosisOutput = out["structured_response"]

    ai = [m for m in out["messages"] if isinstance(m, AIMessage)]
    usage = [m.usage_metadata for m in ai if m.usage_metadata]
    labels = {r.label for r in pack.manifest.root_causes}
    hypotheses = [
        h.model_copy(update={"root_cause_label": h.root_cause_label if h.root_cause_label in labels else None})
        for h in sorted(result.hypotheses, key=lambda h: -h.confidence)
    ]
    diagnosis = Diagnosis(
        investigation_id=bundle.investigation_id,
        hypotheses=hypotheses,
        escalate=result.escalate,
        escalate_reason=result.escalate_reason,
        # 工具调用次数：不算最后那次结构化输出的"工具"调用
        steps=sum(1 for m in ai for tc in m.tool_calls if tc["name"] != DiagnosisOutput.__name__),
        tokens_in=sum(u["input_tokens"] for u in usage),
        tokens_out=sum(u["output_tokens"] for u in usage),
    )
    return DiagnoseResult(diagnosis, model_calls=len(ai))
