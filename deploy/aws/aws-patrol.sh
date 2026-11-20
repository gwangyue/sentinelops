#!/usr/bin/env bash
# D16 的验收：在 EKS 上跑一次调查图，确认代码在云上能跑通（08 M4，2026-11-20 决定：不跑评测）。
# 负责：在云上的 Postgres 里冻结版本、建知识索引、注入一个故障、等症状、在 agent-api 的 Pod 里跑 patrol、撤销故障。
# 不负责：判断诊断对不对（那是评测的事）；这里只要求调查走完、状态是 diagnosed。
# 故障选 allocation_disabled：只改一个集群设置再删一个 Pod，不需要 Chaos Mesh，也不需要在节点上填盘（fill_disk.sh 只能在 kind 上用）。
set -euo pipefail
source "$(dirname "$0")/_common.sh"

agent() { kc -n sentinel-system exec deploy/agent-api -- "$@"; }
SCENARIO=$REPO_ROOT/deploy/chaos/es/allocation_disabled.sh

# 云上的 Postgres 是新的，版本表是空的：在这里冻结一次。内容和本地的版本相同，所以哈希相同，只是序号从 v1 开始
agent sentinel version freeze --note "EKS（悉尼）上的首个版本，组件与本地同一提交的版本相同"
agent sentinel knowledge index --pack es

log "先确认集群健康：巡检应该不调用 LLM 就结束"
agent sentinel patrol es-target

log "注入故障：关闭分片分配，并重启 es-target-es-data-2"
trap 'log "撤销故障"; CTX=$CTX "$SCENARIO" revert || true' EXIT
CTX=$CTX "$SCENARIO" inject

log "等症状出现（检测器触发，最多 5 分钟）"
for _ in $(seq 1 30); do
  n=$(agent sentinel detect es-target | sed -n 's/.*findings=\([0-9]*\).*/\1/p')
  [ "${n:-0}" -gt 0 ] && { log "检测到 $n 个问题"; break; }
  sleep 10
done
[ "${n:-0}" -gt 0 ] || die "5 分钟内没有出现症状"

log "跑一次调查"
out=$(agent sentinel patrol es-target)
echo "$out"
inv=$(echo "$out" | grep -o 'inv_[0-9A-Z]*' | head -1)
agent sentinel investigations show "$inv"
echo "$out" | grep -q '状态 diagnosed' || die "调查没有走到 diagnosed"
log "通过：$inv 在 EKS 上走完了调查图"
