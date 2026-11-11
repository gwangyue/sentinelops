#!/usr/bin/env bash
# 场景 mysql.lock_wait.*：会话 A 更新一行之后不提交（事务一直开着），会话 B 更新同一行，一直等锁。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/_lib.sh"
case ${1:?inject|revert} in
  inject)
    bg_sql "START TRANSACTION; UPDATE shop.orders SET status='hold' WHERE id=1; SELECT SLEEP(1800) /* chaos-lock-holder */;"
    sleep 2
    for _ in 1 2; do
      bg_sql "SET SESSION innodb_lock_wait_timeout=1800; UPDATE shop.orders SET status='paid' WHERE id=1 /* chaos-lock-waiter */;"
    done ;;
  revert)
    kill_matching '%chaos-lock-waiter%'
    kill_matching '%chaos-lock-holder%' ;;   # 持锁的会话被 KILL，事务回滚
esac
