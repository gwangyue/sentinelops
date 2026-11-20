#!/usr/bin/env bash
# 清理 AWS 上的全部资源，并检查有没有残留（S6：忘了 destroy 一个月约 $700）。
# 负责：先删集群里的东西（尤其是 PVC 对应的 EBS 卷），再 terraform destroy，最后按标签查残留。
# 不负责：state 桶和 ECR 之外的账号资源；state 桶一直保留（几乎不花钱）。
#
# 为什么不能直接 terraform destroy：PVC 触发 EBS CSI 动态创建的 EBS 卷不在 terraform 的 state 里。
# 直接 destroy，集群没了，卷却留下来继续计费。所以先删 PVC、等卷删完，再 destroy。
set -euo pipefail
source "$(dirname "$0")/_common.sh"

k8s_cleanup() {
  if ! aws eks describe-cluster --region "$REGION" --name "$CLUSTER" >/dev/null 2>&1; then
    log "集群不存在，跳过集群内清理"
    return
  fi
  aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER" --alias "$CTX" >/dev/null
  # 先删 chart：ECK 删除 Elasticsearch 时会删它的 PVC；Postgres 的 PVC 不会跟着 StatefulSet 删，下面单独删
  hm uninstall sentinel -n default --wait 2>/dev/null || true
  kc delete pvc --all -A --wait=true --timeout=300s || true
  log "等 EBS 卷删除（存储类的回收策略是 Delete）"
  for _ in $(seq 1 60); do
    [ -z "$(kc get pv -o name 2>/dev/null)" ] && break
    sleep 5
  done
  [ -z "$(kc get pv -o name 2>/dev/null)" ] || die "还有 PV 没删掉：$(kc get pv -o name | tr '\n' ' ')（先查原因，不要直接 destroy）"
  hm uninstall elastic-operator -n elastic-system --wait 2>/dev/null || true
}

leftovers() {
  # 打了 project=sentinelops 标签的资源：terraform 建的（default_tags）和 EBS CSI 建的卷（存储类的 tagSpecification）。
  # 标签 API 有几分钟的延迟，刚删的资源可能还会列出来；再加上直接查最贵的几类资源
  local tagged vols nat eks
  tagged=$(aws resourcegroupstaggingapi get-resources --region "$REGION" \
    --tag-filters Key=project,Values=sentinelops --query 'ResourceTagMappingList[].ResourceARN' --output text)
  vols=$(aws ec2 describe-volumes --region "$REGION" --query 'length(Volumes)' --output text)
  nat=$(aws ec2 describe-nat-gateways --region "$REGION" --filter Name=state,Values=available,pending \
    --query 'length(NatGateways)' --output text)
  eks=$(aws eks list-clusters --region "$REGION" --query 'length(clusters)' --output text)
  log "残留检查：EKS 集群 $eks，NAT $nat，EBS 卷 $vols"
  if [ -n "$tagged" ]; then
    log "带 project=sentinelops 标签的资源（刚删的可能要几分钟才从列表里消失）："
    echo "$tagged" | tr '\t' '\n' | grep -v ":s3:::$STATE_BUCKET" | sed 's/^/  /'
  fi
  [ "$eks" = 0 ] && [ "$nat" = 0 ] && [ "$vols" = 0 ] || die "还有计费资源没删，去控制台确认"
  log "干净（state 桶 $STATE_BUCKET 保留）"
}

k8s_cleanup
tf_init
tf destroy -input=false -var "operator_cidr=$(operator_cidr)" ${AUTO_APPROVE:+-auto-approve}
leftovers
