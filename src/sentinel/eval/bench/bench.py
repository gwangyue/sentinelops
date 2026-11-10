"""故障台：按场景定义注入故障、等症状出现、录制全部工具输出、撤销、写入数据集。

负责：编排。写操作全部交给 deploy/chaos/ 里的脚本或 Chaos Mesh 模板，Python 这边只拿只读账号
      （写凭证只在 exec/ 和故障脚本里，08 §6.4）。
不负责：故障本身怎么注入（脚本）、用例怎么评分（runner / scorers）。
录制时不调用 LLM：把所有只读工具扫一遍，回放才能覆盖不同版本的 Agent 可能发起的调用（ADR-003）。
"""

import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ulid import ULID

from sentinel.contracts import EvalExample, ExampleInputs, ExampleMetadata, ExampleOutputs, FaultRef, ScenarioSpec
from sentinel.core.api import (
    LoadedPack,
    Mode,
    ObservationStore,
    ToolBackend,
    load_env,
    load_pack,
    open_backend,
    resolve_credential,
    run_detect,
)
from sentinel.eval.dataset import (
    REPO,
    append_example,
    load_scenarios,
    next_recording,
    split_for,
    write_fixture_index,
)
from sentinel.sdk import DetectContext, ReadOnlyHttp

DEPLOY = REPO / "deploy"
CTX = "kind-sentinel"
TARGET = "es-target"
POLL_S = 5
SCRATCH = REPO / "var" / "bench"  # 故障台自己的观测记录（等症状时的轮询），不进数据集


class BenchError(RuntimeError):
    pass


def run_fault(fault: FaultRef, action: str) -> None:
    path = DEPLOY / fault.ref
    if fault.kind == "chaos":
        verb = "apply" if action == "inject" else "delete"
        extra = [] if verb == "apply" else ["--ignore-not-found"]
        subprocess.run(["kubectl", "--context", CTX, verb, "-f", str(path), *extra], check=True, capture_output=True)
    else:
        env = {**os.environ, **{k.upper(): str(v) for k, v in fault.params.items()}}
        subprocess.run([str(path), action], check=True, env=env, capture_output=True)


def inject_all(spec: ScenarioSpec) -> None:
    """先注入干扰项，再注入主故障：主故障出现时，干扰项已经在集群里了（03 §6.1）。"""
    for d in spec.distractors:
        run_fault(d, "inject")
    run_fault(spec.fault, "inject")


def revert_all(spec: ScenarioSpec) -> None:
    """按相反的顺序撤销。某一步失败也继续撤销其余的，不把集群留在半坏的状态。"""
    errors = []
    for f in [spec.fault, *reversed(spec.distractors)]:
        try:
            run_fault(f, "revert")
        except subprocess.CalledProcessError as e:
            errors.append(f"{f.ref}: {e}")
    if errors:
        raise BenchError("撤销失败：" + "; ".join(errors))


def _live(pack: LoadedPack) -> ToolBackend:
    return open_backend(pack, f"inv_bench_{ULID()}", Mode.LIVE, ObservationStore(SCRATCH))


def _health(pack: LoadedPack) -> dict[str, Any]:
    tb = _live(pack)
    try:
        r = tb.call("es.cluster_health")
        return r.raw if r.ok else {}
    finally:
        tb.close()


def _wait(pred: Callable[[], bool], timeout_s: float) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if pred():
            return round(time.monotonic() - t0, 1)
        time.sleep(POLL_S)
    return None


def wait_green(pack: LoadedPack, timeout_s: float = 600) -> None:
    expected = load_env(TARGET).get("expected_nodes", 3)

    def ok() -> bool:
        h = _health(pack)
        return h.get("status") == "green" and h.get("number_of_nodes") == expected

    if _wait(ok, timeout_s) is None:
        raise BenchError(f"{timeout_s:.0f} 秒内集群没有恢复 green")


def wait_symptom(pack: LoadedPack, spec: ScenarioSpec) -> float | None:
    """等场景声明的检测器触发（03 §6.1 symptom_wait）。返回从注入开始的秒数，超时返回 None。"""
    detector = pack.detectors[spec.symptom_wait.detector]
    wait_nodes = spec.fault.params.get("wait_nodes")
    env = load_env(TARGET)

    def fired() -> bool:
        tb = _live(pack)
        try:
            if wait_nodes is not None:
                r = tb.call("es.cluster_health")
                if not r.ok or r.raw["number_of_nodes"] < wait_nodes:
                    return False
            return bool(detector(DetectContext(pack=pack.manifest.pack, target=TARGET, tools=tb, env=env)))
        finally:
            tb.close()

    return _wait(fired, spec.symptom_wait.timeout_s)


def sweep(pack: LoadedPack, tb: ToolBackend) -> None:
    """录制：检测器和 fact builder 的调用 + 每个工具的默认调用 + 每个非 green 索引的带参调用。"""
    run_detect(pack, TARGET, tb, load_env(TARGET))
    for tid, t in pack.tools.items():
        if t.impl.args_schema is None or not any(f.is_required() for f in t.impl.args_schema.model_fields.values()):
            tb.call(tid)
    indices = tb.call("es.cat_indices")
    if indices.ok:
        for row in indices.raw:
            if row["health"] != "green":
                tb.call("es.cat_shards", {"index": row["index"]})
                tb.call("es.index_settings", {"index": row["index"]})
    # TODO(edge): allocation_explain 针对每个未分配分片的带参调用；目前只录默认的那一个


def _target_version() -> str:
    c = resolve_credential("es-readonly")
    http = ReadOnlyHttp(c.url, c.user, c.password)
    try:
        return str(http.get("/")["version"]["number"])
    finally:
        http.close()


def inject(pack_name: str, scenario_id: str, *, revert: bool = False) -> float:
    """golden-demo 第 2 步：注入并等症状出现，返回秒数。默认**不撤销**：第 3 步要在故障还在的时候诊断。
    撤销用 revert()。症状没出现时总是撤销，不把集群留在半坏的状态。"""
    pack = load_pack(pack_name)
    spec = load_scenarios(pack_name)[scenario_id]
    wait_green(pack)
    t0 = time.monotonic()  # 从开始注入算起（和 S2 的口径一致）：注入脚本本身的耗时也算在内
    inject_all(spec)
    injected = time.monotonic() - t0
    fired = wait_symptom(pack, spec)
    if fired is None or revert:
        revert_fault(pack_name, scenario_id)
    if fired is None:
        raise BenchError(f"{spec.symptom_wait.timeout_s} 秒内检测器 {spec.symptom_wait.detector} 没有触发，已撤销")
    return round(injected + fired, 1)


def revert_fault(pack_name: str, scenario_id: str) -> None:
    """撤销场景的故障，等集群恢复 green。"""
    pack = load_pack(pack_name)
    revert_all(load_scenarios(pack_name)[scenario_id])
    wait_green(pack)


def record(pack_name: str, scenario_id: str, n: int, log: Callable[[str], None] = print) -> list[str]:
    pack = load_pack(pack_name)
    spec = load_scenarios(pack_name)[scenario_id]
    version = _target_version()
    made = []
    for _ in range(n):
        k = next_recording(pack_name, scenario_id)
        rel = Path("fixtures") / scenario_id / f"r{k}.jsonl"
        fixture = REPO / rel
        fixture.unlink(missing_ok=True)
        wait_green(pack)
        t0 = time.monotonic()
        inject_all(spec)
        try:
            took = wait_symptom(pack, spec)
            if took is None:
                # 02 §6：症状没出现就不写用例，防止"故障注入了，但症状没出现"的空用例
                raise BenchError(f"{scenario_id} r{k}：{spec.symptom_wait.timeout_s} 秒内症状没有出现，这条录制作废")
            took = round(time.monotonic() - t0, 1)
            tb = open_backend(pack, f"inv_rec_{ULID()}", Mode.RECORD, ObservationStore(SCRATCH), fixture)
            try:
                sweep(pack, tb)
            finally:
                tb.close()
        finally:
            revert_all(spec)
        wait_green(pack)
        ex = EvalExample(
            id=f"{scenario_id}#r{k}",
            scenario_id=scenario_id,
            split=split_for(scenario_id, spec.origin),
            inputs=ExampleInputs(
                trigger={"kind": "patrol", "target": TARGET}, fixture_ref=str(rel), memory_snapshot=None
            ),
            outputs=ExampleOutputs(**spec.ground_truth.model_dump()),
            metadata=ExampleMetadata(
                pack=pack_name,
                difficulty=spec.difficulty,
                target_version=version,
                recorded_at=datetime.now(UTC),
                stream_id=None,
                stream_pos=None,
                origin=spec.origin,
                source_ref=spec.source_ref,
            ),
        )
        append_example(pack_name, ex)
        write_fixture_index(
            {
                "example_id": ex.id,
                "fixture": str(rel),
                "symptom_after_s": took,
                "timeout_s": spec.symptom_wait.timeout_s,
                "target_version": version,
                "recorded_at": ex.metadata.recorded_at.isoformat(),
                "sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
                "bytes": fixture.stat().st_size,
            }
        )
        log(f"{ex.id}：症状在 {took} 秒后出现，录制 {fixture.stat().st_size // 1024} KB")
        made.append(ex.id)
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return made
