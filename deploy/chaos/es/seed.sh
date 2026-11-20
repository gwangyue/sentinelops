#!/usr/bin/env bash
# es-target 的种子数据：一个有副本的业务索引 bench（3 主分片 × 2 副本，2000 条文档）。
# 为什么需要：系统索引都设了 auto_expand_replicas，节点少了就自动减副本，不会留下未分配分片。
# 没有业务索引时，节点宕机只会表现为"节点数减少"，和真实集群不符（M1 D2 录制时发现；S2 也建了同样的索引）。
# 用管理员账号写入（这是环境准备，不是 Agent 的操作）。文档数够了就跳过；文档用固定 _id，重复写入不会变多。
set -euo pipefail
CTX=${CTX:-kind-sentinel}; PORT=${SEED_PORT:-$((20000 + RANDOM % 1000))}
kc() { kubectl --context "$CTX" "$@"; }
kc -n targets port-forward svc/es-target-es-http "$PORT:9200" >/dev/null 2>&1 &
PF=$!; trap 'kill $PF 2>/dev/null || true' EXIT  # set -e 下 kill 失败会让脚本退出码变成 1
PW=$(kc -n targets get secret es-target-es-elastic-user -o go-template='{{.data.elastic | base64decode}}')
es() { curl -sk -u "elastic:$PW" "$@"; }
U=https://localhost:$PORT
# 等 port-forward 可用（固定的 sleep 不可靠：上一次运行的转发可能还没释放端口）
for _ in $(seq 1 20); do es -o /dev/null "$U/" 2>/dev/null && break; sleep 0.5; done
N=2000
count=$(es "$U/_cat/count/bench?h=count" 2>/dev/null | tr -dc 0-9 || true)
if [ "${count:-0}" -ge "$N" ]; then
  echo "[seed] bench 已有 $count 条文档，跳过"
  exit 0
fi
# 索引可能已经存在但是空的（上次中途失败），所以建索引和写数据分开判断
if [ "$(es -o /dev/null -w '%{http_code}' -I "$U/bench")" != 200 ]; then
  es -XPUT "$U/bench" -H 'content-type: application/json' \
    -d '{"settings":{"number_of_shards":3,"number_of_replicas":2}}' >/dev/null
fi
out=$(python3 -c '
import json
for i in range('"$N"'):
    print(json.dumps({"index": {"_id": str(i)}})); print(json.dumps({"n": i, "msg": "x" * 200}))
' | es -XPOST "$U/bench/_bulk?refresh=true" -H 'content-type: application/x-ndjson' --data-binary @-)
case $out in *'"errors":false'*) ;; *) echo "[seed] 写入失败：${out:0:300}" >&2; exit 1 ;; esac
echo "[seed] bench 已写入：$(es "$U/_cat/count/bench?h=count") 条文档"
