#!/usr/bin/env bash
# 组合故障里的"红鲱鱼"：把另一个节点（默认 data-1）的数据盘填到 DISK_PCT%（默认 92：ES 按整数显示，91 会被显示成 90，正好卡在阈值上）。
# 磁盘检测器会触发，但它不是这次分片未分配的原因（allocation_explain 能看出来）。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
POD=${DISK_POD:-es-target-es-data-1}
case ${1:?inject|revert} in
  inject) "$HERE/fill_disk.sh" --pod "$POD" --pct "${DISK_PCT:-92}" ;;
  revert) "$HERE/fill_disk.sh" --pod "$POD" --revert ;;
esac
