"""记忆服务（05 篇）：指纹、召回和精排、写入与合并、易混淆、反馈与失效、快照。离线（本地存储 + 假向量）。"""

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sentinel.contracts import Fact, FactSheet, Finding
from sentinel.core.context.changes import Change
from sentinel.core.memory import feedback
from sentinel.core.memory.fingerprint import build, weighted_jaccard
from sentinel.core.memory.policy import load_memory_policy
from sentinel.core.memory.provider import StoreMemoryProvider
from sentinel.core.memory.snapshot import export, load
from sentinel.core.memory.store import LocalMemoryStore
from sentinel.core.memory.write import write_confirmed

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
NS = "es/es-target"
POLICY = load_memory_policy()


def fake_embed(texts: Sequence[str]) -> list[list[float]]:
    # 确定性的假向量：按词哈希到 16 维，相同的词得到相近的向量
    out = []
    for t in texts:
        v = [0.0] * 16
        for w in t.lower().split():
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 16] += 1.0
        out.append(v)
    return out


def finding(detector: str, obj: str | None = None) -> Finding:
    return Finding(
        id=f"fnd_{detector}",
        fingerprint="x",
        pack="es",
        detector=detector,
        target="es-target",
        object_ref=obj,
        severity="warn",
        summary="s",
        metrics={},
        observed_at=NOW,
        observation_ids=["obs_1"],
    )


def sheet(node: str, disk: float) -> FactSheet:
    return FactSheet(
        target="t",
        generated_at=NOW,
        omitted_normal_count=0,
        facts=[
            Fact(
                key="cluster.status",
                subject="cluster",
                value="yellow",
                baseline=None,
                status="abnormal",
                observation_id="obs_1",
            ),
            Fact(
                key="node.disk_used_pct",
                subject=node,
                value=disk,
                baseline=None,
                status="abnormal",
                observation_id="obs_2",
            ),
        ],
    )


def disk_fp(node: str = "es-target-es-data-2", disk: float = 92.0) -> dict[str, list[str]]:
    return build([finding("es.unassigned_shards"), finding("es.disk_watermark", node)], [sheet(node, disk)], [])


# ---------- 指纹 ----------


def test_fingerprint_bands_and_hides_names() -> None:
    fp = disk_fp()
    assert fp["abnormal_facts"] == ["cluster.status:yellow", "node.disk_used_pct:high"]
    assert fp["objects"] == ["cluster", "node"]
    assert "es-target-es-data-2" not in str(fp) and "92" not in str(fp)
    # 换一个节点、换一个数值，指纹完全相同（05 §3.2）
    assert disk_fp("es-target-es-data-0", 95.5) == fp


def test_change_keys_without_values() -> None:
    c = [
        Change("es.cluster_settings", "persistent.cluster.routing.allocation.enable", "（无）", "none", NOW, "o"),
        Change("es.nodes_info", "nodes.abc123", "{name=n2}", "（无）", NOW, "o"),
    ]
    fp = build([finding("es.unassigned_shards")], [], c)
    assert fp["changes"] == ["cluster_settings:cluster.routing.allocation.enable", "nodes_info:nodes:removed"]


def test_weighted_jaccard() -> None:
    w = POLICY["fingerprint_weights"]
    a = ["det:x", "fact:y", "obj:z"]
    assert weighted_jaccard(a, a, w) == 1.0
    assert weighted_jaccard(a, ["det:x"], w) == 3 / 6  # det 3 /（det 3 + fact 2 + obj 1）


# ---------- 写入、合并、易混淆 ----------


def _write(
    store: LocalMemoryStore,
    fp: dict[str, list[str]],
    label: str,
    events: list[tuple[str, dict[str, Any]]],
    inv: str = "inv_1",
) -> Any:
    return write_confirmed(
        store,
        namespace=NS,
        investigation_id=inv,
        fingerprint=fp,
        root_cause_label=label,
        root_cause_description="d",
        corrected=False,
        confirmed_by="human:test",
        outcome="resolved",
        action_taken=None,
        target_version="9.5.3",
        embed=fake_embed,
        policy=POLICY,
        audit=lambda t, p: events.append((t, p)),
        now=NOW,
    )


def test_merge_same_label_similar_fingerprint() -> None:
    store, ev = LocalMemoryStore(), []
    a = _write(store, disk_fp(), "es.disk_high_watermark", ev, "inv_1")
    b = _write(store, disk_fp("es-target-es-data-0"), "es.disk_high_watermark", ev, "inv_2")
    assert a.id == b.id and b.occurrences == 2 and b.source_investigations == ["inv_1", "inv_2"]
    assert len(store.items) == 1 and [t for t, _ in ev] == ["memory.written", "memory.written"]


def test_confused_when_same_fingerprint_different_label() -> None:
    store, ev = LocalMemoryStore(), []
    a = _write(store, disk_fp(), "es.disk_high_watermark", ev)
    b = _write(store, disk_fp(), "es.allocation_disabled", ev)
    assert a.id != b.id and b.confused_with == [a.id]
    assert store.items[a.id].record.confused_with == [b.id]  # 双向标记


def test_text_is_template_only() -> None:
    store, ev = LocalMemoryStore(), []
    r = _write(store, disk_fp(), "es.disk_high_watermark", ev)
    assert r.symptom_text.startswith("detectors: es.disk_watermark, es.unassigned_shards")
    assert "es-target" not in r.symptom_text


# ---------- 检索和精排 ----------


def test_retrieves_similar_and_filters_unrelated() -> None:
    store, ev = LocalMemoryStore(), []
    disk = _write(store, disk_fp(), "es.disk_high_watermark", ev)
    _write(store, build([finding("es.shard_limit")], [], []), "es.shard_limit_reached", ev)
    p = StoreMemoryProvider(store, NS, POLICY, fake_embed)
    hits = p.similar([finding("es.unassigned_shards"), finding("es.disk_watermark", "n9")], [sheet("n9", 93.0)], [], 3)
    assert [h.memory_id for h in hits] == [disk.id]  # 分片上限那条不相关，被阈值挡住
    assert hits[0].similarity >= POLICY["min_score"]


def test_namespace_and_invalidated_are_filtered() -> None:
    store, ev = LocalMemoryStore(), []
    r = _write(store, disk_fp(), "es.disk_high_watermark", ev)
    p_other = StoreMemoryProvider(store, "es/other", POLICY, fake_embed)
    q = ([finding("es.unassigned_shards"), finding("es.disk_watermark", "n9")], [sheet("n9", 93.0)], [])
    assert p_other.similar(*q, 3) == []  # 另一个集群的经验不套用（05 §3.6 第 3 道防线）
    store.items[r.id].record = r.model_copy(update={"invalidated": True})
    assert StoreMemoryProvider(store, NS, POLICY, fake_embed).similar(*q, 3) == []


def test_confused_label_is_shown() -> None:
    store, ev = LocalMemoryStore(), []
    _write(store, disk_fp(), "es.disk_high_watermark", ev)
    _write(store, disk_fp(), "es.allocation_disabled", ev)
    hits = StoreMemoryProvider(store, NS, POLICY, fake_embed).similar(
        [finding("es.unassigned_shards"), finding("es.disk_watermark", "n9")], [sheet("n9", 93.0)], [], 3
    )
    assert {h.root_cause_label for h in hits} == {"es.disk_high_watermark", "es.allocation_disabled"}
    assert all(h.confused_labels for h in hits)


# ---------- 反馈与失效 ----------


def test_feedback_helpful_and_harm_invalidation() -> None:
    store, ev = LocalMemoryStore(), []
    r = _write(store, disk_fp(), "es.disk_high_watermark", ev)
    feedback.update(
        store,
        shown=[r.id],
        confirmed_label="es.disk_high_watermark",
        harmed=[],
        policy=POLICY,
        audit=lambda t, p: ev.append((t, p)),
    )
    m = store.items[r.id]
    assert (m.record.hit_count, m.record.helpful_count, m.record.invalidated) == (1, 1, False)
    for _ in range(2):  # 伤害 2 次就失效
        feedback.update(
            store,
            shown=[r.id],
            confirmed_label="es.allocation_disabled",
            harmed=[r.id],
            policy=POLICY,
            audit=lambda t, p: ev.append((t, p)),
        )
    assert store.items[r.id].record.invalidated and ev[-1][0] == "memory.invalidated"


def test_low_helpful_ratio_invalidates() -> None:
    store, ev = LocalMemoryStore(), []
    r = _write(store, disk_fp(), "es.disk_high_watermark", ev)
    for _ in range(5):
        feedback.update(
            store,
            shown=[r.id],
            confirmed_label="es.node_unavailable",
            harmed=[],
            policy=POLICY,
            audit=lambda t, p: ev.append((t, p)),
        )
    assert store.items[r.id].record.invalidated  # 命中 5 次，有用率 0 < 0.3


# ---------- 快照 ----------


def test_snapshot_roundtrip(tmp_path: Path) -> None:
    store, ev = LocalMemoryStore(), []
    _write(store, disk_fp(), "es.disk_high_watermark", ev)
    assert export(store, NS, tmp_path / "m.jsonl") == 1
    other = LocalMemoryStore()
    assert load(other, tmp_path / "m.jsonl") == 1 and list(other.items) == list(store.items)
