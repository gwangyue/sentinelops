# 本地开发连接 mysql-target 的只读账号（core/credentials 的约定）。用法：source deploy/kind/mysql-env.sh
_CTX=kind-sentinel
_PORT=${SENTINEL_MYSQL_PORT:-13306}
pkill -f "port-forward .*${_PORT}:3306" 2>/dev/null
kubectl --context "$_CTX" -n targets port-forward pod/mysql-target-0 "${_PORT}:3306" >/dev/null 2>&1 &
export SENTINEL_CRED_MYSQL_READONLY_URL="mysql://127.0.0.1:${_PORT}"
export SENTINEL_CRED_MYSQL_READONLY_USER=sentinel-ro
export SENTINEL_CRED_MYSQL_READONLY_PASSWORD="$(kubectl --context "$_CTX" -n targets get secret mysql-target-auth -o go-template='{{.data.ro | base64decode}}')"
sleep 2
echo "mysql-target 只读连接：${SENTINEL_CRED_MYSQL_READONLY_URL}（port-forward pid $!）"
