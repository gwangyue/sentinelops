"""执行器进程入口：`python -m sentinel.exec`（ADR-005：独立进程、单独持有写凭证）。

负责：轮询 Postgres 里的 execution_requests，逐个交给 core.process。
不负责：任何校验（core.py）；生产部署时的独立命名空间和网络策略（M4 的 Helm chart）。
"""

import argparse
import os
import sys
import time

from sentinel.exec.core import live_runner, process
from sentinel.exec.live import ExecTools, WriteHttpClient, load_specs, manifest, write_connection
from sentinel.governance.kill_switch import PgFreezeState
from sentinel.governance.remediation_store import PgRemediationStore
from sentinel.sdk import ReadOnlyHttp


def main() -> int:
    ap = argparse.ArgumentParser(description="SentinelOps 执行器")
    ap.add_argument("--pack", default="es")
    ap.add_argument("--once", action="store_true", help="处理完当前队列就退出（测试和录制用）")
    ap.add_argument("--poll", type=float, default=2.0)
    a = ap.parse_args()
    m = manifest(a.pack)
    url, user, pw = write_connection(m)
    specs = load_specs(m)
    store = PgRemediationStore(
        os.environ.get("SENTINEL_PG_URL", "postgresql://postgres:sentinel-local@localhost:5432/sentinel")
    )
    run = live_runner(lambda: WriteHttpClient(url, user, pw))
    freeze_state = PgFreezeState(store.url)
    print(f"执行器已启动：{a.pack}，{len(specs)} 个动作，轮询间隔 {a.poll}s", flush=True)
    while True:
        req = store.claim_execution_request()
        if req is None:
            if a.once:
                return 0
            time.sleep(a.poll)
            continue
        read = ReadOnlyHttp(url, user, pw)
        try:
            r = process(req, store, specs, ExecTools(m, read), run, frozen=freeze_state.is_frozen)
        finally:
            read.close()
        print(f"{req.kind} {req.plan_id} → {r.status} {r.output}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
