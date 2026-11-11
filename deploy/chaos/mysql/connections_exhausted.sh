#!/usr/bin/env bash
# 场景 mysql.connections_exhausted.*：占住 max_connections 的 N%（默认 95%）的空闲连接（模拟连接池泄漏）。
# 连接由本机的一个 Python 进程通过 port-forward 持有（hold_connections.py）；撤销时 kill 掉它，连接随之释放。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/_lib.sh"
STATE=${TMPDIR:-/tmp}/sentinel-chaos-mysql-conn
PORT=${HOLD_PORT:-13399}
case ${1:?inject|revert} in
  inject)
    max=$(sql "SELECT @@GLOBAL.max_connections")
    now=$(sql "SELECT COUNT(*) FROM information_schema.processlist")
    need=$(( max * ${PCT:-95} / 100 - now ))
    kubectl --context "$CTX" -n "$NS" port-forward "pod/$POD" "$PORT:3306" >/dev/null 2>&1 &
    echo $! > "$STATE.pf"
    sleep 2
    nohup uv run --project "$HERE/../../.." python "$HERE/hold_connections.py" "$PORT" "$_PW" "$need" > "$STATE.log" 2>&1 &
    echo $! > "$STATE.pid"
    for _ in $(seq 1 30); do grep -q holding "$STATE.log" 2>/dev/null && break; sleep 1; done
    grep -q holding "$STATE.log" || { cat "$STATE.log" >&2; exit 1; } ;;
  revert)
    for f in "$STATE.pid" "$STATE.pf"; do
      [ -f "$f" ] && { kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; }
    done
    pkill -f "hold_connections.py" 2>/dev/null || true
    pkill -f "port-forward pod/$POD $PORT:3306" 2>/dev/null || true ;;
esac
