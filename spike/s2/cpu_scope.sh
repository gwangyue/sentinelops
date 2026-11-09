#!/usr/bin/env bash
# S2：StressChaos 压 data-2 时，3 个 ES 节点各自报告的 os.cpu.percent / process.cpu.percent，判断指标是节点级还是 VM 级
CTX=kind-sentinel
kubectl --context $CTX -n targets port-forward pod/es-target-es-data-0 9204:9200 >/dev/null 2>&1 & PF=$!; sleep 3
show(){ curl -sk -u sentinel-ro:ro-spike-pass "https://localhost:9204/_nodes/stats/os,process" | jq -r '.nodes[] | "\(.name) os=\(.os.cpu.percent) proc=\(.process.cpu.percent)"'; }
echo "== baseline"; show
kubectl --context $CTX apply -f "$(dirname "$0")/chaos/stress-cpu.yaml" >/dev/null; sleep 30
echo "== stress on data-2 (30s)"; show
kubectl --context $CTX delete -f "$(dirname "$0")/chaos/stress-cpu.yaml" >/dev/null; sleep 30
echo "== 30s after removal"; show
kill $PF
