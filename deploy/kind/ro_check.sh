#!/usr/bin/env bash
# 检查只读账号 sentinel-ro 的权限（01 §6；S1 的检查项）：读 API 都返回 200，写操作和读文档都返回 403。
# 账号由 kind-up.sh 通过 ECK 声明式创建；这里用 elastic 管理员账号准备一个测试索引，结束时删掉。
set -uo pipefail
CTX=kind-sentinel; PORT=19200; ES=https://localhost:$PORT
kc() { kubectl --context "$CTX" "$@"; }
kc -n targets port-forward svc/es-target-es-http $PORT:9200 >/dev/null 2>&1 &
# kill 后面的 || true：port-forward 先退出时 kill 会失败，在 set -e 的脚本里这会让退出码变成 1
PF=$!; trap 'kill $PF 2>/dev/null || true' EXIT; sleep 3

ADM_PW=$(kc -n targets get secret es-target-es-elastic-user -o go-template='{{.data.elastic | base64decode}}')
RO_PW=$(kc -n targets get secret sentinel-ro-user -o go-template='{{.data.password | base64decode}}')
adm() { curl -sk -u "elastic:$ADM_PW" -H 'content-type: application/json' "$@"; }
ro() { curl -sk -o /dev/null -w '%{http_code}' -u "sentinel-ro:$RO_PW" -H 'content-type: application/json' "$@"; }

# 副本数超过节点数，保证 allocation/explain 有未分配分片可解释
adm -XPUT "$ES/ro-check-idx" -d '{"settings":{"number_of_shards":1,"number_of_replicas":3}}' >/dev/null
adm -XPUT "$ES/ro-check-idx/_doc/1?refresh=true" -d '{"a":1}' >/dev/null

FAIL=0
check() {
  local want=$1 name=$2; shift 2; local got; got=$(ro "$@")
  [ "$got" = "$want" ] && r=PASS || { r=FAIL; FAIL=1; }
  printf '%s %-34s want=%s got=%s\n' "$r" "$name" "$want" "$got"
}
check 200 "GET _cluster/health"             "$ES/_cluster/health"
check 200 "GET _cat/shards"                 "$ES/_cat/shards"
check 200 "GET _cluster/allocation/explain" -XGET "$ES/_cluster/allocation/explain" -d '{"index":"ro-check-idx","shard":0,"primary":false}'
check 200 "GET _nodes/stats"                "$ES/_nodes/stats"
check 200 "GET _nodes/hot_threads"          "$ES/_nodes/hot_threads"
check 200 "GET _cluster/settings"           "$ES/_cluster/settings"
check 200 "GET ro-check-idx/_settings"      "$ES/ro-check-idx/_settings"
check 200 "GET _cat/indices"                "$ES/_cat/indices"
check 200 "GET _tasks"                      "$ES/_tasks"
check 200 "GET _cat/recovery"               "$ES/_cat/recovery"
check 403 "PUT ro-check-idx/_doc/2"         -XPUT "$ES/ro-check-idx/_doc/2" -d '{"a":2}'
check 403 "PUT _cluster/settings"           -XPUT "$ES/_cluster/settings" -d '{"persistent":{"cluster.routing.allocation.enable":"none"}}'
check 403 "DELETE ro-check-idx"             -XDELETE "$ES/ro-check-idx"
check 403 "GET ro-check-idx/_search"        "$ES/ro-check-idx/_search"
adm -XDELETE "$ES/ro-check-idx" >/dev/null
exit $FAIL
