#!/usr/bin/env bash
# 全链路内存实测（01 §5；08 M1 D1）：Docker VM 总体、每个 kind 节点、每个容器。结果贴进 spike-results.md。
set -euo pipefail
CLUSTER=sentinel
echo "== Docker VM（在任一节点里看到的是整个 VM）"
docker exec ${CLUSTER}-control-plane free -m | head -2
echo "== kind 节点"
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' $(kind get nodes --name $CLUSTER)
echo "== 每个容器的工作集（crictl stats，按内存降序）"
for n in $(kind get nodes --name $CLUSTER); do
  docker exec "$n" crictl stats -o json | python3 -c '
import json, sys
node = sys.argv[1]
for s in json.load(sys.stdin)["stats"]:
    a = s["attributes"]
    ns = a["labels"].get("io.kubernetes.pod.namespace", "")
    pod = a["labels"].get("io.kubernetes.pod.name", "")
    if "memory" not in s:  # 刚启动或已退出的容器没有内存统计
        continue
    mib = int(s["memory"]["workingSetBytes"]["value"]) / 2**20
    print(f"{mib:8.0f} MiB  {node:<24} {ns}/{pod}/{a["metadata"]["name"]}")
' "$n"
done | sort -rn | head -25
