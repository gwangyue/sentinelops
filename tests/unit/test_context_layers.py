"""D6 上下文层：L3 变更、L4 知识、L5 记忆的渲染、预算降级、L2 压缩、RRF。离线。"""

from datetime import UTC, datetime, timedelta

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sentinel.contracts import Fact, FactSheet, Finding
from sentinel.core.context.build import build_context
from sentinel.core.context.changes import Change, Snapshot, collect_changes, diff
from sentinel.core.context.memory_layer import MemoryHit, render_hit
from sentinel.core.diagnosis.compaction import CompactionRecord, compact
from sentinel.core.knowledge.corpus import knowledge_hash, sections
from sentinel.core.knowledge.es_index import KnowledgeHit
from sentinel.core.packs_registry import load_pack
from sentinel.core.retrieval.rrf import rrf
from sentinel.core.versioning.components import load_context_policy

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
PACK = load_pack("es")


# ---------- L3 ----------


def test_diff_settings_nodes_templates() -> None:
    before = {"persistent": {"a": "1"}, "transient": {}}
    after = {"persistent": {"a": "2", "cluster.routing.allocation.enable": "none"}, "transient": {}}
    assert diff(before, after) == [
        ("persistent.a", "1", "2"),
        ("persistent.cluster.routing.allocation.enable", "（无）", "none"),
    ]
    nodes_b = {"nodes": {"id1": {"name": "n1"}, "id2": {"name": "n2"}}}
    nodes_a = {"nodes": {"id1": {"name": "n1"}}}
    assert diff(nodes_b, nodes_a) == [("nodes.id2", "{name=n2}", "（无）")]
    # 元素带 name 的列表按 name 比较：顺序变化不算变更
    t1 = {"index_templates": [{"name": "a", "p": 1}, {"name": "b", "p": 2}]}
    t2 = {"index_templates": [{"name": "b", "p": 2}, {"name": "a", "p": 1}]}
    assert diff(t1, t2) == []


class _Src:
    def __init__(self, snap: Snapshot | None) -> None:
        self.snap = snap

    def previous(self, tool: str) -> Snapshot | None:
        return self.snap


def test_collect_changes_window() -> None:
    cur = {"es.cluster_settings": ({"persistent": {"x": "1"}}, "obs_2")}
    recent = _Src(Snapshot({"persistent": {}}, NOW - timedelta(hours=1)))
    old = _Src(Snapshot({"persistent": {}}, NOW - timedelta(hours=48)))
    [c] = collect_changes(cur, recent, NOW, 24)
    assert c.path == "persistent.x" and c.observation_id == "obs_2" and "obs_2" in c.line()
    assert collect_changes(cur, old, NOW, 24) == []  # 24 小时窗口外的快照不比较
    assert collect_changes(cur, _Src(None), NOW, 24) == []  # 没有快照（第一次巡检）


# ---------- 组装、预算、降级 ----------


def _finding() -> Finding:
    return Finding(
        id="fnd_1",
        fingerprint="x",
        pack="es",
        detector="es.unassigned_shards",
        target="t",
        object_ref=None,
        severity="warn",
        summary="1 个分片未分配",
        metrics={},
        observed_at=NOW,
        observation_ids=["obs_1"],
    )


def _sheet(n_normal: int) -> FactSheet:
    facts = [
        Fact(
            key="cluster.status",
            subject="cluster",
            value="yellow",
            baseline=None,
            status="abnormal",
            observation_id="obs_1",
        )
    ]
    facts += [
        Fact(
            key="node.heap_used_pct",
            subject=f"n{i}",
            value=10.0,
            baseline=None,
            status="unknown",
            observation_id=f"obs_h{i}",
        )
        for i in range(n_normal)
    ]
    return FactSheet(target="t", generated_at=NOW, facts=facts, omitted_normal_count=0)


def _change(i: int) -> Change:
    return Change("es.cluster_settings", f"persistent.k{i}", "（无）", "v", NOW - timedelta(minutes=i), f"obs_c{i}")


def _kb(i: int) -> KnowledgeHit:
    return KnowledgeHit(f"unassigned_shards.md#{i}", "Runbook", f"Section {i}", "text " * 200, "src")


def _mem(i: int) -> MemoryHit:
    return MemoryHit(f"mem_{i}", 0.9 - i / 10, 3, "resolved", False, "s", "es.disk_high_watermark", "r", None)


def test_layers_render_in_order() -> None:
    policy = load_context_policy()
    b = build_context(
        "inv", [_finding()], [_sheet(0)], {}, policy, changes=[_change(1)], knowledge=[_kb(0)], memories=[_mem(0)]
    )
    r = b.rendered
    assert r.index("## L0") < r.index("## L1") < r.index("## L5") < r.index("## L3") < r.index("## L4")
    assert "persistent.k1" in r and "待验证的假设" in r and "来源：src" in r
    assert set(b.tokens_by_layer) == {"L0", "L1", "L3", "L4", "L5"}


def test_degrade_order_and_protected_items() -> None:
    policy = load_context_policy()
    policy = {**policy, "budgets": {**policy["budgets"], "preassembled_total": 900}}
    b = build_context(
        "inv",
        [_finding()],
        [_sheet(30)],
        {},
        policy,
        changes=[_change(i) for i in range(5)],
        knowledge=[_kb(0), _kb(1)],
        memories=[_mem(0), _mem(1)],
    )
    dropped = {(i.layer, i.drop_reason) for i in b.items if not i.included}
    # L4 最先被删，其次 L3；L0 和异常事实、L5 的 top-1 不删
    assert ("L4", "degrade:L4") in dropped
    assert all(i.included for i in b.items if i.layer == "L0")
    assert next(i for i in b.items if i.layer == "L1" and i.ref == "obs_1").included
    assert next(i for i in b.items if i.layer == "L5" and i.ref == "mem_0").included
    assert sum(b.tokens_by_layer.values()) <= 900


def test_l3_drops_oldest_first() -> None:
    policy = load_context_policy()
    policy = {**policy, "budgets": {**policy["budgets"], "L3": 40}}
    b = build_context("inv", [_finding()], [_sheet(0)], {}, policy, changes=[_change(i) for i in range(4)])
    l3 = [i for i in b.items if i.layer == "L3"]
    assert l3[0].ref == "obs_c0" and l3[0].included  # 最新的变更（0 分钟前）留着
    assert not l3[-1].included and l3[-1].drop_reason == "budget"


def test_memory_rendered_as_hypothesis() -> None:
    s = render_hit(_mem(0))
    assert s.startswith('<memory id="mem_0"') and "必须用本次的观测记录证明或排除它" in s


# ---------- L2 压缩 ----------


def _tool_msg(i: int, size: int) -> ToolMessage:
    body = f'<untrusted source="obs_{i:04d}AAAA">\n[ok] line one of tool {i}\n' + ("x" * size) + "\n</untrusted>"
    return ToolMessage(content=body, tool_call_id=f"c{i}", name=f"es_tool_{i}")


def test_compaction_keeps_recent_and_ids() -> None:
    msgs = [HumanMessage("q")] + [m for i in range(6) for m in (AIMessage(""), _tool_msg(i, 3000))]
    rec = CompactionRecord()
    out = compact(msgs, l2_max=5000, trigger_ratio=0.7, keep_recent=3, record=rec)
    tools = [m for m in out if isinstance(m, ToolMessage)]
    assert all("x" * 100 in str(t.content) for t in tools[-3:])  # 最近 3 条保持原样
    first = str(tools[0].content)
    assert "obs_0000AAAA" in first and "line one of tool 0" in first and len(first) < 300
    assert rec.compacted == ["obs_0000AAAA", "obs_0001AAAA", "obs_0002AAAA"] and rec.tokens_after < rec.tokens_before


def test_compaction_not_triggered_below_threshold() -> None:
    msgs = [HumanMessage("q"), AIMessage(""), _tool_msg(0, 100)]
    assert compact(msgs, 20000, 0.7, 3, CompactionRecord()) == msgs


# ---------- RRF、语料 ----------


def test_rrf_fuses_and_is_deterministic() -> None:
    assert rrf({"a": ["x", "y"], "b": ["y", "z"]})[0] == "y"
    assert rrf({"a": ["p", "q"], "b": ["q", "p"]}) == ["p", "q"]  # 同分按 ID 排序


def test_corpus_sections_and_hash() -> None:
    secs = sections(PACK)
    assert len({s.doc_id.split("#")[0] for s in secs}) == 5
    assert all(s.detectors and s.source.startswith("Elastic docs") for s in secs)
    assert len(knowledge_hash(PACK)) == 64
