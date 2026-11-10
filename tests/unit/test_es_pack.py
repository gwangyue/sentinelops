"""ES 专家包：检测器、摘要器、fact builder 在真实录制（ES 9.5.3，kind）上的行为。全部用回放，不访问网络。"""

import json
from pathlib import Path

import pytest

from sentinel.core.detect import load_env, run_detect
from sentinel.core.observations.store import ObservationStore
from sentinel.core.packs_registry import load_pack
from sentinel.core.toolbackend import Mode, ToolBackend
from sentinel.packs.es.thresholds import setting
from sentinel.sdk import estimate_tokens, fingerprint

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "es"
PACK = load_pack("es")
ENV = load_env("es-target")

# 每份录制应该触发的检测器（录制时制造的故障，见 08 M1 D2 计划）
EXPECTED = {
    "healthy": set(),
    "disk_watermark": {"es.disk_watermark", "es.unassigned_shards"},
    "node_down": {"es.node_count", "es.unassigned_shards"},
    "shard_limit": {"es.shard_limit"},
}


def detect(tmp_path: Path, name: str):  # type: ignore[no-untyped-def]
    tb = ToolBackend(PACK.tools, ObservationStore(tmp_path), f"inv_{name}", Mode.REPLAY, fixture=FIX / f"{name}.jsonl")
    return run_detect(PACK, "es-target", tb, ENV)


def test_pack_loads() -> None:
    assert len(PACK.tools) == 13 and len(PACK.detectors) == 4 and len(PACK.fact_builders) == 1


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_detectors_on_recordings(tmp_path: Path, name: str) -> None:
    res = detect(tmp_path, name)
    assert {f.detector for f in res.findings} == EXPECTED[name]
    for f in res.findings:
        assert f.observation_ids, "每个 Finding 都要指回观测记录（证据链的起点）"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_replay_is_reproducible(tmp_path: Path, name: str) -> None:
    def key(r):  # type: ignore[no-untyped-def]
        return sorted(
            (f.detector, f.severity, f.object_ref, f.summary, tuple(sorted(f.metrics.items()))) for f in r.findings
        )

    assert key(detect(tmp_path / "a", name)) == key(detect(tmp_path / "b", name))


def test_finding_object_and_severity(tmp_path: Path) -> None:
    disk = next(f for f in detect(tmp_path, "disk_watermark").findings if f.detector == "es.disk_watermark")
    assert disk.object_ref == "es-target-es-data-2" and disk.severity == "warn"
    limit = next(f for f in detect(tmp_path, "shard_limit").findings if f.detector == "es.shard_limit")
    assert limit.severity == "critical"  # 分片数已经到达上限


def test_fingerprint_ignores_values() -> None:
    # 02 §2：指纹不含数值，91% 和 93% 是同一个问题
    assert fingerprint("es-target", "es.disk_watermark", "n2", "warn") == fingerprint(
        "es-target", "es.disk_watermark", "n2", "warn"
    )
    assert fingerprint("es-target", "es.disk_watermark", "n2", "warn") != fingerprint(
        "es-target", "es.disk_watermark", "n2", "critical"
    )


def test_fact_sheet_disk(tmp_path: Path) -> None:
    sheet = detect(tmp_path, "disk_watermark").fact_sheets[0]
    disk = [f for f in sheet.facts if f.key == "node.disk_used_pct"]
    assert [(f.subject, f.status) for f in disk] == [("es-target-es-data-2", "abnormal")]
    assert sheet.omitted_normal_count > 0  # 正常的节点只计数，不列出


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_summaries_fit_budget_without_truncation(name: str) -> None:
    # 预算要定得够用：在真实录制上，摘要器的原始输出就应该在预算内，不依赖截断
    for line in (FIX / f"{name}.jsonl").read_text().splitlines():
        rec = json.loads(line)
        if rec.get("error"):
            continue
        spec = PACK.tools[rec["tool"]]
        digest = spec.impl.summarize(rec["raw"], {**spec.impl.defaults, **rec["args"]})
        assert estimate_tokens(digest) <= spec.decl.output_budget_tokens, rec["tool"]


def test_allocation_explain_digest_has_reason(tmp_path: Path) -> None:
    tb = ToolBackend(PACK.tools, ObservationStore(tmp_path), "inv_e", Mode.REPLAY, fixture=FIX / "disk_watermark.jsonl")
    d = tb.call("es.allocation_explain").digest
    assert "es-target-es-data-2: no [disk_threshold]" in d


def test_threshold_setting() -> None:
    k = "cluster.routing.allocation.disk.watermark.high"
    assert setting(None, k) == 90.0
    assert setting({"persistent": {k: "92%"}}, k) == 92.0
    assert setting({"persistent": {k: "92%"}, "transient": {k: "80%"}}, k) == 80.0
    assert setting({"persistent": {k: "10gb"}}, k) == 90.0  # 字节形式暂时退回默认值（TODO(edge)）
