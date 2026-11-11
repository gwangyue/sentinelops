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


def _remediation_run():  # type: ignore[no-untyped-def]
    """修复模式跑到审批暂停：disk_watermark 录制 + 一条 post 记录（执行 raise_watermark 之后变绿）。
    假模型提议提高水位。"""
    import tempfile

    from tests.flows.test_remediation import GOOD, fixture, with_plan
    from tests.harness import Harness

    tmp = Path(tempfile.mkdtemp())
    h = Harness(tmp, fixture(tmp), with_plan(GOOD), remediate=True)
    return h, h.run("remediate")


def step5_plan() -> tuple[Status, str]:
    """动作在白名单里；参数能通过 params_schema；plan_hash 能由确定性代码重新算出来（golden-demo 第 5 步）。"""
    import jsonschema

    from sentinel.contracts import RemediationPlan
    from sentinel.core.packs_registry import load_pack
    from sentinel.governance.plan_hash import plan_hash

    _, s = _remediation_run()
    if not s.get("plan"):
        return Status.FAIL, f"没有修复方案：{s.get('remediation')}"
    pl = RemediationPlan.model_validate(s["plan"])
    specs = load_pack("es").actions
    if pl.action_id not in specs:
        return Status.FAIL, f"{pl.action_id} 不在动作目录里"
    try:
        jsonschema.validate(pl.params, specs[pl.action_id].params_schema)
    except jsonschema.ValidationError as e:
        return Status.FAIL, f"参数通不过 params_schema：{e.message}"
    if plan_hash(pl.action_id, pl.target, pl.params, pl.precheck_snapshot) != pl.plan_hash:
        return Status.FAIL, "plan_hash 不能由确定性代码重新算出来"
    return Status.PASS, ""


def step6_approve_execute_verify() -> tuple[Status, str]:
    """审计中依次出现 approval.decided → action.executed → verify.passed；
    执行器记录的 plan_hash 和审批时一致；幂等键唯一（golden-demo 第 6 步）。"""
    h, s = _remediation_run()
    h.decide("approved")
    out = h.resume(s["inv_id"])
    ev = h.store.audit_events(s["inv_id"])
    types = [e.type for e in ev]
    try:
        ok = types.index("approval.decided") < types.index("action.executed") < types.index("verify.passed")
    except ValueError:
        return Status.FAIL, f"审计里缺少事件：{types}"
    if not ok:
        return Status.FAIL, f"顺序不对：{types}"
    decided = next(e for e in ev if e.type == "approval.decided").payload["plan_hash"]
    executed = next(e for e in ev if e.type == "action.executed").payload["plan_hash"]
    if decided != executed:
        return Status.FAIL, "执行器记录的 plan_hash 和审批时的不一致"
    keys = list(h.rem_store.executions)
    if len(keys) != len(set(keys)) or out.get("status") != "resolved":
        return Status.FAIL, f"幂等键重复或没有解决：{keys} {out.get('status')}"
    return Status.PASS, ""


def step9_curve_and_ablation() -> tuple[Status, str]:
    """曲线上的每个点都有对应的版本和数据集标签；消融结果包含增益、伤害率、区间 3 项（离线、用合成结果）。"""
    import tempfile

    from tests.contracts.factories import all_samples

    from sentinel.eval import results
    from sentinel.eval.curve import points
    from sentinel.eval.stats.bootstrap import compare
    from sentinel.eval.stream import memory_summary

    tmp = Path(tempfile.mkdtemp())
    base = all_samples()["run_score"]
    a = [base.model_copy(update={"example_id": f"s{i}.x#r1", "steps": 4}) for i in range(4)]  # type: ignore[attr-defined]
    b = [x.model_copy(update={"steps": 3, "memory_hit": True}) for x in a]
    results.save(tmp / "v1__ds-20261111-1__dev.jsonl", a)
    pts = points(tmp, {1: "note"}, {})
    if not pts or any(not (p.seq and p.tag) for p in pts):
        return Status.FAIL, "曲线的点缺少版本或数据集标签"
    c = compare(a, b, metric="steps")
    side = tmp / "x.memory.jsonl"
    side.write_text('{"truth": "t", "l5": ["t"], "harm": false}\n' * 4, encoding="utf-8")
    summary = memory_summary(side, b)
    if not ("伤害率" in summary and c.low <= c.point <= c.high):
        return Status.FAIL, f"消融结果缺少增益、伤害率或区间：{summary}"
    return Status.PASS, ""


SCENARIO = Path(__file__).resolve().parents[1] / "src/sentinel/packs/es/scenarios/disk_watermark.single.yaml"

# 步骤编号 → 检查函数。实现某一步的那天，在这里换成真正的检查。
CHECKS: dict[int, Callable[[], tuple[Status, str]]] = {n: _not_implemented for n in range(1, 11)}
CHECKS[1] = step1_patrol_healthy
CHECKS[2] = step2_inject
CHECKS[3] = step3_diagnosis
CHECKS[4] = step4_context_report
CHECKS[5] = step5_plan
CHECKS[6] = step6_approve_execute_verify
CHECKS[9] = step9_curve_and_ablation


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
