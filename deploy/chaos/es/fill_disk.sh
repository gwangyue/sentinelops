#!/usr/bin/env bash
# 磁盘水位故障（03 §6.1 场景 es.disk_watermark.*）：把一个 ES 数据节点的数据盘填到指定百分比。
#
# 负责：解析目标盘的位置、注入前的安全检查、填盘、撤销。
# 不负责：等症状出现（bench 的 symptom_wait 负责）、创建新索引来触发未分配分片（bench 负责）。
#
# 为什么在 kind 节点上写、而不是在 ES 容器里写：loop 设备的页缓存记在发起写入的容器的 cgroup 上，
# 在 ES 容器里写到约 1GB 就会把 ES 撑到 OOMKill（S2 实测）。故障含义不变：同一块盘被别的进程写满。
#
# 用法：
#   fill_disk.sh --pod es-target-es-data-2 --pct 93      # 按 Pod 找到它的数据盘
#   fill_disk.sh --pod es-target-es-data-2 --revert
#   fill_disk.sh --node sentinel-worker --path /mnt/esdata-0 --pct 93   # 直接指定
set -euo pipefail

CTX=kind-sentinel
NS=targets
FILL_NAME=sentinel-fill
MAX_BYTES=$((4 * 1024 * 1024 * 1024))   # 安全检查：目标盘不能超过 4G
EXIT_REFUSED=3

POD= NODE= MNT= PCT= REVERT=0
while [ $# -gt 0 ]; do
  case $1 in
    --pod) POD=$2; shift 2 ;;
    --node) NODE=$2; shift 2 ;;
    --path) MNT=$2; shift 2 ;;
    --pct) PCT=$2; shift 2 ;;
    --revert) REVERT=1; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

kc() { kubectl --context "$CTX" "$@"; }
refuse() { echo "拒绝执行：$*" >&2; exit $EXIT_REFUSED; }

if [ -n "$POD" ]; then
  # Pod → PVC → 静态 local PV → 所在节点和路径（deploy/kind/manifests/fixed-pv.yaml）
  pv=$(kc -n "$NS" get pvc "elasticsearch-data-$POD" -o jsonpath='{.spec.volumeName}')
  MNT=$(kc get pv "$pv" -o jsonpath='{.spec.local.path}')
  NODE=$(kc get pv "$pv" -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]}')
fi
[ -n "$NODE" ] && [ -n "$MNT" ] || { echo "需要 --pod，或者 --node 加 --path" >&2; exit 2; }
[ "$REVERT" = 1 ] || [ -n "$PCT" ] || { echo "需要 --pct 或 --revert" >&2; exit 2; }

# 安全检查：目标必须是一个 loop 设备的挂载点，并且设备不超过 4G。
# 防止参数写错时填满 Docker VM 的系统盘（S2：kind 默认的卷和 VM 共用一块 40G 的盘）
src=$(docker exec "$NODE" findmnt -n -o SOURCE --mountpoint "$MNT" 2>/dev/null) || refuse "$NODE:$MNT 不是挂载点"
case $src in
  /dev/loop*) ;;
  *) refuse "$NODE:$MNT 的挂载源是 ${src}，不是 loop 设备" ;;
esac
bytes=$(docker exec "$NODE" blockdev --getsize64 "$src")
[ "$bytes" -le "$MAX_BYTES" ] || refuse "${src} 大小 ${bytes} 字节，超过 4G 上限"

if [ "$REVERT" = 1 ]; then
  docker exec "$NODE" rm -f "$MNT/$FILL_NAME"
  echo "已撤销：$NODE:$MNT/$FILL_NAME"
  exit 0
fi

# 按 ES 的口径算使用率：1 - 可用/总量。ext4 给 root 保留的 5% 在 ES 看来也是"已用"，
# 所以不能用 df 的 used 列（它不含保留块），否则会多写约 5%，越过 flood stage
read -r size avail < <(docker exec "$NODE" df -B1M --output=size,avail "$MNT" | tail -1)
need=$(( size * PCT / 100 - (size - avail) ))
if [ "$need" -le 0 ]; then
  echo "已经达到 ${PCT}%（可用 ${avail}M / 总量 ${size}M），不需要再写"
  exit 0
fi
docker exec "$NODE" dd if=/dev/zero of="$MNT/$FILL_NAME" bs=1M count="$need" oflag=append conv=notrunc,fsync status=none
read -r size avail < <(docker exec "$NODE" df -B1M --output=size,avail "$MNT" | tail -1)
echo "已填盘：$NODE:$MNT (${src}) ES 口径使用率 $(( 100 - avail * 100 / size ))%"
