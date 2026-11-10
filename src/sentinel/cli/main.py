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
version_app = typer.Typer(no_args_is_help=True, help="Agent 版本（03 §5.2）")
inv_app = typer.Typer(no_args_is_help=True, help="调查")
app.add_typer(version_app, name="version")
app.add_typer(inv_app, name="investigations")

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


@version_app.command("freeze")
def version_freeze(note: Annotated[str, typer.Option(help="这一版改了什么（必填）")]) -> None:
    """冻结当前的提示词、模型、工具、策略和代码为一个版本；内容相同就复用（03 §5.2）。"""
    from sentinel.core.graph.run import load_packs
    from sentinel.core.store import open_store
    from sentinel.core.versioning.components import behavior_dirty, collect_components
    from sentinel.core.versioning.freeze import freeze

    v, created = freeze(open_store(), collect_components(list(load_packs().values())), note, dirty=behavior_dirty())
    typer.echo(f"{'新建' if created else '复用'} v{v.seq} {v.hash[:19]}…  parent={(v.parent or '-')[:19]}  {v.note}")


@version_app.command("list")
def version_list() -> None:
    from sentinel.core.store import open_store

    for v in open_store().list_versions():
        typer.echo(f"v{v.seq:<3} {v.hash[:19]}…  {v.created_at:%Y-%m-%d %H:%M}  {v.note}")


@app.command()
def patrol(
    target: str,
    pack: Annotated[str, typer.Option()] = "es",
    backend: Annotated[str, typer.Option(help="live | record | replay")] = "live",
    fixture: Annotated[Path | None, typer.Option(help="record / replay 的录制文件")] = None,
) -> None:
    """巡检一次：没有问题就结束（不调用 LLM）；有问题就只读诊断并出报告（02 §2）。"""
    import time

    from sentinel.core.graph.run import patrol as run_patrol
    from sentinel.core.toolbackend import Mode

    t0 = time.monotonic()
    s = run_patrol(target, pack, Mode(backend), fixture)
    took = time.monotonic() - t0
    if s["status"] == "healthy":
        typer.echo(f"健康，0 个问题，用时 {took:.1f} 秒  {s['inv_id']}")
    else:
        typer.echo(f"{s['inv_id']}  状态 {s['status']}  问题 {len(s['findings'])} 个  用时 {took:.1f} 秒")
        typer.echo(f"查看：sentinel investigations show {s['inv_id']}")


@inv_app.command("list")
def inv_list() -> None:
    from sentinel.core.store import open_store

    for r in open_store().list_investigations():
        typer.echo(f"{r.id}  {r.created_at:%m-%d %H:%M}  {r.target:<10} {r.status:<10} 问题 {len(r.findings)}")


@inv_app.command("show")
def inv_show(inv_id: str) -> None:
    """02 §3.3 的格式：问题事件、假设、置信度、证据（观测 ID + 引文）。"""
    from sentinel.contracts import Diagnosis
    from sentinel.core.diagnosis.evidence import check_evidence, support_rate
    from sentinel.core.store import open_store

    store = open_store()
    r = store.get_investigation(inv_id)
    if r is None:
        raise typer.BadParameter(f"找不到调查 {inv_id}")
    v = store.get_version(r.agent_version)
    typer.echo(f"调查 {r.id}   集群 {r.target}   版本 v{v.seq if v else '?'}   状态 {r.status}")
    for f in r.findings:
        typer.echo(f"问题事件  {f['detector']}  {f['severity']}  {f['summary']}")
    if r.diagnosis is None:
        return
    d = Diagnosis.model_validate(r.diagnosis)
    checks = {(c.hypothesis_id, c.claim_id): c for c in check_evidence(d, store.observations(inv_id))}
    typer.echo("诊断")
    for h in d.hypotheses:
        typer.echo(f"  {h.id}  {h.confidence:.2f}  [{h.root_cause_label or '未归类'}] {h.root_cause}")
        for c in h.claims:
            mark = "✓" if checks[(h.id, c.id)].supported else f"✗ {checks[(h.id, c.id)].reason}"
            typer.echo(f"        {c.id} {c.text}")
            typer.echo(f"           证据 {','.join(c.evidence)} {mark}" + (f'  "{c.quote}"' if c.quote else ""))
    rate = support_rate(list(checks.values()))
    typer.echo(f"证据支持率 {rate:.0%}   工具调用 {d.steps} 次   token 输入 {d.tokens_in} / 输出 {d.tokens_out}")
    if r.escalate_reason:
        typer.echo(f"已升级给人：{r.escalate_reason}")
