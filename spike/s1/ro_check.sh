#!/usr/bin/env bash
# S1：创建只读角色/用户，并检查读 API = 200、写 API = 403（01 §6 只读权限的准确名称）
set -u
CTX=kind-sentinel; ES=https://localhost:9202
PW=$(kubectl --context $CTX -n targets get secret es-target-es-elastic-user -o go-template='{{.data.elastic | base64decode}}')
adm(){ curl -sk -u "elastic:$PW" -H 'content-type: application/json' "$@"; }
ro(){ curl -sk -o /dev/null -w '%{http_code}' -u "sentinel-ro:ro-spike-pass" -H 'content-type: application/json' "$@"; }

adm -XPUT $ES/_security/role/sentinel_readonly -d '{"cluster":["monitor"],"indices":[{"names":["*"],"privileges":["monitor","view_index_metadata"]}]}' >/dev/null
adm -XPUT $ES/_security/user/sentinel-ro -d '{"password":"ro-spike-pass","roles":["sentinel_readonly"]}' >/dev/null
# 准备一个副本数超过节点数的索引，保证 allocation/explain 有未分配分片可解释
adm -XPUT "$ES/spike-idx" -d '{"settings":{"number_of_shards":1,"number_of_replicas":3}}' >/dev/null
adm -XPUT "$ES/spike-idx/_doc/1?refresh=true" -d '{"a":1}' >/dev/null

check(){ local want=$1 name=$2; shift 2; local got; got=$(ro "$@"); printf '%s %-34s want=%s got=%s\n' "$([ "$got" = "$want" ] && echo PASS || echo FAIL)" "$name" "$want" "$got"; }
check 200 "GET _cluster/health"            $ES/_cluster/health
check 200 "GET _cat/shards"                $ES/_cat/shards
check 200 "GET _cluster/allocation/explain" -XGET $ES/_cluster/allocation/explain -d '{"index":"spike-idx","shard":0,"primary":false}'
check 200 "GET _nodes/stats"               $ES/_nodes/stats
check 200 "GET _nodes/hot_threads"         $ES/_nodes/hot_threads
check 200 "GET _cluster/settings"          $ES/_cluster/settings
check 200 "GET spike-idx/_settings"        $ES/spike-idx/_settings
check 200 "GET _cat/indices"               $ES/_cat/indices
check 200 "GET _tasks"                     $ES/_tasks
check 200 "GET _cat/recovery"              $ES/_cat/recovery
check 403 "PUT spike-idx/_doc/2"           -XPUT $ES/spike-idx/_doc/2 -d '{"a":2}'
check 403 "PUT _cluster/settings"          -XPUT $ES/_cluster/settings -d '{"persistent":{"cluster.routing.allocation.enable":"none"}}'
check 403 "DELETE spike-idx"               -XDELETE $ES/spike-idx
check 403 "GET spike-idx/_search"          $ES/spike-idx/_search
adm -XDELETE "$ES/spike-idx" >/dev/null
