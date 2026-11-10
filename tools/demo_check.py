"""`make demo-check`：golden-demo.md 的机器检查（G6）。

负责：按 tools/demo_steps.yaml 列出 10 个步骤，执行每一步的检查函数，打印状态表；
      只有"已打开"的步骤失败或未实现时才返回非零。
不负责：检查的具体内容——每个检查函数在对应功能完成的那天实现（D1 全部是"未实现"）。
检查只看契约和关系，不看具体数值（golden-demo.md 规则 3）。
"""

import argparse
import sys
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

import yaml

MILESTONES = ["none", "M1", "M2", "M3", "M4"]
STEPS_FILE = Path(__file__).with_name("demo_steps.yaml")


class Status(StrEnum):
    PASS = "通过"
    FAIL = "失败"
    TODO = "未实现"


def _not_implemented() -> tuple[Status, str]:
    return Status.TODO, ""


def _harness(fixture: str, script=None):  # type: ignore[no-untyped-def]
    # 回放模式 + 假 LLM（golden-demo 规则 1）；和 tests/flows 用同一套组装
    import tempfile

    from tests.harness import Harness

    return Harness(Path(tempfile.mkdtemp()), fixture, script)


def step1_patrol_healthy() -> tuple[Status, str]:
    """审计里有 patrol.healthy；这次巡检没有任何模型调用。"""
    h = _harness("healthy")
    s = h.run()
    types = h.audit_types(s["inv_id"])
    if "patrol.healthy" not in types:
        return Status.FAIL, f"审计里没有 patrol.healthy：{types}"
    if h.model.calls:
        return Status.FAIL, f"健康巡检调用了 {h.model.calls} 次模型"
    return Status.PASS, ""


def step3_diagnosis() -> tuple[Status, str]:
    """诊断通过 schema 校验；引用的观测 ID 存在且属于本次调查；引文是摘要的子串；H1 标签等于场景真实答案。"""
    from tests.flows.scripts import diagnose_script

    from sentinel.contracts import Diagnosis, ScenarioSpec
    from sentinel.core.diagnosis.evidence import check_evidence

    truth = ScenarioSpec.model_validate(yaml.safe_load(SCENARIO.read_text(encoding="utf-8"))).ground_truth
    h = _harness("disk_watermark", diagnose_script())
    s = h.run()
    d = Diagnosis.model_validate(s["diagnosis"])  # schema 校验
    bad = [c for c in check_evidence(d, h.obs.list(s["inv_id"])) if not c.supported]
    if bad:
        return Status.FAIL, f"证据检查不通过：{bad[0].reason}"
    if not d.hypotheses or d.hypotheses[0].root_cause_label != truth.root_cause_label:
        return Status.FAIL, "H1 的根因标签和场景真实答案不一致"
    return Status.PASS, ""


def step2_inject() -> tuple[Status, str]:
    """检测器在超时内触发。注入本身要在线做（sentinel bench inject），这里检查最近一次录制留下的记录。"""
    from sentinel.eval.dataset import read_fixture_index

    rows = [r for r in read_fixture_index() if r["example_id"].startswith("es.disk_watermark.single#")]
    if not rows:
        return Status.FAIL, "fixtures/index.jsonl 里没有 es.disk_watermark.single 的录制"
    last = rows[-1]
    if last["symptom_after_s"] > last["timeout_s"]:
        return Status.FAIL, f"症状在 {last['symptom_after_s']} 秒后才出现，超过 {last['timeout_s']} 秒"
    return Status.PASS, f"（{last['example_id']}：{last['symptom_after_s']} 秒）"


def step4_context_report() -> tuple[Status, str]:
    """ContextBundle 能通过 schema 校验；L0 和异常事实没有被删除；各层 token 总数不超过预算（golden-demo 第 4 步）。"""
    from tests.flows.scripts import diagnose_script

    from sentinel.contracts import ContextBundle
    from sentinel.core.versioning.components import load_context_policy

    h = _harness("disk_watermark", diagnose_script())
    s = h.run()
    b = ContextBundle.model_validate(s["bundle"])
    if any(not i.included for i in b.items if i.layer == "L0"):
        return Status.FAIL, "L0 问题事件被删除了"
    abnormal = {x["observation_id"] for fs in s["fact_sheets"] for x in fs["facts"] if x["status"] == "abnormal"}
    if any(not i.included for i in b.items if i.layer == "L1" and i.ref in abnormal):
        return Status.FAIL, "异常事实被删除了"
    budgets = load_context_policy()["budgets"]
    over = [k for k, v in b.tokens_by_layer.items() if v > budgets[k]]
    if over or sum(b.tokens_by_layer.values()) > budgets["preassembled_total"]:
        return Status.FAIL, f"超出预算：{over or '总量'}"
    return Status.PASS, ""


SCENARIO = Path(__file__).resolve().parents[1] / "src/sentinel/packs/es/scenarios/disk_watermark.single.yaml"

# 步骤编号 → 检查函数。实现某一步的那天，在这里换成真正的检查。
CHECKS: dict[int, Callable[[], tuple[Status, str]]] = {n: _not_implemented for n in range(1, 11)}
CHECKS[1] = step1_patrol_healthy
CHECKS[2] = step2_inject
CHECKS[3] = step3_diagnosis
CHECKS[4] = step4_context_report


def run(force_all: bool) -> int:
    cfg = yaml.safe_load(STEPS_FILE.read_text(encoding="utf-8"))
    opened_idx = MILESTONES.index(cfg["opened_through"])
    bad = 0
    print(f"golden-demo 检查（已打开到：{cfg['opened_through']}）")
    for s in cfg["steps"]:
        opened = MILESTONES.index(s["open_at"]) <= opened_idx
        if not (opened or force_all):
            print(f"  {s['n']:>2}. {s['title']:<16} [未打开 · {s['open_at']}]")
            continue
        status, detail = CHECKS[s["n"]]()
        if opened and status is not Status.PASS:
            bad += 1
        tag = "已打开" if opened else f"未打开 · {s['open_at']}"
        print(f"  {s['n']:>2}. {s['title']:<16} [{tag}] {status} {detail}".rstrip())
    print(f"已打开的步骤中未通过：{bad}")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="未打开的步骤也执行检查，用来看进度")
    return run(ap.parse_args(argv).all)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
