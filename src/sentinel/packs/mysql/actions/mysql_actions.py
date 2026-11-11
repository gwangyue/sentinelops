"""MySQL 的修复动作（M4 D13；03 §4.2）：临时提高 max_connections。规格在同目录的 <动作 ID>.yaml。

前置检查、预演、验证只用只读工具（回放也能跑）；执行、回滚只用执行器传进来的写连接（ctx.conn，有 execute 方法）。
用 SET GLOBAL 而不是 SET PERSIST：重启之后恢复成持久化的值，"临时"就是字面意思；回滚按前置检查快照恢复原值。
"""

from typing import Any

from sentinel.packs.mysql.thresholds import CONN_WARN, usage
from sentinel.sdk import ActionContext, CheckResult, ExecContext, ToolCaller, VerifyResult


def _raw(tools: ToolCaller, tool: str) -> Any:
    r = tools.call(tool)
    if not r.ok:
        raise RuntimeError(f"{tool} 调用失败（{r.status}）：{r.digest}")
    return r.raw


def raise_max_connections_precheck(ctx: ActionContext) -> CheckResult:
    st, var = _raw(ctx.tools, "mysql.global_status"), _raw(ctx.tools, "mysql.global_variables")
    cur, want = int(var["max_connections"]), int(ctx.params["max_connections"])
    u = usage(st, var)
    snap = {"before": cur, "threads_connected": int(st["Threads_connected"])}
    if u < CONN_WARN:
        return CheckResult(False, f"连接使用率 {u:.0%}，没有接近上限", snap)
    if not cur < want <= cur * 2:
        return CheckResult(False, f"新的上限 {want} 必须大于当前的 {cur}、最多是它的 2 倍（{cur * 2}）", snap)
    return CheckResult(True, f"连接 {st['Threads_connected']}/{cur}（{u:.0%}），临时提高到 {want}", snap)


def raise_max_connections_dry_run(ctx: ActionContext) -> dict[str, Any]:
    var = _raw(ctx.tools, "mysql.global_variables")
    return {
        "settings_changed": 1,
        "extra_connections": int(ctx.params["max_connections"]) - int(var["max_connections"]),
    }


def raise_max_connections_execute(ctx: ExecContext) -> dict[str, Any]:
    n = int(ctx.params["max_connections"])  # 已经过 params_schema 校验是整数，拼进 SQL 是安全的
    return dict(ctx.conn.execute(f"SET GLOBAL max_connections = {n}"))


def raise_max_connections_rollback(ctx: ExecContext) -> dict[str, Any]:
    n = int(ctx.precheck_snapshot["before"])
    return dict(ctx.conn.execute(f"SET GLOBAL max_connections = {n}"))


def connection_usage_verify(ctx: ActionContext) -> VerifyResult:
    st, var = _raw(ctx.tools, "mysql.global_status"), _raw(ctx.tools, "mysql.global_variables")
    u = usage(st, var)
    return VerifyResult(u < CONN_WARN, f"连接 {st['Threads_connected']}/{var['max_connections']}（{u:.0%}）")
