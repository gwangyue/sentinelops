#!/usr/bin/env bash
# 场景 es.shard_limit.*：把 cluster.max_shards_per_node 调到"刚好等于当前用量"，新建索引会直接失败。
# 已知局限（S2）：这个注入方式会在集群设置里留下线索；更真实的做法是建大量小索引，M2 扩充场景时再改。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/_lib.sh"
case ${1:?inject|revert} in
  inject)
    read -r active nodes < <(es "$U/_cluster/health" | python3 -c 'import json,sys; h=json.load(sys.stdin); print(h["active_shards"], h["number_of_data_nodes"])')
    put_setting cluster.max_shards_per_node $(( (active + nodes - 1) / nodes )) ;;
  revert)
    put_setting cluster.max_shards_per_node null ;;
esac
