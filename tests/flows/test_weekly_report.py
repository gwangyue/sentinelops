"""治理周报（07 §8；golden-demo 第 10 步）：周报里的每个数字都能用 SQL 从审计表里重新算出来。

用流程测试的组装真跑几次调查产生审计事件，装进 sqlite，用周报的 SQL 算，再和直接数事件的结果比对。
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sentinel.contracts import AuditEvent
from sentinel.governance.kill_switch import freeze, unfreeze
from sentinel.governance.report import audit_numbers, sqlite_runner
from tests.flows.test_remediation import GOOD, RAISE, approve, fixture, with_plan
from tests.harness import Harness

H = Any


def events_from_runs(harness: H, tmp_path: Path) -> list[AuditEvent]:
    # 一次修好并确认；一次不安全提议；一次审批被拒绝；一次冻结和解冻
    h: Harness = harness(fixture(tmp_path), with_plan(GOOD), remediate=True)
    s = h.run("remediate")
    approve(h)
    h.resume(s["inv_id"])
    h.confirm(s["inv_id"])
    h.resume(s["inv_id"])
    bad = json.dumps({"action_id": "es.delete_index", "params": {}, "rationale_hypothesis": "H1"})
    h2: Harness = harness(fixture(tmp_path), with_plan(bad), remediate=True)
    h2.run("remediate")
    h3: Harness = harness(fixture(tmp_path), with_plan(GOOD), remediate=True)
    s3 = h3.run("remediate")
    approve(h3, "rejected")
    h3.resume(s3["inv_id"])
    freeze(h3.freeze_state, h3.rem_store, "演练", "human:ops")
    unfreeze(h3.freeze_state, h3.rem_store, "结束", "human:approver")
    return [*h.store.audit_events(), *h2.store.audit_events(), *h3.store.audit_events()]


def test_every_number_recomputes_from_audit_with_sql(harness: H, tmp_path: Path) -> None:
    ev = events_from_runs(harness, tmp_path)
    now = datetime.now(UTC)
    a = audit_numbers(sqlite_runner(ev), now - timedelta(days=1), now + timedelta(days=1))

    def count(t: str, **payload: Any) -> int:
        return sum(1 for e in ev if e.type == t and all(e.payload.get(k) == v for k, v in payload.items()))

    assert a.investigations == count("investigation.started") == 3
    assert a.executed == count("action.executed", status="succeeded") == 1
    assert a.verify_passed == count("verify.passed") == 1
    assert a.unsafe == count("plan.rejected_unsafe") == 1
    assert len(a.approvals_rejected) == count("approval.decided", decision="rejected") == 1
    assert a.freezes == count("freeze.enabled") == 1
    assert a.resolved == count("investigation.closed", outcome="resolved") == 1
    assert a.confirmed == count("rootcause.confirmed") == 1 and a.corrected == 0
    assert a.diagnoses == count("diagnosis.completed") == 3
    assert a.tokens_in == sum(e.payload["tokens_in"] for e in ev if e.type == "diagnosis.completed")
    # 执行成功、没有回滚的临时修复会列进"需要你决定的事"
    assert [x[2] for x in a.temporary_fixes] == [RAISE]
    assert a.approvals_by_action[RAISE] == {"approved": 1, "rejected": 1}


def test_window_excludes_events_outside_the_week(harness: H, tmp_path: Path) -> None:
    ev = events_from_runs(harness, tmp_path)
    later = datetime.now(UTC) + timedelta(days=30)
    a = audit_numbers(sqlite_runner(ev), later, later + timedelta(days=7))
    assert a.investigations == 0 and a.executed == 0 and a.unsafe == 0
    assert len(a.temporary_fixes) == 1  # 临时修复不限开始时间：之前执行、到现在还没撤销的也要列出来
