#!/usr/bin/env bash
# 场景 es.allocation_disabled.*：关闭分片分配，再重启 data-2。节点回来后，它的分片无法重新分配（S2 的 config_change）。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/_lib.sh"
case ${1:?inject|revert} in
  inject)
    put_setting cluster.routing.allocation.enable '"none"'
    kc -n "$NS" delete pod "$VICTIM" --wait=false >/dev/null ;;
  revert)
    put_setting cluster.routing.allocation.enable null ;;
esac
