# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""S2：在 kind 上逐个注入 ES 故障，测“多久出现症状”和“撤销后多久恢复 green”（08 §1 S2；03 §6.1）。

负责：注入 / 探测 / 撤销 / 计时，结果写 JSON。
不负责：正式检测器（D2 实现）——这里的探针只是按检测器的判断条件临时写的。
所有故障都打在 data-2 上；探针和管理操作走 data-0 的 port-forward，这样被打的节点挂了探针也不断。
"""
import argparse
import base64
import json
import math
import subprocess
import time
from pathlib import Path

import httpx

CTX, NS, VICTIM = "kind-sentinel", "targets", "es-target-es-data-2"
PORT = 9203
HERE = Path(__file__).parent
RO_AUTH = ("sentinel-ro", "ro-spike-pass")


def kc(*args: str, check: bool = True, input: str | None = None) -> str:
    r = subprocess.run(["kubectl", "--context", CTX, *args], capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def admin_pw() -> str:
    raw = kc("-n", NS, "get", "secret", "es-target-es-elastic-user", "-o", "jsonpath={.data.elastic}")
    return base64.b64decode(raw).decode()


ADM = httpx.Client(base_url=f"https://localhost:{PORT}", verify=False, timeout=15)
RO = httpx.Client(base_url=f"https://localhost:{PORT}", verify=False, timeout=15, auth=RO_AUTH)


def ro_get(path: str) -> dict | list | None:
    try:
        r = RO.get(path)
        return r.json() if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


def health() -> dict:
    return ro_get("/_cluster/health") or {}


# ---------- 探针：判断条件对应设计里的检测器 ----------

def victim_disk_pct() -> float:
    rows = ro_get("/_cat/allocation?format=json") or []
    for r in rows:
        if r.get("node") == VICTIM and r.get("disk.percent"):
            return float(r["disk.percent"])
    return 0.0


def victim_cpu_pct() -> int:
    stats = ro_get("/_nodes/stats/os") or {}
    for n in stats.get("nodes", {}).values():
        if n.get("name") == VICTIM:
            return n["os"]["cpu"]["percent"]
    return 0


def shard_ratio() -> float:
    s = ro_get("/_cluster/settings?include_defaults=true&flat_settings=true") or {}
    lim = None
    for part in ("transient", "persistent", "defaults"):
        lim = lim or s.get(part, {}).get("cluster.max_shards_per_node")
    h = health()
    return h.get("active_shards", 0) / (int(lim) * max(h.get("number_of_data_nodes", 1), 1)) if lim else 0.0


PROBES = {
    "unassigned": lambda: health().get("unassigned_shards", 0) > 0,           # es.unassigned_shards
    "node_left": lambda: health().get("number_of_nodes", 3) < 3,               # 节点数减少
    "disk_high": lambda: victim_disk_pct() >= 90,                              # es.disk_watermark（默认高水位 90%）
    "cpu_high": lambda: victim_cpu_pct() >= 80,                                # 热点 / CPU
    "shard_limit": lambda: shard_ratio() >= 0.9,                               # es.shard_limit
}


# ---------- 故障：inject / revert ----------

def put_settings(body: dict) -> None:
    ADM.put("/_cluster/settings", json={"persistent": body}).raise_for_status()


def chaos_apply(name: str) -> None:
    kc("apply", "-f", str(HERE / "chaos" / f"{name}.yaml"))


def chaos_delete(name: str) -> None:
    kc("delete", "-f", str(HERE / "chaos" / f"{name}.yaml"), "--ignore-not-found", check=False)


def victim_volume() -> tuple[str, str]:
    """data-2 的数据卷在哪个 kind 节点、哪个目录（静态 local PV，见 fixed-pv.yaml）。"""
    pv = kc("-n", NS, "get", "pvc", f"elasticsearch-data-{VICTIM}", "-o", "jsonpath={.spec.volumeName}")
    path = kc("get", "pv", pv, "-o", "jsonpath={.spec.local.path}")
    node = kc("get", "pv", pv, "-o", "jsonpath={.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]}")
    return node, path


def disk_inject() -> None:
    # 在 kind 节点上（ES 容器之外）写文件填盘。S2 发现：在 ES 容器里写，loop 设备的页缓存记在 ES 的
    # 内存 cgroup 上，写到约 1GB 就把 ES 容器 OOMKill 了（加 oflag=direct 也一样）。
    # 故障含义不变：同一块盘被别的进程写满。填到 93%：超过高水位 90%，低于 flood stage 95%
    node, path = victim_volume()
    out = subprocess.run(["docker", "exec", node, "df", "-B1M", path], capture_output=True, text=True, check=True).stdout
    size, used = map(int, out.splitlines()[1].split()[1:3])
    need = int(size * 0.93) - used
    subprocess.run(["docker", "exec", node, "dd", "if=/dev/zero", f"of={path}/spike-fill", "bs=1M", f"count={max(need, 0)}",
                    "conv=fsync", "status=none"], check=True)
    # ES 每 30 秒刷新一次磁盘信息（cluster.info.update.interval）。先等它看到超水位，再创建新索引，
    # 否则新副本可能在刷新前就被分到 data-2 上，导致 unassigned 时有时无（S2 第一轮观察到）
    wait(PROBES["disk_high"], 120)
    # 模拟日常的新索引（例如按天滚动）：副本必须放到每个节点上，data-2 超过低水位就放不下
    ADM.put("/bench-new", json={"settings": {"number_of_shards": 1, "number_of_replicas": 2}}).raise_for_status()


def disk_revert() -> None:
    node, path = victim_volume()
    subprocess.run(["docker", "exec", node, "rm", "-f", f"{path}/spike-fill"], check=False)
    ADM.delete("/bench-new")


def alloc_inject() -> None:
    put_settings({"cluster.routing.allocation.enable": "none"})
    kc("-n", NS, "delete", "pod", VICTIM, "--wait=false")


def alloc_revert() -> None:
    put_settings({"cluster.routing.allocation.enable": None})


def shard_limit_inject() -> None:
    h = ADM.get("/_cluster/health").json()
    put_settings({"cluster.max_shards_per_node": math.ceil(h["active_shards"] / h["number_of_data_nodes"])})
    r = ADM.put("/bench-overflow", json={"settings": {"number_of_shards": 3, "number_of_replicas": 2}})
    print("    create index ->", r.status_code, r.json().get("error", {}).get("reason", "")[:120])


def shard_limit_revert() -> None:
    put_settings({"cluster.max_shards_per_node": None})
    ADM.delete("/bench-overflow")


def hot_inject() -> None:
    chaos_apply("stress-cpu")


def hot_revert() -> None:
    chaos_delete("stress-cpu")


FAULTS = {
    "es.disk_watermark": (disk_inject, disk_revert, ["disk_high", "unassigned"]),
    "es.node_down": (lambda: chaos_apply("pod-failure"), lambda: chaos_delete("pod-failure"), ["node_left"]),
    "es.config_change": (alloc_inject, alloc_revert, ["unassigned"]),
    "es.shard_limit": (shard_limit_inject, shard_limit_revert, ["shard_limit"]),
    "es.hot_node_cpu": (hot_inject, hot_revert, ["cpu_high"]),
    "es.network_partition": (lambda: chaos_apply("partition"), lambda: chaos_delete("partition"), ["node_left"]),
}


def setup() -> None:
    pw = admin_pw()
    ADM.auth = ("elastic", pw)
    ADM.put("/_security/role/sentinel_readonly", json={"cluster": ["monitor"], "indices": [{"names": ["*"], "privileges": ["monitor", "view_index_metadata"]}]}).raise_for_status()
    ADM.put("/_security/user/sentinel-ro", json={"password": RO_AUTH[1], "roles": ["sentinel_readonly"]}).raise_for_status()
    # 种子数据：副本数 2，保证每个节点上都有每个分片的一份拷贝
    if ADM.head("/bench").status_code == 404:
        ADM.put("/bench", json={"settings": {"number_of_shards": 3, "number_of_replicas": 2}}).raise_for_status()
        bulk = "".join(json.dumps({"index": {}}) + "\n" + json.dumps({"n": i, "msg": "x" * 200}) + "\n" for i in range(2000))
        ADM.post("/bench/_bulk?refresh=true", content=bulk, headers={"content-type": "application/x-ndjson"}).raise_for_status()


def wait(pred, timeout: int, every: float = 2.0) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return round(time.monotonic() - t0, 1)
        time.sleep(every)
    return None


def green() -> bool:
    h = health()
    return h.get("status") == "green" and h.get("number_of_nodes") == 3


def run_trial(name: str, timeout: int) -> dict:
    inject, revert, probes = FAULTS[name]
    t0 = time.monotonic()
    try:
        inject()
        # 检测时间一律从注入开始算（包括注入本身的耗时，例如 dd 写盘），所以加上 inject 用掉的时间
        def since_start(dt: float | None) -> float | None:
            return None if dt is None else round(time.monotonic() - t0, 1)
        detect = {p: since_start(wait(PROBES[p], timeout if i == 0 else 120)) for i, p in enumerate(probes)}
    except Exception as e:  # spike：记录失败原因，继续下一个
        detect = {"error": str(e)[:300]}
    finally:
        revert()
    recover = wait(green, 600, every=5)
    primary = probes[0]
    return {"detect_s": detect.get(primary), "detect_all": detect, "recover_s": recover}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--faults", default="all")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    names = list(FAULTS) if args.faults == "all" else args.faults.split(",")

    pf = subprocess.Popen(["kubectl", "--context", CTX, "-n", NS, "port-forward", "pod/es-target-es-data-0", f"{PORT}:9200"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    try:
        setup()
        assert wait(green, 300), "开始前集群不是 green"
        results = []
        for name in names:
            trials = []
            for t in range(args.trials):
                r = run_trial(name, args.timeout)
                print(f"{name} #{t + 1}: {r}", flush=True)
                trials.append(r)
            results.append({"fault": name, "trials": trials})
            Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        pf.terminate()


if __name__ == "__main__":
    main()
