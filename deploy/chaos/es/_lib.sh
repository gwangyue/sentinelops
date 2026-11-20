# 故障脚本的公共部分：管理员连接（只用于故障台，不是 Agent 的操作）。
# port-forward 固定连 data-0：故障都打在 data-2 上，连 Service 可能正好连到被注入故障的 Pod（D2 发现）。
CTX=${CTX:-kind-sentinel}   # EKS 上用 CTX=sentinel-aws（deploy/aws/aws-patrol.sh）
NS=targets
VICTIM=${VICTIM:-es-target-es-data-2}
kc() { kubectl --context "$CTX" "$@"; }
_PORT=$((21000 + RANDOM % 1000))
kc -n "$NS" port-forward pod/es-target-es-data-0 "$_PORT:9200" >/dev/null 2>&1 &
_PF=$!
# `|| true` 必须有：port-forward 进程可能已经先退出了，kill 会失败；在 set -e 下，EXIT trap 里失败的命令
# 会把整个脚本的退出码变成 1——注入其实已经成功，故障台却当成失败（M2 录制时"exit 1、stderr 为空"的原因）
trap 'kill $_PF 2>/dev/null || true' EXIT
_PW=$(kc -n "$NS" get secret es-target-es-elastic-user -o go-template='{{.data.elastic | base64decode}}')
U=https://localhost:$_PORT
es() { curl -sk -u "elastic:$_PW" -H 'content-type: application/json' "$@"; }
for _ in $(seq 1 20); do es -o /dev/null "$U/" 2>/dev/null && break; sleep 0.5; done
# 没连上就明确报错：否则后面的 curl 静默失败，脚本退出时什么也不说（M2 录制时撤销偶发失败、stderr 为空）
es -o /dev/null "$U/" 2>/dev/null || { echo "port-forward 到 es-target-es-data-0 未就绪（端口 $_PORT）" >&2; exit 1; }
# 写集群设置；值为 null 表示恢复默认
put_setting() { es -XPUT "$U/_cluster/settings" -d "{\"persistent\":{\"$1\":$2}}" >/dev/null; }
