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
bench_app = typer.Typer(no_args_is_help=True, help="故障台（02 §6）")
dataset_app = typer.Typer(no_args_is_help=True, help="数据集（06 §5）")
eval_app = typer.Typer(no_args_is_help=True, help="评测（06 篇）")
knowledge_app = typer.Typer(no_args_is_help=True, help="L4 知识层（04 §3.5）")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(bench_app, name="bench")
app.add_typer(dataset_app, name="dataset")
app.add_typer(eval_app, name="eval")

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
) -> None:
    """巡检一次：没有问题就结束（不调用 LLM）；有问题就只读诊断并出报告（02 §2）。"""
    import time

    from sentinel.core.graph.run import patrol as run_patrol
    from sentinel.core.toolbackend import Mode
    from sentinel.core.versioning.freeze import FreezeError

    t0 = time.monotonic()
    try:
        s = run_patrol(target, pack, Mode(backend), fixture)
    except FreezeError as e:
        typer.echo(f"拒绝开始调查：{e}", err=True)
        raise typer.Exit(1) from e
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


@eval_app.command("run")
def eval_run(
    pack: Annotated[str, typer.Option()] = "es",
    split: Annotated[str, typer.Option()] = "dev",
    dataset_tag: Annotated[str | None, typer.Option(help="数据集版本标签（必填，不能是 latest）")] = None,
    reps: Annotated[int, typer.Option(help="每个用例重复几次")] = 3,
    version: Annotated[str | None, typer.Option(help="只能等于当前版本；不填就是当前版本")] = None,
) -> None:
    """在回放模式下评测当前版本（06 §9）。"""
    from sentinel.core.versioning.freeze import FreezeError
    from sentinel.eval.guards import EvalGuardError
    from sentinel.eval.runner import EvalError, run
    from sentinel.eval.stats.bootstrap import stability

    try:
        s = run(pack, split, dataset_tag, reps, version)
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
    by: Annotated[str | None, typer.Option(help="分组：difficulty（06 §6.2；08 §7 要求按难度分组报告）")] = None,
) -> None:
    """配对比较 B − A 的 top1：点估计、按场景聚类的 bootstrap 95% 区间、判断（06 §4.2）。"""
    from sentinel.eval import results
    from sentinel.eval.dataset import load_examples
    from sentinel.eval.guards import require_dataset_tag
    from sentinel.eval.stats.bootstrap import compare

    tag = require_dataset_tag(dataset_tag)

    def load(name: str) -> list:  # type: ignore[type-arg]
        return results.load(results.result_file(name.replace("baseline:", "baseline-"), tag, split))

    ra, rb = load(a), load(b)
    groups: dict[str, set[str] | None] = {"全部": None}
    if by == "difficulty":
        # RunScore 里没有难度，按用例 ID 从 git 里的数据集查（数据集以 git 为准，06 §5.2）
        diff = {e.id: e.metadata.difficulty for e in load_examples("es")}
        for d in ("single", "distractor", "compound"):
            groups[d] = {eid for eid, v in diff.items() if v == d}
    elif by is not None:
        raise typer.BadParameter("--by 目前只支持 difficulty")
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
