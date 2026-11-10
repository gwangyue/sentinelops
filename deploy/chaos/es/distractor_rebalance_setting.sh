#!/usr/bin/env bash
# 干扰项 X2：一个看起来和"分配"有关、其实无害的设置改动（只允许主分片做再平衡）。
# 它不会导致分片未分配，但在 cluster_settings 里和真正的根因放在一起，考验模型能不能分清。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/_lib.sh"
case ${1:?inject|revert} in
  inject) put_setting cluster.routing.rebalance.enable '"primaries"' ;;
  revert) put_setting cluster.routing.rebalance.enable null ;;
esac
