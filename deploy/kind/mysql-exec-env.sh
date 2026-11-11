# 执行器对 mysql-target 的写凭证（ADR-005）：只在执行器的 shell 里 source。
_CTX=kind-sentinel
_PORT=${SENTINEL_MYSQL_EXEC_PORT:-13307}
pkill -f "port-forward .*${_PORT}:3306" 2>/dev/null
kubectl --context "$_CTX" -n targets port-forward pod/mysql-target-0 "${_PORT}:3306" >/dev/null 2>&1 &
export SENTINEL_CRED_MYSQL_EXECUTOR_URL="mysql://127.0.0.1:${_PORT}"
export SENTINEL_CRED_MYSQL_EXECUTOR_USER=sentinel-exec
export SENTINEL_CRED_MYSQL_EXECUTOR_PASSWORD="$(kubectl --context "$_CTX" -n targets get secret mysql-target-auth -o go-template='{{.data.exec | base64decode}}')"
sleep 2
echo "mysql 执行器写连接：${SENTINEL_CRED_MYSQL_EXECUTOR_URL}（port-forward pid $!）"
