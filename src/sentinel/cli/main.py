"""`sentinel` 命令行（Typer）。

负责：命令的入口和参数解析。各子命令（patrol、bench、eval、version、approvals……）按 08 篇的排期逐天加入。
不负责：业务逻辑，一律转给 core / eval / governance。
"""

import json
from pathlib import Path
from typing import Annotated

import typer
from ulid import ULID

app = typer.Typer(no_args_is_help=True, help="SentinelOps 运维 Agent")
pack_app = typer.Typer(no_args_is_help=True, help="专家包")
tools_app = typer.Typer(no_args_is_help=True, help="只读工具（开发调试用）")
app.add_typer(pack_app, name="pack")
app.add_typer(tools_app, name="tools")

# 观测记录的原始输出放在仓库根目录的 var/ 下（已加入 .gitignore）
OBS_ROOT = Path("var/observations")


@app.callback()
def main() -> None:
    """有了 callback，Typer 才会把各命令当作子命令（而不是唯一的默认命令）。"""


@app.command()
def info() -> None:
    """打印模型配置里有哪些用途和对应型号（型号只来自配置文件，ADR-008）。"""
    from sentinel.core.llm.config import load_models_config

    for purpose, m in sorted(load_models_config().purposes.items()):
        typer.echo(f"{purpose}: {m.provider}/{m.model}")


@pack_app.command("check")
def pack_check(name: str) -> None:
    """校验 pack.yaml，并确认所有 entry 都能加载（03 §3.1）。"""
    from sentinel.core.packs_registry import load_pack

    p = load_pack(name)
    m = p.manifest
    typer.echo(f"{m.pack} {m.version} supports {m.supports}")
    typer.echo(
        f"  工具 {len(p.tools)}：" + ", ".join(f"{t}({lt.decl.output_budget_tokens})" for t, lt in p.tools.items())
    )
    typer.echo(f"  检测器 {len(p.detectors)}：" + ", ".join(p.detectors))
    typer.echo(f"  fact builder {len(p.fact_builders)}")


def _backend(pack_name: str, mode: str, fixture: Path | None):  # type: ignore[no-untyped-def]
    from sentinel.core.observations.store import ObservationStore
    from sentinel.core.packs_registry import load_pack
    from sentinel.core.toolbackend import Mode, open_backend

    pack = load_pack(pack_name)
    inv = f"inv_{ULID()}"
    return pack, inv, open_backend(pack, inv, Mode(mode), ObservationStore(OBS_ROOT), fixture)


@tools_app.command("call")
def tools_call(
    tool: str,
    args: Annotated[str, typer.Option(help="JSON 参数")] = "{}",
    backend: Annotated[str, typer.Option(help="live | record | replay")] = "live",
    fixture: Annotated[Path | None, typer.Option(help="record / replay 的录制文件")] = None,
) -> None:
    """调用一个只读工具，打印观测 ID 和摘要。"""
    from sentinel.sdk import estimate_tokens

    pack_name = tool.split(".", 1)[0]
    _, inv, tb = _backend(pack_name, backend, fixture)
    try:
        r = tb.call(tool, json.loads(args))
    finally:
        tb.close()
    typer.echo(f"{inv} {r.observation_id} status={r.status} digest_tokens≈{estimate_tokens(r.digest)}")
    typer.echo(r.digest)


@app.command()
def detect(
    target: str,
    pack: Annotated[str, typer.Option()] = "es",
    backend: Annotated[str, typer.Option(help="live | record | replay")] = "live",
    fixture: Annotated[Path | None, typer.Option(help="record / replay 的录制文件")] = None,
) -> None:
    """运行专家包的全部检测器和 fact builder，打印 Finding 和事实卡片（不调用 LLM）。"""
    from sentinel.core.detect import load_env, run_detect

    loaded, inv, tb = _backend(pack, backend, fixture)
    try:
        res = run_detect(loaded, target, tb, load_env(target))
    finally:
        tb.close()
    typer.echo(f"{inv} backend={backend} findings={len(res.findings)}")
    for f in res.findings:
        typer.echo(f"  [{f.severity}] {f.detector} {f.object_ref or ''} — {f.summary}  {','.join(f.observation_ids)}")
    for fs in res.fact_sheets:
        typer.echo(f"  事实卡片：{len(fs.facts)} 条，省略正常项 {fs.omitted_normal_count} 条")
        for fact in fs.facts:
            typer.echo(f"    {fact.status:<8} {fact.subject:<22} {fact.key:<26} {fact.value}  {fact.observation_id}")
