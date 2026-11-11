#!/usr/bin/env bash
# MySQL 故障脚本的公共部分（M4 D13）：在 mysql-target 的 Pod 里用 root 执行 SQL（写凭证只在故障脚本里，08 §6.4）。
# 长时间保持的会话（占连接、持锁）在 Pod 里后台运行，撤销时按 SQL 文本找到它们 KILL 掉。
CTX=${CTX:-kind-sentinel}
NS=targets
POD=mysql-target-0
_PW=$(kubectl --context "$CTX" -n "$NS" get secret mysql-target-auth -o go-template='{{.data.root | base64decode}}')
[ -n "$_PW" ] || { echo "拿不到 mysql-target 的 root 密码" >&2; exit 1; }
# 前台执行一条 SQL，-N 去掉表头
sql() { kubectl --context "$CTX" -n "$NS" exec "$POD" -- mysql -uroot -p"$_PW" -N -e "$1" 2>/dev/null; }
# 在 Pod 里后台执行一条 SQL（会话一直保持到 SQL 结束或被 KILL）
bg_sql() { kubectl --context "$CTX" -n "$NS" exec "$POD" -- sh -c "nohup mysql -uroot -p'$_PW' -e \"$1\" >/dev/null 2>&1 &"; }
# KILL 掉 SQL 文本匹配 LIKE 模式的会话
kill_matching() {
  local ids
  ids=$(sql "SELECT id FROM information_schema.processlist WHERE info LIKE '$1' AND id <> CONNECTION_ID()")
  for i in $ids; do sql "KILL $i" || true; done
}
