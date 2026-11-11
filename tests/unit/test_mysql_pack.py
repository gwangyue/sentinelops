"""MySQL 专家包（M4 D13）：检测器、事实卡片、动作的前置检查和回滚、只读连接。离线：假的工具输出。"""

from typing import Any

from sentinel.core.packs_registry import load_pack
from sentinel.packs.mysql.actions import mysql_actions as A
from sentinel.packs.mysql.detectors import connections, locks, read_only
from sentinel.packs.mysql.facts import instance_facts
from sentinel.sdk import ActionContext, DetectContext, ExecContext
from tests.unit.test_actions import FakeTools

STATUS = {"Threads_connected": "57", "Connection_errors_max_connections": "3"}
VARS = {"max_connections": "60", "read_only": "OFF"}


def tools(**over: Any) -> FakeTools:
    base = {"mysql.global_status": STATUS, "mysql.global_variables": VARS, "mysql.innodb_trx": []}
    return FakeTools(base | {k.replace("_", ".", 1): v for k, v in over.items()})


def ctx(t: FakeTools, env: dict[str, Any] | None = None) -> DetectContext:
    return DetectContext(pack="mysql", target="mysql-target", tools=t, env=env or {"role": "primary"})


class FakeSql:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, sql: str) -> dict[str, Any]:
        self.sql.append(sql)
        return {"ok": True}


def test_pack_loads_with_one_action_and_entries_resolve() -> None:
    p = load_pack("mysql")
    assert (
        len(p.tools) == 5 and len(p.detectors) == 3 and list(p.actions) == ["mysql.raise_max_connections_temporarily"]
    )
    assert p.manifest.connection.kind == "mysql"


def test_connection_usage_detector_and_facts() -> None:
    f = connections.detect(ctx(tools()))
    assert len(f) == 1 and f[0].severity == "warn" and f[0].metrics["connection_usage"] == 0.95
    assert connections.detect(ctx(tools(mysql_global_status={**STATUS, "Threads_connected": "20"}))) == []
    sheet = instance_facts(ctx(tools()))
    by = {x.key: x for x in sheet.facts}
    assert by["instance.connection_usage_pct"].status == "abnormal" and by["instance.max_connections"].value == 60.0


def test_lock_wait_and_read_only_detectors() -> None:
    trx = [
        {"trx_state": "RUNNING", "age_s": 300, "wait_s": 0, "trx_rows_locked": 1, "trx_mysql_thread_id": 9},
        {"trx_state": "LOCK WAIT", "age_s": 40, "wait_s": 40, "trx_rows_locked": 0, "trx_mysql_thread_id": 10},
    ]
    assert locks.detect(ctx(tools(mysql_innodb_trx=trx)))[0].metrics["longest_wait_s"] == 40
    ro = {**VARS, "read_only": "ON"}
    assert read_only.detect(ctx(tools(mysql_global_variables=ro)))[0].severity == "critical"
    assert read_only.detect(ctx(tools(mysql_global_variables=ro), {"role": "replica"})) == []  # 从库只读是正常的


def test_raise_max_connections_bounds_execute_and_rollback() -> None:
    def c(n: int) -> ActionContext:
        return ActionContext("mysql-target", {"max_connections": n}, tools())

    pre = A.raise_max_connections_precheck(c(100))
    assert pre.ok and pre.snapshot["before"] == 60
    assert not A.raise_max_connections_precheck(c(130)).ok  # 超过 2 倍
    assert not A.raise_max_connections_precheck(c(60)).ok  # 没有提高
    w = FakeSql()
    A.raise_max_connections_execute(ExecContext("mysql-target", {"max_connections": 100}, pre.snapshot, w))
    A.raise_max_connections_rollback(ExecContext("mysql-target", {"max_connections": 100}, pre.snapshot, w))
    assert w.sql == ["SET GLOBAL max_connections = 100", "SET GLOBAL max_connections = 60"]


def test_mysql_read_connection_has_no_write_methods() -> None:
    from sentinel.sdk.sql import ReadOnlySql

    for m in ("execute", "executemany", "commit", "put", "post"):
        assert not hasattr(ReadOnlySql, m)
