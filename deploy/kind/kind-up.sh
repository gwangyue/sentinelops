#!/usr/bin/env bash
# 本地全链路环境（01 §5 形态 B；08 M1 D1 的 make kind-up 要求）。
# 负责：kind 集群、每个 worker 的 loop 虚拟盘、静态 PV、ECK、Chaos Mesh、命名空间和网络策略、
#       es-target、es-memory、Postgres、只读用户、es-target 的种子数据。
# 不负责：Agent 本身的部署（D3 起用 Helm chart）、MySQL（M4）、执行器的写账号（M3）。
# 每一步先判断是否已经完成，完成了就跳过，所以可以重复执行。
# Docker 重启之后只需要重新挂盘：deploy/kind/kind-up.sh remount-disks（make kind-remount）。
set -euo pipefail

CLUSTER=sentinel
CTX=kind-${CLUSTER}
HERE=$(cd "$(dirname "$0")" && pwd)
M=$HERE/manifests
# 版本写死，只用发布满两周的版本（spike-results.md）
ECK_CHART_VERSION=3.5.0
CHAOS_CHART_VERSION=2.8.6
DISK_SIZE=3G
DISKS_PER_WORKER=2

# 显式 context：本机默认 context 指向别的集群，不能依赖 current-context（S1 发现）
kc() { kubectl --context "$CTX" "$@"; }
hm() { helm --kube-context "$CTX" "$@"; }
log() { printf '\033[1m[kind-up]\033[0m %s\n' "$*"; }

step_cluster() {
  if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    log "集群 $CLUSTER 已存在，跳过"
  else
    log "创建集群 $CLUSTER"
    kind create cluster --name "$CLUSTER" --config "$HERE/kind.yaml"
  fi
  kubectl config get-contexts -o name | grep -qx "$CTX" || { echo "找不到 context $CTX" >&2; exit 1; }
}

# 盘的挂载（S2 的固定大小 loop 盘）。镜像文件放在节点容器的 /var 下，挂载点 /mnt/esdata-N 对应 fixed-pv.yaml。
# 节点容器重启（Docker 重启、机器重启）之后挂载会丢，但镜像文件还在：只重新挂载，不重新格式化，数据保留。
WORKERS() { kind get nodes --name "$CLUSTER" | grep worker | sort; }
ES_TARGET_PODS=elasticsearch.k8s.elastic.co/cluster-name=es-target

disks_missing() {
  local node i
  for node in $(WORKERS); do
    for ((i = 0; i < DISKS_PER_WORKER; i++)); do
      docker exec "$node" mountpoint -q "/mnt/esdata-$i" || return 0
    done
  done
  return 1
}

mount_disks() {
  local node i img mnt
  for node in $(WORKERS); do
    for ((i = 0; i < DISKS_PER_WORKER; i++)); do
      img=/var/esdata-$i.img
      mnt=/mnt/esdata-$i
      if docker exec "$node" mountpoint -q "$mnt"; then
        log "$node:$mnt 已挂载，跳过"
        continue
      fi
      docker exec "$node" sh -euc "
        [ -f $img ] || truncate -s $DISK_SIZE $img
        blkid $img >/dev/null 2>&1 || mkfs.ext4 -q -F $img
        mkdir -p $mnt
        mount -o loop $img $mnt
        chmod 777 $mnt   # ES 容器以 uid 1000 运行，要能写
      "
      log "$node:$mnt 已挂载 $(docker exec "$node" findmnt -n -o SOURCE,SIZE "$mnt")"
    done
  done
}

# es-target 已经在跑时重新挂盘。顺序不能错（2026-11-20 踩过）：
#   节点的 / 是 shared 传播。Pod 还在时挂盘，新挂载会传播进旧 Pod 的卷路径；随后删 Pod，kubelet 清理卷路径时的
#   umount 又传播回 /mnt/esdata-N，盘被卸掉，重建的 Pod 拿到的还是节点上的空目录（ES 报 master not discovered）。
# 所以：封锁 worker → 删 es-target 的 Pod（新 Pod 停在 Pending）→ 等节点上没有 esdata 卷挂载 → 挂盘 → 解除封锁。
remount_with_pods_stopped() {
  local workers node
  workers=$(WORKERS)
  log "es-target 在运行：先封锁 worker、停掉 es-target 的 Pod，再挂盘"
  # shellcheck disable=SC2086
  kc cordon $workers
  # 中途失败也要解除封锁，否则之后所有 Pod 都调度不上去
  # shellcheck disable=SC2064,SC2086
  trap "kubectl --context $CTX uncordon $workers >/dev/null" EXIT
  # ES 的优雅退出最多要 3 分钟
  kc -n targets delete pod -l "$ES_TARGET_PODS" --wait=true --timeout=300s
  for _ in $(seq 1 60); do
    local busy=0
    for node in $workers; do
      docker exec "$node" sh -c 'mount | grep -q "local-volume/esdata"' && busy=1
    done
    [ $busy = 0 ] && break
    sleep 2
  done
  [ $busy = 0 ] || { echo "2 分钟后节点上还有 esdata 卷挂载，没有挂盘" >&2; return 1; }
  mount_disks
  # shellcheck disable=SC2086
  kc uncordon $workers
  trap - EXIT
}

step_disks() {
  if ! disks_missing; then
    log "所有 esdata 盘都已挂载，跳过"
    return 0
  fi
  if kc -n targets get pod -l "$ES_TARGET_PODS" -o name 2>/dev/null | grep -q .; then
    remount_with_pods_stopped
  else
    mount_disks  # 第一次建集群：还没有 es-target，直接挂
  fi
}

step_helm() {
  hm repo add elastic https://helm.elastic.co >/dev/null 2>&1 || true
  hm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null 2>&1 || true
  if hm status elastic-operator -n elastic-system >/dev/null 2>&1; then
    log "ECK operator 已安装，跳过"
  else
    hm repo update >/dev/null
    log "安装 ECK operator $ECK_CHART_VERSION"
    hm install elastic-operator elastic/eck-operator --version "$ECK_CHART_VERSION" \
      -n elastic-system --create-namespace --wait
  fi
  if hm status chaos-mesh -n chaos-mesh >/dev/null 2>&1; then
    log "Chaos Mesh 已安装，跳过"
  else
    hm repo update >/dev/null
    log "安装 Chaos Mesh $CHAOS_CHART_VERSION"
    # kind 节点用 containerd，chaos-daemon 要指到它的 socket（S2）
    hm install chaos-mesh chaos-mesh/chaos-mesh --version "$CHAOS_CHART_VERSION" \
      -n chaos-mesh --create-namespace --wait \
      --set chaosDaemon.runtime=containerd \
      --set chaosDaemon.socketPath=/run/containerd/containerd.sock
  fi
}

# 只在 Secret 不存在时生成随机密码；已经存在就保留，否则重跑会让已有的连接失效
ensure_secret() {
  local ns=$1 name=$2; shift 2
  if kc -n "$ns" get secret "$name" >/dev/null 2>&1; then
    log "Secret $ns/$name 已存在，跳过"
  else
    kc -n "$ns" create secret generic "$name" "$@"
    log "Secret $ns/$name 已创建"
  fi
}

step_workloads() {
  # apply 本身就是幂等的
  kc apply -f "$M/namespaces.yaml" -f "$M/fixed-pv.yaml" -f "$M/netpol.yaml"
  ensure_secret sentinel-data postgres-auth --from-literal=password="$(openssl rand -hex 16)"
  # ECK 的 fileRealm 支持 basic-auth 类型的 Secret，roles 键写角色名
  if ! kc -n targets get secret sentinel-ro-user >/dev/null 2>&1; then
    kc -n targets create secret generic sentinel-ro-user --type=kubernetes.io/basic-auth \
      --from-literal=username=sentinel-ro \
      --from-literal=password="$(openssl rand -hex 16)" \
      --from-literal=roles=sentinel_readonly
    log "Secret targets/sentinel-ro-user 已创建"
  else
    log "Secret targets/sentinel-ro-user 已存在，跳过"
  fi
  # 执行器的写账号（ADR-005）：只有执行器进程会拿到它，见 deploy/kind/exec-env.sh
  if ! kc -n targets get secret sentinel-exec-user >/dev/null 2>&1; then
    kc -n targets create secret generic sentinel-exec-user --type=kubernetes.io/basic-auth \
      --from-literal=username=sentinel-exec \
      --from-literal=password="$(openssl rand -hex 16)" \
      --from-literal=roles=sentinel_executor
    log "Secret targets/sentinel-exec-user 已创建"
  else
    log "Secret targets/sentinel-exec-user 已存在，跳过"
  fi
  # mysql-target（M4 D13）：root / 只读 / 执行器三个账号的密码，首次运行时随机生成
  ensure_secret targets mysql-target-auth --from-literal=root="$(openssl rand -hex 16)" \
    --from-literal=ro="$(openssl rand -hex 16)" --from-literal=exec="$(openssl rand -hex 16)"
  kc apply -f "$M/es-target.yaml" -f "$M/es-memory.yaml" -f "$M/postgres.yaml" -f "$M/mysql-target.yaml"
}

wait_green() {
  local ns=$1 name=$2 health
  for _ in $(seq 1 120); do
    health=$(kc -n "$ns" get elasticsearch "$name" -o jsonpath='{.status.health}' 2>/dev/null || true)
    [ "$health" = green ] && { log "$ns/$name: green"; return 0; }
    sleep 5
  done
  echo "$ns/$name 10 分钟内没有变成 green（当前：${health:-无}）" >&2
  return 1
}

step_wait() {
  wait_green targets es-target
  wait_green sentinel-data es-memory
  kc -n sentinel-data rollout status statefulset/postgres --timeout=300s
}

step_seed() {
  # 业务数据：没有带副本的业务索引，节点类故障不会留下未分配分片（见 seed.sh 的说明）
  "$HERE/../chaos/es/seed.sh"
}

summary() {
  log "完成。状态："
  kc get elasticsearch -A
  kc -n sentinel-data get pods
  docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' $(kind get nodes --name "$CLUSTER")
}

case "${1:-all}" in
  all)
    step_cluster
    step_disks
    step_helm
    step_workloads
    step_wait
    step_seed
    summary
    ;;
  remount-disks)
    # Docker 重启之后用：只重新挂盘并等 es-target 变绿，不动其他组件（make kind-remount）
    step_disks
    wait_green targets es-target
    ;;
  *)
    echo "用法：$0 [all|remount-disks]" >&2
    exit 2
    ;;
esac
