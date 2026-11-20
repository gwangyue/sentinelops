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

MILESTONES = ["none", "M1", "M2", "M3", "M4", "M5"]
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
    if len(keys) != len(set(keys)) or out.get("remediation") != "resolved":
        return Status.FAIL, f"幂等键重复或没有解决：{keys} {out.get('status')}"
    return Status.PASS, ""


def _confirmed():  # type: ignore[no-untyped-def]
    """第 6 步走完之后，人确认 H1，写入记忆。返回 (harness, 调查 ID, 恢复之后的状态)。"""
    h, s = _remediation_run()
    h.decide("approved")
    h.resume(s["inv_id"])
    h.confirm(s["inv_id"])
    return h, s["inv_id"], h.resume(s["inv_id"])


def step7_confirm_memorize() -> tuple[Status, str]:
    """审计中有 rootcause.confirmed → memory.written；
    记忆的 source_investigations 包含本次调查（golden-demo 第 7 步）。"""
    h, inv, out = _confirmed()
    types = h.audit_types(inv)
    if "rootcause.confirmed" not in types or "memory.written" not in types:
        return Status.FAIL, f"审计里缺少事件：{types}"
    if types.index("rootcause.confirmed") > types.index("memory.written"):
        return Status.FAIL, "memory.written 出现在 rootcause.confirmed 之前"
    m = h.mem_index.get(str(out.get("memory_id")))
    if m is None or inv not in m.record.source_investigations:
        return Status.FAIL, f"记忆不存在，或者 source_investigations 不包含 {inv}"
    return Status.PASS, ""


def step8_memory_reused() -> tuple[Status, str]:
    """再次遇到同类故障：第二次调查的 L5 包含 mem_x；H1 的 from_memory 包含 mem_x（不检查步数）。
    用的是打开 L5 的演示配置（M-E1 之后 L5 没有上线，第 8 步按人的决定用演示版本，见 version_decisions.yaml）。"""
    import re
    import tempfile

    from tests.flows.scripts import diagnose_script
    from tests.harness import Harness
    from tests.unit.test_memory import fake_embed

    from sentinel.core.memory.policy import load_memory_policy
    from sentinel.core.memory.provider import StoreMemoryProvider
    from sentinel.core.versioning.components import load_context_policy

    h1, _, out = _confirmed()
    mem_x = str(out.get("memory_id"))
    policy = load_context_policy()
    policy = {**policy, "layers": {**policy["layers"], "L5": True}}
    base = diagnose_script()
    seen: dict[str, str] = {}

    def script(messages):  # type: ignore[no-untyped-def]
        msg = base(messages)
        ids = re.findall(r'<memory id="(mem_[0-9A-Z]+)"', "\n".join(str(m.content) for m in messages))
        if ids and msg.tool_calls and msg.tool_calls[0]["name"] == "DiagnosisOutput":
            msg.tool_calls[0]["args"]["hypotheses"][0]["from_memory"] = [ids[0]]
            seen["ref"] = ids[0]
        return msg

    h2 = Harness(Path(tempfile.mkdtemp()), "disk_watermark", script, policy)  # 新的存储：避开 30 分钟去重
    h2.deps.memory = StoreMemoryProvider(h1.mem_index, None, load_memory_policy(), fake_embed)
    s2 = h2.run()
    l5 = [i["ref"] for i in s2["bundle"]["items"] if i["layer"] == "L5" and i["included"]]
    if mem_x not in l5:
        return Status.FAIL, f"第二次调查的 L5 不包含 {mem_x}：{l5}"
    if mem_x not in s2["diagnosis"]["hypotheses"][0]["from_memory"]:
        return Status.FAIL, "H1 的 from_memory 不包含 mem_x"
    return Status.PASS, ""


def step10_report_and_audit() -> tuple[Status, str]:
    """周报的每个数字都能用 SQL 从审计表里重新算出来；audit verify 返回 0（golden-demo 第 10 步）。"""
    from datetime import UTC, datetime, timedelta

    from sentinel.governance.audit import verify_chain
    from sentinel.governance.report import audit_numbers, sqlite_runner

    h, _, _ = _confirmed()
    ev = h.store.audit_events()
    now = datetime.now(UTC)
    a = audit_numbers(sqlite_runner(ev), now - timedelta(days=1), now + timedelta(days=1))
    expect = {
        "investigations": sum(1 for e in ev if e.type == "investigation.started"),
        "executed": sum(1 for e in ev if e.type == "action.executed" and e.payload.get("status") == "succeeded"),
        "verify_passed": sum(1 for e in ev if e.type == "verify.passed"),
        "confirmed": sum(1 for e in ev if e.type == "rootcause.confirmed"),
        "diagnoses": sum(1 for e in ev if e.type == "diagnosis.completed"),
    }
    got = {k: getattr(a, k) for k in expect}
    if got != expect:
        return Status.FAIL, f"SQL 算出的 {got} 和直接数事件的 {expect} 不一致"
    if problems := verify_chain(ev):
        return Status.FAIL, f"审计链断了：{problems[0]}"
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


def step11_feishu_approval() -> tuple[Status, str]:
    """回放 + 假飞书（golden-demo 第 11 步）：approval.requested → 审批卡片（内容和 approvals show 一致）→
    白名单外的点击被拒绝 → 审批人点「批准」→ approval.decided（human:<名字>，reason 以 [feishu] 开头）
    → action.executed → verify.passed → 推送结果卡片。"""
    import tempfile

    from tests.flows.test_remediation import GOOD, fixture, with_plan
    from tests.harness import Harness

    from sentinel.chatops.approvals import CHANNEL, ApprovalHandler, CardAction
    from sentinel.chatops.config import ChatopsConfig
    from sentinel.chatops.fake import FakeFeishuClient
    from sentinel.chatops.notifier import Notifier
    from sentinel.core.approval_view import load_approval_view

    tmp = Path(tempfile.mkdtemp())
    h = Harness(tmp, fixture(tmp), with_plan(GOOD), remediate=True)
    cfg = ChatopsConfig(env_label="kind-dev", chat_id="oc_demo", approvers={"ou_approver": "approver"})
    feishu = FakeFeishuClient()
    n = Notifier(h.store, h.rem_store, feishu, cfg)
    n.tick()  # 初始化游标：只推之后发生的事
    s = h.run("remediate")
    n.tick()
    cards = [c for _, c, _ in feishu.sent if "待审批" in c["header"]["title"]["content"]]
    if not cards or not s.get("approval_id"):
        return Status.FAIL, f"没有推送审批卡片：{[c['header']['title']['content'] for _, c, _ in feishu.sent]}"
    title = cards[0]["header"]["title"]["content"]
    text = "\n".join(e.get("content", "") for e in cards[0]["elements"])  # 卡片里的 markdown 正文
    v = load_approval_view(h.rem_store, s["approval_id"])
    assert v is not None
    if not title.startswith("[kind-dev]") or any(val not in text for _, val in v.lines()):
        return Status.FAIL, "审批卡片缺环境标签，或者内容和 approvals show 不一致"
    gw = ApprovalHandler(h.rem_store, h.store.append_audit, cfg, resume=h.resume, spawn=lambda fn: fn())
    value = {"approval_id": s["approval_id"], "decision": "approved"}
    if gw.on_card_action(CardAction("ou_stranger", "oc_demo", "om_0", value)).toast_type != "error":
        return Status.FAIL, "白名单外的点击没有被拒绝"
    reply = gw.on_card_action(CardAction("ou_approver", "oc_demo", "om_0", value))
    if reply.card is None or "执行中" not in str(reply.card):
        return Status.FAIL, f"点击之后卡片没有变成「已批准，执行中」：{reply.toast}"
    ev = h.store.audit_events(s["inv_id"])
    types = [e.type for e in ev]
    try:
        ok = types.index("approval.decided") < types.index("action.executed") < types.index("verify.passed")
    except ValueError:
        return Status.FAIL, f"审计里缺少事件：{types}"
    decided = [e for e in ev if e.type == "approval.decided"]
    if not ok or len(decided) != 1:
        return Status.FAIL, f"顺序不对或者决定了不止一次：{types}"
    d = decided[0]
    if d.actor != "human:approver" or not str(d.payload.get("reason", "")).startswith(CHANNEL):
        return Status.FAIL, f"approval.decided 的 actor/reason 不对：{d.actor} {d.payload.get('reason')}"
    n.tick()
    if not any("验证通过" in c["header"]["title"]["content"] for _, c, _ in feishu.sent):
        return Status.FAIL, "没有推送验证通过的卡片"
    return Status.PASS, ""


SCENARIO = Path(__file__).resolve().parents[1] / "src/sentinel/packs/es/scenarios/disk_watermark.single.yaml"

# 步骤编号 → 检查函数。实现某一步的那天，在这里换成真正的检查。
CHECKS: dict[int, Callable[[], tuple[Status, str]]] = {n: _not_implemented for n in range(1, 12)}
CHECKS[1] = step1_patrol_healthy
CHECKS[2] = step2_inject
CHECKS[3] = step3_diagnosis
CHECKS[4] = step4_context_report
CHECKS[5] = step5_plan
CHECKS[6] = step6_approve_execute_verify
CHECKS[7] = step7_confirm_memorize
CHECKS[8] = step8_memory_reused
CHECKS[9] = step9_curve_and_ablation
CHECKS[10] = step10_report_and_audit
CHECKS[11] = step11_feishu_approval


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
