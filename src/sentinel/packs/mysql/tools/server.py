"""MySQL 只读工具（M4 D13）：全局状态、全局变量、会话列表、InnoDB 事务。

连接是 sdk.connect 按 connection.kind=mysql 建的只读连接，只有 query 方法（ReadOnlySql）。
会话列表和事务都**不取 SQL 原文**：原文里可能有业务数据（04 §3.8 数据外流），诊断用不到。
"""

from typing import Any

from sentinel.sdk import Args, ReadTool

STATUS_KEYS = (
    "Threads_connected",
    "Threads_running",
    "Max_used_connections",
    "Connection_errors_max_connections",
    "Aborted_connects",
    "Innodb_row_lock_current_waits",
    "Innodb_row_lock_waits",
    "Uptime",
)
VARIABLE_KEYS = ("max_connections", "read_only", "super_read_only", "innodb_lock_wait_timeout", "wait_timeout")


def _kv(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {r["Variable_name"]: r["Value"] for r in rows}


def _in(keys: tuple[str, ...]) -> str:
    return ", ".join(f"'{k}'" for k in keys)  # 常量列表，不来自外部输入


global_status = ReadTool(
    description="全局状态计数器：当前连接数、运行中的线程、历史最大连接数、因为连接数满而被拒绝的次数、行锁等待。",
    fetch=lambda conn, args: _kv(conn.query(f"SHOW GLOBAL STATUS WHERE Variable_name IN ({_in(STATUS_KEYS)})")),
    summarize=lambda raw, args: " ".join(f"{k}={raw.get(k)}" for k in STATUS_KEYS),
)

global_variables = ReadTool(
    description="关键的全局变量：max_connections、read_only、super_read_only、锁等待超时。看配置有没有被改过。",
    fetch=lambda conn, args: _kv(conn.query(f"SHOW GLOBAL VARIABLES WHERE Variable_name IN ({_in(VARIABLE_KEYS)})")),
    summarize=lambda raw, args: " ".join(f"{k}={raw.get(k)}" for k in VARIABLE_KEYS),
)


def _processlist_summary(raw: Any, args: Args) -> str:
    by: dict[tuple[str, str], int] = {}
    for r in raw:
        k = (str(r["user"]), str(r["command"]))
        by[k] = by.get(k, 0) + 1
    longest = max((int(r["time"] or 0) for r in raw), default=0)
    parts = [f"{u}/{c}={n}" for (u, c), n in sorted(by.items(), key=lambda kv: -kv[1])]
    return f"sessions={len(raw)} longest_s={longest} by_user_command: " + " ".join(parts)


processlist = ReadTool(
    description="当前所有会话：按用户和命令类型（Sleep、Query……）计数，以及最长的会话时长。不含 SQL 原文。",
    fetch=lambda conn, args: conn.query(
        "SELECT id, user, command, time, state FROM performance_schema.processlist ORDER BY time DESC"
    ),
    summarize=_processlist_summary,
)


def _trx_summary(raw: Any, args: Args) -> str:
    if not raw:
        return "没有进行中的 InnoDB 事务"
    lines = [f"transactions={len(raw)} lock_wait={sum(1 for r in raw if r['trx_state'] == 'LOCK WAIT')}"]
    for r in raw[:10]:
        lines.append(
            f"thread={r['trx_mysql_thread_id']} state={r['trx_state']} age_s={r['age_s']} "
            f"wait_s={r['wait_s']} rows_locked={r['trx_rows_locked']}"
        )
    return "\n".join(lines)


innodb_trx = ReadTool(
    description="进行中的 InnoDB 事务：状态（RUNNING / LOCK WAIT）、开了多久、等锁等了多久、锁了多少行。"
    "看长事务和锁等待。",
    fetch=lambda conn, args: conn.query(
        "SELECT trx_mysql_thread_id, trx_state, TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_s, "
        "IFNULL(TIMESTAMPDIFF(SECOND, trx_wait_started, NOW()), 0) AS wait_s, trx_rows_locked "
        "FROM information_schema.innodb_trx ORDER BY trx_started"
    ),
    summarize=_trx_summary,
)


def _lock_waits_summary(raw: Any, args: Args) -> str:
    if not raw:
        return "没有锁等待"
    return "\n".join(
        f"waiting_thread={r['waiting_thread']} blocking_thread={r['blocking_thread']} "
        f"object={r['object_schema']}.{r['object_name']} lock_type={r['lock_type']}"
        for r in raw[:10]
    )


lock_waits = ReadTool(
    description="锁等待关系：哪个会话在等哪个会话持有的锁、锁的是哪张表。找到阻塞源头。",
    fetch=lambda conn, args: conn.query(
        "SELECT w.REQUESTING_THREAD_ID AS waiting_thread, w.BLOCKING_THREAD_ID AS blocking_thread, "
        "l.OBJECT_SCHEMA AS object_schema, l.OBJECT_NAME AS object_name, l.LOCK_TYPE AS lock_type "
        "FROM performance_schema.data_lock_waits w "
        "JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    ),
    summarize=_lock_waits_summary,
)
