#!/usr/bin/env bash
# 场景 mysql.connections_exhausted.*：开一批空闲连接（SELECT SLEEP），占到 max_connections 的 N%（默认 95%）。
# 真实情况通常是应用的连接池泄漏；这里用长 SLEEP 模拟"连上了但一直不释放"。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/_lib.sh"
case ${1:?inject|revert} in
  inject)
    max=$(sql "SELECT @@GLOBAL.max_connections")
    now=$(sql "SELECT COUNT(*) FROM information_schema.processlist")
    need=$(( max * ${PCT:-95} / 100 - now ))
    for _ in $(seq 1 "$need"); do bg_sql "SELECT SLEEP(1800) /* chaos-conn */"; done ;;
  revert)
    kill_matching 'SELECT SLEEP(1800) /* chaos-conn */%' ;;
esac
