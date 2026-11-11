#!/usr/bin/env bash
# 场景 mysql.read_only.*：主库被设成只读（例如切换演练之后忘了改回来），所有写入失败。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/_lib.sh"
case ${1:?inject|revert} in
  inject) sql "SET GLOBAL read_only = ON" ;;
  revert) sql "SET GLOBAL read_only = OFF" ;;
esac
