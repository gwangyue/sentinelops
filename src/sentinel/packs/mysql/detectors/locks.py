"""mysql.lock_waits：有事务等行锁超过阈值（写入卡住，常见原因是某个事务一直不提交）。"""

from sentinel.contracts import Finding
from sentinel.packs.mysql.thresholds import LOCK_WAIT_S
from sentinel.sdk import DetectContext, make_finding

ID = "mysql.lock_waits"


def detect(ctx: DetectContext) -> list[Finding]:
    trx = ctx.tools.call("mysql.innodb_trx")
    if not trx.ok:
        return []
    waiting = [r for r in trx.raw if r["trx_state"] == "LOCK WAIT" and int(r["wait_s"]) >= LOCK_WAIT_S]
    if not waiting:
        return []
    longest = max(int(r["wait_s"]) for r in waiting)
    return [
        make_finding(
            ctx,
            ID,
            severity="warn",
            summary=f"{len(waiting)} 个事务在等行锁，最长等了 {longest} 秒",
            metrics={"lock_waiting_trx": float(len(waiting)), "longest_wait_s": float(longest)},
            observation_ids=[trx.observation_id],
        )
    ]
