#!/usr/bin/env bash
# 本地全链路环境（01 §5 形态 B；08 M1 D1 的 make kind-up 要求）。
# 负责：kind 集群、每个 worker 的 loop 虚拟盘、静态 PV、ECK、Chaos Mesh、命名空间和网络策略、
#       es-target、es-memory、Postgres、只读用户、es-target 的种子数据。
# 不负责：Agent 本身的部署（D3 起用 Helm chart）、MySQL（M4）、执行器的写账号（M3）。
# 每一步先判断是否已经完成，完成了就跳过，所以可以重复执行。
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

step_disks() {
  # 镜像文件放在节点容器的 /var 下，大小固定；挂载点 /mnt/esdata-N 对应 fixed-pv.yaml 里的路径。
  # 节点容器重启后挂载会丢，但镜像文件还在：重跑时只重新挂载，不重新格式化，数据保留。
  local node i img mnt
  for node in $(kind get nodes --name "$CLUSTER" | grep worker | sort); do
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
  kc apply -f "$M/es-target.yaml" -f "$M/es-memory.yaml" -f "$M/postgres.yaml"
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

step_cluster
step_disks
step_helm
step_workloads
step_wait
step_seed
summary
