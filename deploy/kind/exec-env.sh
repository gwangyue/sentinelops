# 执行器进程的写凭证（ADR-005）：只在启动执行器的那个 shell 里 source，不要和 dev-env.sh 混在同一个 shell。
# 用法：source deploy/kind/exec-env.sh && uv run python -m sentinel.exec
_CTX=kind-sentinel
_PORT=${SENTINEL_EXEC_ES_PORT:-19201}
pkill -f "port-forward .*${_PORT}:9200" 2>/dev/null
kubectl --context "$_CTX" -n targets port-forward pod/es-target-es-data-0 "${_PORT}:9200" >/dev/null 2>&1 &
export SENTINEL_CRED_ES_EXECUTOR_URL="https://localhost:${_PORT}"
export SENTINEL_CRED_ES_EXECUTOR_USER=sentinel-exec
export SENTINEL_CRED_ES_EXECUTOR_PASSWORD="$(kubectl --context "$_CTX" -n targets get secret sentinel-exec-user -o go-template='{{.data.password | base64decode}}')"
sleep 2
echo "执行器写连接：${SENTINEL_CRED_ES_EXECUTOR_URL}（port-forward pid $!）"
