#!/usr/bin/env bash
# 干扰项 X1 / 场景 es.replicas_unassignable.*：一个副本数超过可分配节点数的无关索引。
# 3 个数据节点放不下 1 主 + 3 副本：永远有 1 个副本未分配，集群一直是 yellow。这是 ES 论坛上最常见的 yellow 原因之一。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/_lib.sh"
IDX=${IDX:-archive-2025}
case ${1:?inject|revert} in
  inject)
    es -XPUT "$U/$IDX" -d '{"settings":{"number_of_shards":1,"number_of_replicas":3}}' >/dev/null
    es -XPOST "$U/$IDX/_doc?refresh=true" -d '{"msg":"archived"}' >/dev/null ;;
  revert)
    es -XDELETE "$U/$IDX" >/dev/null || true ;;
esac
