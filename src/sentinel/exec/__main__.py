"""执行器进程入口：`python -m sentinel.exec`（ADR-005：独立进程、单独持有写凭证）。

负责：加载所有有写凭证的专家包；轮询 Postgres 里的 execution_requests，按方案所属的专家包取连接，交给 core.process。
不负责：任何校验（core.py）；生产部署时的独立命名空间和网络策略（M4 的 Helm chart）。
"""

import argparse
import os
import sys
import time
from dataclasses import dataclass

from sentinel.contracts import ActionSpec, PackManifest
from sentinel.exec.core import live_runner, process
from sentinel.exec.live import ExecCredentialError, ExecTools, discover_packs, load_specs, manifest, write_connection
from sentinel.governance.kill_switch import PgFreezeState
from sentinel.governance.remediation_store import PgRemediationStore
from sentinel.sdk import open_read, open_write


@dataclass
class PackRuntime:
    manifest: PackManifest
    url: str
    user: str
    password: str


def main() -> int:
    ap = argparse.ArgumentParser(description="SentinelOps 执行器")
    ap.add_argument("--once", action="store_true", help="处理完当前队列就退出（测试和录制用）")
    ap.add_argument("--poll", type=float, default=2.0)
    a = ap.parse_args()
    # 所有专家包的动作目录合在一起；只有设置了写凭证的专家包才能执行（没有凭证的动作会被拒绝：不在目录里）
    packs: dict[str, PackRuntime] = {}
    specs: dict[str, ActionSpec] = {}
    for name in discover_packs():
        m = manifest(name)
        try:
            url, user, pw = write_connection(m)
        except ExecCredentialError as e:
            print(f"跳过 {name}：{e}", flush=True)
            continue
        packs[name] = PackRuntime(m, url, user, pw)
        specs |= load_specs(m)
    store = PgRemediationStore(
        os.environ.get("SENTINEL_PG_URL", "postgresql://postgres:sentinel-local@localhost:5432/sentinel")
    )
    freeze_state = PgFreezeState(store.url)
    print(
        f"执行器已启动：{', '.join(packs) or '没有可用的专家包'}，{len(specs)} 个动作，轮询间隔 {a.poll}s", flush=True
    )
    while True:
        req = store.claim_execution_request()
        if req is None:
            if a.once:
                return 0
            time.sleep(a.poll)
            continue
        # 方案属于哪个专家包，由它的动作规格决定；连接类型由那个专家包的 connection.kind 决定
        plan = store.get_plan(req.plan_id)
        spec = specs.get(plan.action_id) if plan else None
        rt = packs.get(spec.pack) if spec else None
        if rt is None:
            # 交给 process：它会以"不在动作目录里 / 方案不存在"拒绝并写审计
            rt = next(iter(packs.values()), None)
        if rt is None:
            print(f"{req.plan_id}：没有任何专家包有写凭证，无法处理", flush=True)
            continue
        kind = rt.manifest.connection.kind
        run = live_runner(lambda rt=rt, kind=kind: open_write(kind, rt.url, rt.user, rt.password))  # type: ignore[misc]
        read = open_read(kind, rt.url, rt.user, rt.password)
        try:
            r = process(req, store, specs, ExecTools(rt.manifest, read), run, frozen=freeze_state.is_frozen)
        finally:
            read.close()
        print(f"{req.kind} {req.plan_id} → {r.status} {r.output}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
