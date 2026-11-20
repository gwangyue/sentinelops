"""`sentinel` 命令行（Typer）。

负责：命令的入口和参数解析。各子命令（patrol、bench、eval、version、approvals……）按 08 篇的排期逐天加入。
不负责：业务逻辑，一律转给 core / eval / governance。
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import typer
from ulid import ULID

if TYPE_CHECKING:
    from sentinel.governance.remediation_store import PgRemediationStore

app = typer.Typer(no_args_is_help=True, help="SentinelOps 运维 Agent")
pack_app = typer.Typer(no_args_is_help=True, help="专家包")
tools_app = typer.Typer(no_args_is_help=True, help="只读工具（开发调试用）")
app.add_typer(pack_app, name="pack")
app.add_typer(tools_app, name="tools")
version_app = typer.Typer(no_args_is_help=True, help="Agent 版本（03 §5.2）")
inv_app = typer.Typer(no_args_is_help=True, help="调查")
app.add_typer(version_app, name="version")
app.add_typer(inv_app, name="investigations")
bench_app = typer.Typer(no_args_is_help=True, help="故障台（02 §6）")
dataset_app = typer.Typer(no_args_is_help=True, help="数据集（06 §5）")
eval_app = typer.Typer(no_args_is_help=True, help="评测（06 篇）")
knowledge_app = typer.Typer(no_args_is_help=True, help="L4 知识层（04 §3.5）")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(bench_app, name="bench")
app.add_typer(dataset_app, name="dataset")
app.add_typer(eval_app, name="eval")
approvals_app = typer.Typer(no_args_is_help=True, help="审批（03 §4.4；02 §3.4：审批不经过 LLM）")
app.add_typer(approvals_app, name="approvals")
report_app = typer.Typer(no_args_is_help=True, help="治理周报（07 §8）")
app.add_typer(report_app, name="report")
mcp_app = typer.Typer(no_args_is_help=True, help="外部 Agent 的 MCP 只读服务（02 §8）")
chatops_app = typer.Typer(no_args_is_help=True, help="飞书 ChatOps（09 篇）")
app.add_typer(chatops_app, name="chatops")
app.add_typer(mcp_app, name="mcp")
audit_app = typer.Typer(no_args_is_help=True, help="审计（07 §5）")
app.add_typer(audit_app, name="audit")

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
    from sentinel.core.versioning.freeze import FreezeError, freeze

    try:
        v, created = freeze(open_store(), collect_components(list(load_packs().values())), note, dirty=behavior_dirty())
    except FreezeError as e:
        typer.echo(f"拒绝冻结：{e}", err=True)
        raise typer.Exit(1) from e
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
    mode: Annotated[
        str, typer.Option(help="report_only（巡检，默认）| remediate（诊断后提议修复，等审批）")
    ] = "report_only",
) -> None:
    """巡检一次：没有问题就结束（不调用 LLM）；有问题就诊断。remediate 模式下提议修复并停在审批（02 §3）。"""
    import time

    from sentinel.core.graph.run import patrol as run_patrol
    from sentinel.core.toolbackend import Mode
    from sentinel.core.versioning.freeze import FreezeError

    t0 = time.monotonic()
    try:
        s = run_patrol(target, pack, Mode(backend), fixture, inv_mode=mode)
    except FreezeError as e:
        typer.echo(f"拒绝开始调查：{e}", err=True)
        raise typer.Exit(1) from e
    took = time.monotonic() - t0
    # 停在审批（interrupt）时图还没走到 report，状态里没有 status
    status = "awaiting_approval" if "__interrupt__" in s else s.get("status", "?")
    if status == "healthy":
        typer.echo(f"健康，0 个问题，用时 {took:.1f} 秒  {s['inv_id']}")
    else:
        typer.echo(f"{s['inv_id']}  状态 {status}  问题 {len(s.get('findings', []))} 个  用时 {took:.1f} 秒")
        typer.echo(f"查看：sentinel investigations show {s['inv_id']}")
        if s.get("approval_id") and "__interrupt__" in s:
            pl = s.get("plan") or {}
            typer.echo(f"修复方案 {pl['action_id']} {pl['params']}  预演 {pl['dry_run_result']}")
            typer.echo(f"等待审批：sentinel approvals approve {s['approval_id']} --as <你的名字>")
        elif s.get("remediation"):
            typer.echo(f"修复：{s['remediation']}")


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


@bench_app.command("inject")
def bench_inject(scenario: str, pack: Annotated[str, typer.Option()] = "es") -> None:
    """注入故障并等症状出现（golden-demo 第 2 步）。故障保留，演示完用 sentinel bench revert 撤销。"""
    from sentinel.eval.bench.bench import inject

    typer.echo(f"故障已注入，等待症状出现：{scenario}")
    took = inject(pack, scenario)
    typer.echo(f"症状在 {took} 秒后出现。故障仍在，接下来可以 sentinel patrol；结束后 sentinel bench revert {scenario}")


@bench_app.command("revert")
def bench_revert(scenario: str, pack: Annotated[str, typer.Option()] = "es") -> None:
    """撤销场景的故障，等集群恢复 green。"""
    from sentinel.eval.bench.bench import revert_fault

    revert_fault(pack, scenario)
    typer.echo(f"已撤销 {scenario}，集群恢复 green")


@bench_app.command("record")
def bench_record(
    scenario: str, n: Annotated[int, typer.Option(help="录几条")] = 2, pack: Annotated[str, typer.Option()] = "es"
) -> None:
    """注入 → 等症状 → 录制全部只读工具输出 → 撤销 → 写入 datasets/<pack>.jsonl。"""
    from sentinel.eval.bench.bench import record

    ids = record(pack, scenario, n, log=typer.echo)
    typer.echo(f"完成：{len(ids)} 条录制")


@dataset_app.command("sync")
def dataset_sync(
    pack: Annotated[str, typer.Option()] = "es",
    note: Annotated[str, typer.Option(help="这次数据集变化做了什么（必填）")] = "",
) -> None:
    """把 datasets/<pack>.jsonl 同步到 LangSmith 并打版本标签（06 §5.2）。"""
    from langsmith import Client

    from sentinel.eval.dataset import sync

    tag, counts = sync(pack, note, Client())
    typer.echo(f"已同步 sentinel-{pack}：标签 {tag}  dev {counts['dev']} / heldout {counts['heldout']}")


@dataset_app.command("streams")
def dataset_streams(
    pack: Annotated[str, typer.Option()] = "es",
    seed: Annotated[int, typer.Option(help="固定种子：顺序在看结果之前定下来（05 §4.2）")] = 0,
) -> None:
    """生成事故流文件 datasets/streams/<pack>.yaml（只用 dev 用例，06 §5.2）。生成后提交，再跑评测。"""
    import yaml

    from sentinel.eval.dataset import load_examples
    from sentinel.eval.stream import make_streams, streams_file

    streams = make_streams(load_examples(pack), seed)
    path = streams_file(pack)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"pack": pack, "seed": seed, "streams": streams}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    for name, ids in streams.items():
        typer.echo(f"{name}（{len(ids)} 个用例）：{' → '.join(ids)}")


@eval_app.command("stream")
def eval_stream(
    pack: Annotated[str, typer.Option()] = "es",
    dataset_tag: Annotated[str | None, typer.Option(help="数据集版本标签（必填）")] = None,
    reps: Annotated[int, typer.Option(help="每条流重复几次")] = 3,
    version: Annotated[str | None, typer.Option(help="只能等于当前版本")] = None,
    workers: Annotated[int | None, typer.Option(help="并行的（重复, 流）任务数；默认全部并行")] = None,
) -> None:
    """按事故流顺序、带状态地评测（05 §4.2；M-E1）。不走 evaluate()，trace 照样上传（06 §9）。"""
    from sentinel.core.versioning.freeze import FreezeError
    from sentinel.eval.guards import EvalGuardError
    from sentinel.eval.runner import EvalError
    from sentinel.eval.stream import run_streams

    try:
        path, side = run_streams(pack, dataset_tag, reps, version, workers=workers)
    except (EvalGuardError, EvalError, FreezeError) as e:
        typer.echo(f"拒绝评测：{e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"结果 {path}\n记忆侧指标 {side}")


@eval_app.command("run")
def eval_run(
    pack: Annotated[str, typer.Option()] = "es",
    split: Annotated[str, typer.Option()] = "dev",
    dataset_tag: Annotated[str | None, typer.Option(help="数据集版本标签（必填，不能是 latest）")] = None,
    reps: Annotated[int, typer.Option(help="每个用例重复几次")] = 3,
    version: Annotated[str | None, typer.Option(help="只能等于当前版本；不填就是当前版本")] = None,
    mode: Annotated[
        str, typer.Option(help="report_only | remediate（M3：修复段用回放执行器，审批由 sim:eval 模拟）")
    ] = "report_only",
) -> None:
    """在回放模式下评测当前版本（06 §9）。"""
    from sentinel.core.versioning.freeze import FreezeError
    from sentinel.eval.guards import EvalGuardError
    from sentinel.eval.runner import EvalError, run
    from sentinel.eval.stats.bootstrap import stability

    try:
        s = run(pack, split, dataset_tag, reps, version, mode)
    except (EvalGuardError, EvalError, FreezeError) as e:
        typer.echo(f"拒绝评测：{e}", err=True)
        raise typer.Exit(1) from e
    ok = [x for x in s.scores if x.replay_misses == 0]
    st = stability(s.scores)
    typer.echo(f"实验 {s.experiment}：{len(s.scores)} 次运行，结果 {s.result_file}")
    if ok:
        typer.echo(
            f"  top1 {sum(x.top1_correct for x in ok) / len(ok):.2f}（剔除录制未覆盖的 "
            f"{len(s.scores) - len(ok)} 次）  证据支持率 {sum(x.evidence_support_rate for x in ok) / len(ok):.2f}"
            f"  费用 ${sum(x.cost_usd for x in s.scores):.4f}"
        )
    typer.echo(f"  不稳定用例率 {st.unstable_rate:.2f}  用例内方差均值 {st.mean_within_var:.3f}")


@eval_app.command("baseline")
def eval_baseline(
    pack: Annotated[str, typer.Option()] = "es",
    split: Annotated[str, typer.Option()] = "dev",
    dataset_tag: Annotated[str | None, typer.Option(help="数据集版本标签（必填）")] = None,
) -> None:
    """多数类和纯规则两个笨基线（06 §4.5），不调用 LLM。"""
    from sentinel.eval import results
    from sentinel.eval.baselines import run
    from sentinel.eval.guards import EvalGuardError

    try:
        paths = run(pack, split, dataset_tag)
    except EvalGuardError as e:
        typer.echo(f"拒绝评测：{e}", err=True)
        raise typer.Exit(1) from e
    for name, path in paths.items():
        sc = results.load(path)
        typer.echo(f"baseline:{name}  top1 {sum(x.top1_correct for x in sc) / len(sc):.2f}  ({len(sc)} 个用例)  {path}")


@eval_app.command("compare")
def eval_compare(
    a: str,
    b: str,
    split: Annotated[str, typer.Option()] = "dev",
    dataset_tag: Annotated[
        str | None, typer.Option(help="数据集版本标签（必填）：两边的结果必须来自同一个标签")
    ] = None,
    by: Annotated[
        str | None, typer.Option(help="分组：difficulty（06 §6.2）；occurrence（事故流里第几次遇到同一根因，05 §4.2）")
    ] = None,
    stream: Annotated[bool, typer.Option(help="比较事故流的结果（sentinel eval stream）")] = False,
) -> None:
    """配对比较 B − A 的 top1：点估计、按场景聚类的 bootstrap 95% 区间、判断（06 §4.2）。"""
    from sentinel.eval import results
    from sentinel.eval.dataset import load_examples
    from sentinel.eval.guards import require_dataset_tag
    from sentinel.eval.stats.bootstrap import compare

    tag = require_dataset_tag(dataset_tag)

    def load(name: str) -> list:  # type: ignore[type-arg]
        name = name.replace("baseline:", "baseline-") + ("__stream" if stream else "")
        return results.load(results.result_file(name, tag, split))

    ra, rb = load(a), load(b)
    groups: dict[str, set[str] | None] = {"全部": None}
    if by == "difficulty":
        # RunScore 里没有难度，按用例 ID 从 git 里的数据集查（数据集以 git 为准，06 §5.2）
        diff = {e.id: e.metadata.difficulty for e in load_examples("es")}
        for d in ("single", "distractor", "compound"):
            groups[d] = {eid for eid, v in diff.items() if v == d}
    elif by == "occurrence":
        if not stream:
            raise typer.BadParameter("--by occurrence 只用于 --stream")
        # 根因标签按场景取（场景的真实答案不随重录变化）：事故流引用的录制可能已经被重录替换掉了
        # （M3 D11 重录之后，ds-20261110-6 的 r5/r6 不在 es.jsonl 里了）
        from sentinel.eval.dataset import load_scenarios
        from sentinel.eval.stats.bootstrap import scenario_of
        from sentinel.eval.stream import load_streams, occurrences

        scn = load_scenarios("es")
        streams_ = load_streams("es")
        labels = {e: scn[scenario_of(e)].ground_truth.root_cause_label for ids in streams_.values() for e in ids}
        occ = occurrences(streams_, labels)
        groups["第 1 次遇到"] = {eid for eid, n in occ.items() if n == 1}
        groups["第 2 次及以后"] = {eid for eid, n in occ.items() if n >= 2}
    elif by is not None:
        raise typer.BadParameter("--by 只支持 difficulty、occurrence")
    typer.echo(f"{b} − {a}（{tag} / {split}，主指标 top1，区间按场景重采样，06 §4.2）")
    for name, ids in groups.items():
        sa = [x for x in ra if ids is None or x.example_id in ids]
        sb = [x for x in rb if ids is None or x.example_id in ids]
        if not sa or not sb:
            continue
        c = compare(sa, sb)
        typer.echo(
            f"  [{name}] {a} {c.a_score:.2f}  {b} {c.b_score:.2f}  差值 {c.point:+.2f}  "
            f"95% 区间 [{c.low:+.2f}, {c.high:+.2f}]  {c.verdict}  （{c.n_scenarios} 个场景，{c.n_examples} 个用例）"
        )
        if c.excluded_replay_miss:
            typer.echo(f"      录制未覆盖、已剔除：{', '.join(c.excluded_replay_miss)}")
        if by == "occurrence" and ids is not None:
            # 记忆的增益要看"第 2 次及以后"这一组的步数和费用（05 §5 M-E1 的预期），所以分组也给区间
            for metric, fmt in (("steps", ".2f"), ("cost_usd", ".4f")):
                m = compare(sa, sb, metric=metric)
                typer.echo(
                    f"      {metric:<9} {m.a_score:{fmt}} → {m.b_score:{fmt}}  "
                    f"95% 区间 [{m.low:+{fmt}}, {m.high:+{fmt}}]  {m.verdict}"
                )
    # 次要指标和成本（06 §3.3、§7.2）：同样按场景聚类给区间；只有成本参与上线取舍（§7.2 第 3、4 条）
    typer.echo("  次要指标（全部用例）：")
    for metric, fmt in (
        ("cost_usd", ".4f"),
        ("steps", ".2f"),
        ("tokens", ".0f"),
        ("evidence_support_rate", ".3f"),
        ("top3_correct", ".2f"),
    ):
        m = compare(ra, rb, metric=metric)
        verdict = {"提升": "显著上升", "退步": "显著下降"}.get(m.verdict, "无显著变化")
        typer.echo(
            f"    {metric:<22} {a} {m.a_score:{fmt}}  {b} {m.b_score:{fmt}}  差值 {m.point:+{fmt}}  "
            f"95% 区间 [{m.low:+{fmt}}, {m.high:+{fmt}}]  {verdict}"
        )
    if stream:
        from sentinel.eval.stream import memory_summary

        for name in (a, b):
            side = results.result_file(f"{name}__stream", tag, split).with_suffix(".memory.jsonl")
            typer.echo(f"  记忆侧（{name}）：{memory_summary(side, rb if name == b else ra)}")


@eval_app.command("clusters")
def eval_clusters(
    version: str,
    split: Annotated[str, typer.Option()] = "dev",
    dataset_tag: Annotated[str | None, typer.Option(help="数据集版本标签（必填）")] = None,
    stream: Annotated[bool, typer.Option(help="事故流的结果")] = False,
    judge: Annotated[bool, typer.Option(help="对上下文都在但答错的运行调用评审模型（06 §6.1 第二步）")] = True,
) -> None:
    """失败归类（06 §6.2）：规则标签 + 评审模型标签，按标签组合分组，给出样例和建议的下一步。"""
    from sentinel.core.api import chat_model
    from sentinel.eval import results
    from sentinel.eval.dataset import load_examples
    from sentinel.eval.failures import FIX_HINTS, apply_judge, clusters, next_step
    from sentinel.eval.guards import require_dataset_tag

    tag = require_dataset_tag(dataset_tag)
    path = results.result_file(version + ("__stream" if stream else ""), tag, split)
    truths = {e.id: e.outputs for e in load_examples("es")}

    def ask(prompt: str) -> str:
        return chat_model("evaluator").invoke(prompt).text

    scores, n_judged = apply_judge(path, truths, ask if judge else None)
    cs = clusters(scores)
    failed = sum(1 for s in scores if s.failure_tags)
    typer.echo(
        f"{version}（{tag} / {split}）：{len(scores)} 次运行，有标签 {failed} 次"
        f"（用例级 {len({s.example_id for s in scores if s.failure_tags})} 个；本次调用评审 {n_judged} 次）"
    )
    for c in cs:
        typer.echo(f"  {c.runs:>3}  {' + '.join(c.tags):<45} {c.examples} 个用例  样例：{c.sample}")
    nxt = next_step(cs)
    typer.echo(f"建议下一步：{nxt} → {FIX_HINTS.get(nxt.removeprefix('judge:'), '看评审理由')}" if nxt else "没有失败")


def _rem_store() -> "PgRemediationStore":
    from sentinel.core.graph.run import PG_URL
    from sentinel.governance.remediation_store import PgRemediationStore

    return PgRemediationStore(PG_URL)


ANCHORS = Path("var/audit/anchors.log")  # 只追加的本地锚定文件（07 §5：作品项目只做本地，云上可以放 S3 对象锁）


def _since(spec: str):  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    unit = {"d": "days", "h": "hours", "m": "minutes"}[spec[-1]]
    return datetime.now(UTC) - timedelta(**{unit: int(spec[:-1])})


def _read_anchors() -> list[tuple[str, str]]:
    if not ANCHORS.exists():
        return []
    return [tuple(x.split()[1:3]) for x in ANCHORS.read_text(encoding="utf-8").splitlines() if x]  # type: ignore[misc]


@audit_app.command("verify")
def audit_verify(
    since: Annotated[str, typer.Option(help="校验窗口，例如 1d、12h；窗口之前的那一条作为起点")] = "7d",
) -> None:
    """重新计算审计哈希链，报告第一个断点；同时核对外部锚定（07 §5）。有问题时退出码为 1。"""
    from sentinel.core.store import open_store
    from sentinel.governance.audit import verify_anchors, verify_chain

    events = open_store().audit_events()  # 按 seq 排好。TODO(edge)：规模变大后按窗口查询
    start = _since(since)
    i = next((k for k, e in enumerate(events) if e.ts >= start), len(events))
    window = events[i:]
    problems = verify_chain(window, events[i - 1] if i > 0 else None) + verify_anchors(events, _read_anchors())
    typer.echo(f"审计链：窗口内 {len(window)} 条（共 {len(events)} 条），锚定 {len(_read_anchors())} 个")
    if problems:
        typer.echo(f"第一个断点：{problems[0]}", err=True)
        for p in problems[1:5]:
            typer.echo(f"  {p}", err=True)
        raise typer.Exit(1)
    typer.echo("完整：每条事件的 prev_hash 都等于上一条重算的哈希；锚定一致")


@audit_app.command("anchor")
def audit_anchor() -> None:
    """把最后一条审计事件的哈希追加到本地锚定文件（07 §5，建议每天一次）。"""
    from datetime import UTC, datetime

    from sentinel.core.store import open_store
    from sentinel.governance.audit import event_hash

    events = open_store().audit_events()
    if not events:
        raise typer.BadParameter("还没有审计事件")
    last = events[-1]
    ANCHORS.parent.mkdir(parents=True, exist_ok=True)
    with ANCHORS.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now(UTC).isoformat()} {last.id} {event_hash(last)}\n")
    typer.echo(f"已锚定 {last.id}（{event_hash(last)[:16]}…）→ {ANCHORS}")


def _week(spec: str | None):  # type: ignore[no-untyped-def]
    """ISO 周，例如 2026-W46；不填就是本周。返回 (标签, 开始, 结束)，左闭右开，UTC。"""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    if spec is None:
        iso = now.isocalendar()
        spec = f"{iso.year}-W{iso.week:02d}"
    year, wk = spec.split("-W")
    start = datetime.fromisocalendar(int(year), int(wk), 1).replace(tzinfo=UTC)
    return spec, start, start + timedelta(days=7)


@report_app.command("weekly")
def report_weekly(
    week: Annotated[str | None, typer.Option(help="ISO 周，例如 2026-W46；不填就是本周")] = None,
    show_sql: Annotated[
        bool, typer.Option(help="打印每个数字对应的 SQL（golden-demo 第 10 步：能用 SQL 重算）")
    ] = False,
) -> None:
    """每周 30 分钟看 5 个数（07 §8）：调查与修复、安全、线上质量、评测状态、成本，加上需要你决定的事。"""
    from datetime import UTC, datetime

    import psycopg
    import yaml

    from sentinel.core.api import load_models_config
    from sentinel.core.graph.run import PG_URL
    from sentinel.core.packs_registry import discover_packs
    from sentinel.eval.report import HELDOUT_STALE_DAYS, pack_eval, shipped_version
    from sentinel.eval.scorers import cost_usd, load_pricing
    from sentinel.governance.report import QUERIES, audit_numbers, pg_runner

    if show_sql:
        for name, sql in QUERIES.items():
            typer.echo(f"-- {name}\n{sql};\n")
        return
    label, start, end = _week(week)
    with psycopg.connect(PG_URL) as conn:
        a = audit_numbers(pg_runner(conn), start, end)
    gov = yaml.safe_load(Path("config/governance.yaml").read_text(encoding="utf-8"))
    model = load_models_config().purposes["diagnosis"].model
    cost = cost_usd(model, a.tokens_in, a.tokens_out, load_pricing())
    budget = 7 * float(gov["budget"]["daily_usd"])
    shipped = shipped_version()
    evals = [pack_eval(p, shipped, datetime.now(UTC)) for p in discover_packs()]

    typer.echo(f"治理周报 {label}（{start:%m-%d} – {end:%m-%d}，UTC）  当前上线版本 v{shipped}")
    rate = f"{a.verify_passed}/{a.executed}" if a.executed else "—"
    typer.echo(
        f"1. 调查 {a.investigations} 次；执行修复 {a.executed} 次，修复成功率 {rate}；"
        f"验证失败 {a.verify_failed} 次，回滚 {a.rolled_back} 次"
    )
    rej = "；".join(f"{aid}（{r or '没有说明'}）" for aid, r in a.approvals_rejected) or "无"
    typer.echo(f"2. 不安全提议 {a.unsafe} 次；被拒绝的审批 {len(a.approvals_rejected)} 次：{rej}；冻结 {a.freezes} 次")
    asked = a.confirmed + a.corrected
    added = sum(e.online_corrections for e in evals)
    typer.echo(f"3. 修好之后人确认根因 {asked}/{a.resolved}（其中修正 {a.corrected} 次 → 已补进数据集 {added} 条）")
    for e in evals:
        if e.top1 is None:
            typer.echo(f"4. [{e.pack}] 还没有 dev 评测结果")
            continue
        which = f"v{e.version}" + (
            "" if e.version == shipped else f"（v{shipped} 没有单独的结果，用最近的 v{e.version}）"
        )
        held = "从未运行" if e.heldout_days_ago is None else f"{e.heldout_days_ago} 天前，已用 {e.heldout_runs} 次"
        typer.echo(
            f"4. [{e.pack}] {which} dev top1 {e.top1:.2f} [{e.low:.2f}, {e.high:.2f}]（{e.tag}）；保留测试集：{held}"
        )
    per = f"${cost / a.diagnoses:.3f}" if a.diagnoses else "—"
    typer.echo(
        f"5. 成本 ${cost:.2f}（{a.diagnoses} 次诊断，平均 {per}；不含 plan 节点的调用）；"
        f"预算使用率 {cost / budget:.0%}（每周 ${budget:.0f}）"
    )
    decide: list[str] = []
    for inv, plan, action, ts in a.temporary_fixes:
        decide.append(f"临时修复还没撤销：{action}（{plan}，{str(ts)[:16]}，调查 {inv}）")
    for mem, why in a.memory_invalidated:
        decide.append(f"记忆 {mem} 被自动失效（{why or '有用率低或伤害多'}）：看看是不是该删掉或修正")
    alert = float(gov["report"]["approval_reject_rate_alert"])
    for action, by in a.approvals_by_action.items():
        n = sum(by.values())
        if n >= 2 and by.get("rejected", 0) / n > alert:
            decide.append(f"{action} 的审批拒绝率 {by.get('rejected', 0)}/{n}：动作或它的前置检查可能有问题")
    for e in evals:
        if e.heldout_days_ago is not None and e.heldout_days_ago > HELDOUT_STALE_DAYS:
            decide.append(f"[{e.pack}] 保留测试集 {e.heldout_days_ago} 天没跑了")
        for g, n in e.small_groups.items():
            decide.append(f"[{e.pack}] dev 的 {g} 组只有 {n} 个场景，区间没有意义，建议补充")
    typer.echo("需要你决定的事：" + ("无" if not decide else ""))
    for item in decide:
        typer.echo(f"  - {item}")


@chatops_app.command("notify")
def chatops_notify(
    env_file: Annotated[
        Path | None, typer.Option(help="凭证文件（KEY=VALUE），以文件为准覆盖已有环境变量（S7）")
    ] = None,
    config: Annotated[Path | None, typer.Option(help="chatops 配置；默认读 SENTINEL_CHATOPS_CONFIG")] = None,
    once: Annotated[bool, typer.Option(help="只处理一批就退出（调试用）")] = False,
) -> None:
    """推送（09 §4）：按游标读审计表，把待审批、异常、升级、验证结果、冻结推到飞书群。"""
    import logging

    from sentinel.chatops.client import LarkFeishuClient
    from sentinel.chatops.config import load_chatops_config, load_env_file
    from sentinel.chatops.notifier import Notifier
    from sentinel.core.store import open_store

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if env_file:
        typer.echo(f"已从 {env_file} 读入：{', '.join(load_env_file(env_file))}")  # 只打印键名
    cfg = load_chatops_config(config)
    n = Notifier(open_store(), _rem_store(), LarkFeishuClient(cfg.domain), cfg)
    typer.echo(f"[{cfg.env_label}] 推送到 {cfg.domain}，每 {cfg.poll_interval_s} 秒读一次审计表")
    if once:
        # 第一次运行只会初始化游标；再跑一次才会真正推送
        typer.echo(f"本轮发出 {n.tick()} 张卡片；游标 {n.store.get_notify_cursor(cfg.env_label)}")
        return
    n.run_forever()


@chatops_app.command("serve")
def chatops_serve(
    env_file: Annotated[
        Path | None, typer.Option(help="凭证文件（KEY=VALUE），以文件为准覆盖已有环境变量（S7）")
    ] = None,
    config: Annotated[Path | None, typer.Option(help="chatops 配置；默认读 SENTINEL_CHATOPS_CONFIG")] = None,
) -> None:
    """飞书入口（09 §3）：长连接收卡片审批点击 + 同一进程里推送。固定指令在 D20 加。"""
    import logging

    from sentinel.chatops.approvals import ApprovalHandler
    from sentinel.chatops.client import LarkFeishuClient
    from sentinel.chatops.config import load_chatops_config, load_env_file
    from sentinel.chatops.gateway import serve
    from sentinel.chatops.notifier import Notifier
    from sentinel.core.graph.run import resume_online
    from sentinel.core.store import open_store
    from sentinel.core.toolbackend import Mode

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if env_file:
        typer.echo(f"已从 {env_file} 读入：{', '.join(load_env_file(env_file))}")  # 只打印键名
    cfg = load_chatops_config(config)
    if not cfg.approvers:
        typer.echo("提醒：approvers 为空，所有审批点击都会被拒绝（09 §5.1）", err=True)
    store, rem = open_store(), _rem_store()
    handler = ApprovalHandler(rem, store.append_audit, cfg, resume=lambda inv: resume_online(inv, Mode.LIVE))
    notifier = Notifier(store, rem, LarkFeishuClient(cfg.domain), cfg)
    typer.echo(
        f"[{cfg.env_label}] 连接 {cfg.domain}；审批人 {len(cfg.approvers)} 个，飞书审批上限 {cfg.max_risk}；"
        f"每 {cfg.poll_interval_s} 秒推送一次"
    )
    serve(handler, notifier)


@mcp_app.command("serve")
def mcp_serve(
    caller: Annotated[str, typer.Option("--as", help="调用方身份（config/governance.yaml 的 mcp.callers）")],
) -> None:
    """以 stdio 方式运行 MCP 服务：只导出 mcp_export 的只读工具，只返回摘要，每次调用写审计 mcp.call。"""
    import yaml

    from sentinel.core.graph.run import OBS_ROOT, load_packs
    from sentinel.core.observations.store import ObservationStore
    from sentinel.core.store import open_store
    from sentinel.core.toolbackend import Mode, open_backend
    from sentinel.mcp.server import build_server, exported_tools

    cfg = yaml.safe_load(Path("config/governance.yaml").read_text(encoding="utf-8"))["mcp"]
    if caller not in cfg["callers"]:
        raise typer.BadParameter(f"不认识的调用方 {caller}（config/governance.yaml 的 mcp.callers）")
    allowed = {t: cfg["targets"][t] for t in cfg["callers"][caller]}
    packs = load_packs()
    store = open_store()

    def call(pack: str, tool: str, target: str, args: dict) -> tuple[str, str]:  # type: ignore[type-arg]
        # 外部调用也是一次"调查"级别的观测：留下观测记录，可以追溯（UC7）
        tb = open_backend(packs[pack], f"inv_mcp_{ULID()}", Mode.LIVE, ObservationStore(OBS_ROOT, index=store))
        try:
            r = tb.call(tool, args)
        finally:
            tb.close()
        return r.status, r.digest

    def audit(who: str, tool: str, target: str, status: str) -> None:
        store.append_audit(
            investigation_id=None,
            agent_version=None,
            actor=f"mcp:{who}",
            type="mcp.call",
            payload={"tool": tool, "target": target, "status": status},
        )

    build_server(exported_tools(packs), caller=caller, allowed_targets=allowed, call=call, audit=audit).run("stdio")


@app.command("freeze")
def kill_freeze(
    reason: Annotated[str, typer.Option(help="为什么冻结（写进审计）")],
    who: Annotated[str, typer.Option("--as", help="操作人")],
) -> None:
    """紧急停止（07 §7）：执行器拒绝所有动作；待审批的全部作废；诊断照常，但不出修复方案。"""
    from sentinel.core.graph.run import PG_URL
    from sentinel.governance.kill_switch import PgFreezeState, freeze

    voided = freeze(PgFreezeState(PG_URL), _rem_store(), reason, f"human:{who}")
    typer.echo(f"已冻结（{reason}）：执行器拒绝所有动作；作废待审批 {len(voided)} 个 {' '.join(voided)}")
    typer.echo("解冻：sentinel unfreeze --reason ... --as <审批人>")


@app.command("unfreeze")
def kill_unfreeze(
    reason: Annotated[str, typer.Option(help="为什么解冻（写进审计）")],
    who: Annotated[str, typer.Option("--as", help="审批人（07 §7：解冻需要审批人角色）")],
) -> None:
    """解除紧急停止。被冻结作废的审批不会恢复，需要重新发起调查。"""
    from sentinel.core.graph.run import PG_URL
    from sentinel.governance.kill_switch import PgFreezeState, unfreeze

    unfreeze(PgFreezeState(PG_URL), _rem_store(), reason, f"human:{who}")
    typer.echo(f"已解冻（{reason}）")


@approvals_app.command("list")
def approvals_list() -> None:
    """最近的审批请求和状态。"""
    from datetime import UTC, datetime

    st = _rem_store()
    for r in st.list_approval_requests():
        d = st.get_decision(r.id)
        state = d.decision if d else ("expired" if datetime.now(UTC) > r.expires_at else "pending")
        plan = st.get_plan(r.plan_id)
        typer.echo(
            f"{r.id}  {state:<8} {plan.action_id if plan else '?':<34} 到期 {r.expires_at:%m-%d %H:%M}  {r.plan_id}"
        )


@approvals_app.command("show")
def approvals_show(approval_id: str) -> None:
    """审批人要看的全部内容（02 §3.3）。和飞书审批卡片共用 core/approval_view.py，两边内容一致（09 §4.3）。"""
    from sentinel.core.approval_view import load_approval_view

    v = load_approval_view(_rem_store(), approval_id)
    if v is None:
        raise typer.BadParameter(f"没有审批请求 {approval_id}")
    typer.echo(f"审批 {v.request.id}   调查 {v.plan.investigation_id}   状态 {v.state}")
    for label, value in v.lines():
        # 中文标签按 4 个字宽对齐（一个汉字占两个空格宽），和原来的输出逐字节一致
        typer.echo(f"  {label}{' ' * (2 * (4 - len(label)))}  {value}")


def _decide(approval_id: str, decision: Literal["approved", "rejected"], who: str, reason: str | None) -> None:
    """CLI 的审批：和飞书共用 decide_approval（09 §5.2），CLI 没有风险上限，然后同步等调查恢复。"""
    from sentinel.core.approval_view import decide_approval, resume_summary
    from sentinel.core.graph.run import resume_online
    from sentinel.core.store import open_store
    from sentinel.core.toolbackend import Mode

    r = decide_approval(_rem_store(), open_store().append_audit, approval_id, decision, f"human:{who}", reason)
    if r.outcome == "not_found":
        raise typer.BadParameter(f"没有审批请求 {approval_id}")
    if r.outcome == "duplicate":
        prev = r.view.decision if r.view else None
        by = f"已被 {prev.approver} {prev.decision}" if prev else "已经有决定"
        typer.echo(f"拒绝：{approval_id} {by}（一个审批只能决定一次）", err=True)
        raise typer.Exit(1)
    assert r.view is not None  # decided 一定带着重新读出来的 view
    plan = r.view.plan
    word = "批准" if decision == "approved" else "拒绝"
    typer.echo(f"已{word} {approval_id}（方案哈希 {plan.plan_hash[:16]}…），继续调查 {plan.investigation_id}")
    out = resume_online(plan.investigation_id, Mode.LIVE)
    status = resume_summary(out)  # type: ignore[arg-type]
    typer.echo(f"{plan.investigation_id}  状态 {status}  修复 {out.get('remediation')}")
    if status == "awaiting_confirmation":
        typer.echo(f"确认根因：sentinel investigations confirm {plan.investigation_id} --root-cause H1 --as <你的名字>")


@approvals_app.command("approve")
def approvals_approve(
    approval_id: str,
    who: Annotated[str, typer.Option("--as", help="审批人身份（第一版就是 CLI 传入的名字，07 §4）")],
    reason: Annotated[str | None, typer.Option()] = None,
) -> None:
    """批准并继续调查：执行器（独立进程）会重新校验哈希、有效期、幂等和前置检查之后才执行。"""
    _decide(approval_id, "approved", who, reason)


@approvals_app.command("reject")
def approvals_reject(
    approval_id: str,
    who: Annotated[str, typer.Option("--as", help="审批人身份")],
    reason: Annotated[str | None, typer.Option()] = None,
) -> None:
    """拒绝：调查以"只出报告"结束，不执行任何动作。"""
    _decide(approval_id, "rejected", who, reason)


@eval_app.command("gate")
def eval_gate(
    version: str,
    dataset_tag: Annotated[str | None, typer.Option(help="数据集版本标签（必填）")] = None,
    split: Annotated[str, typer.Option()] = "dev",
) -> None:
    """上线门槛（06 §7.1）：安全（不安全提议 = 0）、契约、回放覆盖率。任何一项不通过，退出码为 1。"""
    from sentinel.eval.gate import gate
    from sentinel.eval.guards import require_dataset_tag

    checks = gate(version, require_dataset_tag(dataset_tag), split)
    for c in checks:
        typer.echo(f"  {'✓' if c.passed else '✗'} {c.name:<6} {c.detail}")
    ok = all(c.passed for c in checks)
    typer.echo(f"{version}：{'通过上线门槛' if ok else '不允许上线'}")
    if not ok:
        raise typer.Exit(1)


@eval_app.command("curve")
def eval_curve(
    pack: Annotated[str, typer.Option()] = "es",
    out: Annotated[Path, typer.Option(help="输出的 PNG")] = Path("var/eval/curve.png"),
) -> None:
    """收敛曲线（06 §8）：每个版本 dev 上的 top1 和 95% 区间；无效改动画空心点；heldout 画菱形。"""
    from sentinel.core.store import open_store
    from sentinel.eval import results
    from sentinel.eval.curve import load_decisions, points, render

    # TODO(edge)：结果文件按领域分开存；现在只有 es，pack 参数只做校验
    if pack != "es":
        raise typer.BadParameter("目前只有 es 的评测结果")
    notes = {v.seq: v.note for v in open_store().list_versions()}
    pts = points(results.ROOT, notes, load_decisions())
    render(pts, out)
    for p in pts:
        mark = "○" if p.ineffective else "●"
        typer.echo(
            f"  {mark} v{p.seq:<3} {p.split:<7} {p.tag}  top1 {p.top1:.2f} [{p.low:.2f}, {p.high:.2f}]  {p.note[:50]}"
        )
    typer.echo(f"已生成 {out}")


ablation_app = typer.Typer(no_args_is_help=True, help="消融实验（05 §5）")
eval_app.add_typer(ablation_app, name="ablation")


@ablation_app.command("memory")
def ablation_memory(
    streams: Annotated[str, typer.Option(help="事故流的 split（只有 dev）")] = "dev",
    control: Annotated[str | None, typer.Option(help="关闭记忆的版本（必填），例如 v20")] = None,
    treatment: Annotated[str | None, typer.Option(help="打开记忆的版本（必填），例如 v23")] = None,
    dataset_tag: Annotated[str | None, typer.Option(help="数据集版本标签（必填）")] = None,
) -> None:
    """M-E1（08 M2 验收）：读两个版本已有的事故流结果，不重跑；输出增益、伤害率和区间（按第几次遇到分组）。

    版本和标签都必须显式给出（M2 独立审阅发现 3：写成默认值时，数据集换了新标签，这条命令会悄悄输出旧结果，
    绕过了"评测必须指定数据集版本标签"的护栏，08 §6.4）。"""
    from sentinel.eval.guards import require_dataset_tag

    if not control or not treatment:
        raise typer.BadParameter("--control 和 --treatment 必须显式给出")
    eval_compare(
        control, treatment, split=streams, dataset_tag=require_dataset_tag(dataset_tag), by="occurrence", stream=True
    )


@knowledge_app.command("index")
def knowledge_index(pack: Annotated[str, typer.Option()] = "es") -> None:
    """把专家包 knowledge/ 下的 runbook 切成段落、算向量，重建 es-memory 上的知识索引。"""
    from sentinel.core.knowledge.corpus import knowledge_hash, sections
    from sentinel.core.knowledge.es_index import build_index, index_name
    from sentinel.core.llm.config import load_models_config
    from sentinel.core.llm.embed import embed
    from sentinel.core.packs_registry import load_pack

    p = load_pack(pack)
    secs = sections(p)
    dims = int(load_models_config().purposes["embedding"].params()["output_dimensionality"])
    n = build_index(pack, secs, knowledge_hash(p), embed, dims)
    typer.echo(f"{index_name(pack)}：{n} 段（来自 {len({s.doc_id.split('#')[0] for s in secs})} 篇 runbook）")
    for s in secs:
        typer.echo(f"  {s.doc_id:<26} {','.join(s.detectors):<55} {s.heading}")


def awaiting_confirmation(audit_types: list[str]) -> bool:
    """调查是否停在 confirm 节点：修复验证通过了，调查还没有结束（02 §3.4；M3 独立审阅发现 2）。"""
    return "verify.passed" in audit_types and "investigation.closed" not in audit_types


@inv_app.command("confirm")
def inv_confirm(
    inv_id: str,
    who: Annotated[str, typer.Option("--as", help="确认人")],
    root_cause: Annotated[str | None, typer.Option("--root-cause", help="确认某个假设，例如 H1")] = None,
    label: Annotated[str | None, typer.Option(help="直接给出根因标签（和 H1 不同就是修正）")] = None,
    decline: Annotated[bool, typer.Option(help="不确认：调查结束，不写记忆（02 §3.4）")] = False,
) -> None:
    """人确认根因（02 §1 confirm 节点；05 §3.1）：确认或修正之后写入事故记忆；不确认就不写。"""
    from sentinel.contracts import Diagnosis
    from sentinel.core.graph.confirm import record_confirmation
    from sentinel.core.graph.run import resume_online
    from sentinel.core.packs_registry import load_pack
    from sentinel.core.store import open_store
    from sentinel.core.toolbackend import Mode

    store = open_store()
    rec = store.get_investigation(inv_id)
    if rec is None or rec.diagnosis is None:
        raise typer.BadParameter(f"调查 {inv_id} 不存在或没有诊断")
    # 只有停在 confirm 的调查才能确认根因（02 §3.4：审批在修复前、确认在修复后，两者分开）：
    # 验证通过、还没结束。否则恢复会把还在等审批的调查当成"审批被拒绝"关掉（M3 独立审阅发现 2）
    if not awaiting_confirmation([e.type for e in store.audit_events(inv_id)]):
        raise typer.BadParameter(f"调查 {inv_id} 没有停在等待确认根因（要先修复并验证通过；已经结束的调查不能再确认）")
    if not decline:
        if (root_cause is None) == (label is None):
            raise typer.BadParameter("--root-cause 和 --label 二选一（或者 --decline）")
        try:
            e = record_confirmation(
                store.append_audit,
                inv_id=inv_id,
                diagnosis=Diagnosis.model_validate(rec.diagnosis),
                hypothesis=root_cause,
                label=label,
                who=f"human:{who}",
                # 合法标签取这次调查所属的专家包（问题事件里记着 pack）
                valid_labels=[r.label for r in load_pack(rec.findings[0]["pack"]).manifest.root_causes],
            )
        except ValueError as err:
            raise typer.BadParameter(str(err)) from err
        typer.echo(f"{'已修正' if e.type == 'rootcause.corrected' else '已确认'}根因：{e.payload['label']}")
    out = resume_online(inv_id, Mode.LIVE)
    mem = out.get("memory_id")
    typer.echo(f"{inv_id}  状态 {out.get('status')}  " + (f"已写入记忆 {mem}" if mem else "没有写入记忆"))


@inv_app.command("context")
def inv_context(inv_id: str) -> None:
    """上下文报告（04 §3.9；golden-demo 第 4 步）：每层的 token、放了什么、删了什么、为什么删，以及 L2 压缩记录。"""
    from collections import defaultdict

    from sentinel.contracts import ContextBundle
    from sentinel.core.store import open_store

    store = open_store()
    r = store.get_investigation(inv_id)
    if r is None or r.context_bundle is None:
        raise typer.BadParameter(f"调查 {inv_id} 没有上下文包")
    b = ContextBundle.model_validate(r.context_bundle)
    by: dict[str, list] = defaultdict(list)  # type: ignore[type-arg]
    for it in b.items:
        by[it.layer].append(it)
    typer.echo(f"上下文报告 {inv_id}   策略 {b.policy_hash[:19]}…")
    for layer in ("L0", "L1", "L5", "L3", "L4"):
        items = by.get(layer, [])
        kept = [i for i in items if i.included]
        tokens, dropped = b.tokens_by_layer.get(layer, 0), len(items) - len(kept)
        typer.echo(f"  {layer}  {tokens:>5} token  放入 {len(kept)} 条  删除 {dropped} 条")
        for i in items:
            if not i.included:
                typer.echo(f"        删除 {i.ref}（{i.tokens} token）：{i.drop_reason}")
    for e in store.audit_events(inv_id):
        if e.type == "diagnosis.completed" and e.payload.get("compaction", {}).get("compacted"):
            c = e.payload["compaction"]
            typer.echo(f"  L2 压缩：{len(c['compacted'])} 条，{c['tokens_before']} → {c['tokens_after']} token")
