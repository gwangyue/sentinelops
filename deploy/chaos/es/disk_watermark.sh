#!/usr/bin/env bash
# 场景 es.disk_watermark.*：把 data-2 的数据盘填到 PCT%（默认 93），等 ES 刷新磁盘信息，再建一个需要 3 份拷贝的索引。
# 等待是必须的：分配决策用的磁盘快照每 30 秒刷新一次，立刻建索引的话副本照样会分到 data-2（S2、D2 实测）。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/_lib.sh"
case ${1:?inject|revert} in
  inject)
    "$HERE/fill_disk.sh" --pod "$VICTIM" --pct "${PCT:-93}"
    sleep "${REFRESH_WAIT:-40}"
    es -XPUT "$U/bench-new" -d '{"settings":{"number_of_shards":1,"number_of_replicas":2}}' >/dev/null ;;
  revert)
    es -XDELETE "$U/bench-new" >/dev/null || true
    "$HERE/fill_disk.sh" --pod "$VICTIM" --revert ;;
esac
