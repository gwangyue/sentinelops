# 本地开发连接 es-target：开 port-forward，导出只读凭证的环境变量（core/credentials.py 的约定）。
# 用法：source deploy/kind/dev-env.sh    （必须 source，环境变量才会留在当前 shell）
# 只导出只读账号；写账号（M3 执行器）不在这里。
_CTX=kind-sentinel
_PORT=${SENTINEL_ES_PORT:-19200}
# 固定连 data-0：故障都打在 data-2 上；连 Service 时可能正好连到被注入故障的 Pod，连接会断（D2 发现）
pkill -f "port-forward .*${_PORT}:9200" 2>/dev/null
kubectl --context "$_CTX" -n targets port-forward pod/es-target-es-data-0 "${_PORT}:9200" >/dev/null 2>&1 &
export SENTINEL_CRED_ES_READONLY_URL="https://localhost:${_PORT}"
export SENTINEL_CRED_ES_READONLY_USER=sentinel-ro
export SENTINEL_CRED_ES_READONLY_PASSWORD="$(kubectl --context "$_CTX" -n targets get secret sentinel-ro-user -o go-template='{{.data.password | base64decode}}')"
sleep 2
echo "es-target 只读连接：$SENTINEL_CRED_ES_READONLY_URL（port-forward pid $!）"
