#!/usr/bin/env bash
# 场景 es.shard_limit.*：把 cluster.max_shards_per_node 调到"当前用量 / TARGET_RATIO"。
# TARGET_RATIO=1（默认）：刚好等于当前用量，新建索引会直接失败；0.93：接近上限但还没到（外部改编场景）。
# 已知局限（S2）：这个注入方式会在集群设置里留下线索；更真实的做法是建大量小索引，M2 扩充场景时再改。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/_lib.sh"
case ${1:?inject|revert} in
  inject)
    read -r active nodes < <(es "$U/_cluster/health" | python3 -c 'import json,sys; h=json.load(sys.stdin); print(h["active_shards"], h["number_of_data_nodes"])')
    limit=$(python3 -c "import math; print(math.ceil($active / $nodes / ${TARGET_RATIO:-1}))")
    # 分片少的时候上限粒度太粗（15 个分片、目标 0.93 → 上限 6 → 实际只有 0.83）。
    # "接近上限"的真实集群本来就有更多分片：补一个无副本的小索引，把用量凑到 ≥ 90% 且不到上限
    need=$(python3 -c "import math; print(max(0, math.ceil(0.9 * $limit * $nodes) - $active))")
    if [ "$need" -gt 0 ]; then
      es -XPUT "$U/capacity-filler" -d "{\"settings\":{\"number_of_shards\":$need,\"number_of_replicas\":0}}" >/dev/null
    fi
    put_setting cluster.max_shards_per_node "$limit" ;;
  revert)
    put_setting cluster.max_shards_per_node null
    es -XDELETE "$U/capacity-filler" >/dev/null || true ;;
esac
