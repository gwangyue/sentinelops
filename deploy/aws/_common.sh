# aws-up / aws-patrol / aws-down 的公共部分。
REGION=ap-southeast-2         # 不是东京：组织的 SCP 只放开了悉尼（ADR-007 修订）
CLUSTER=sentinel
CTX=sentinel-aws              # kubeconfig 里的 context 别名；显式传，不依赖 current-context（S1 发现）
ECK_CHART_VERSION=3.5.0       # 和 kind-up.sh 一致
REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
TF_DIR=$REPO_ROOT/deploy/terraform/aws
CHART=$REPO_ROOT/deploy/helm/sentinel
export AWS_PAGER=""

kc() { kubectl --context "$CTX" "$@"; }
hm() { helm --kube-context "$CTX" "$@"; }
log() { printf '\033[1m[%s]\033[0m %s\n' "$(basename "$0" .sh)" "$*"; }
die() { printf '\033[31m[%s] %s\033[0m\n' "$(basename "$0" .sh)" "$*" >&2; exit 1; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text) || die "AWS 凭证不可用（aws sts get-caller-identity 失败）"
STATE_BUCKET=sentinelops-tfstate-$ACCOUNT

tf() { terraform -chdir="$TF_DIR" "$@"; }
tf_init() { tf init -input=false -reconfigure -backend-config="bucket=$STATE_BUCKET" >/dev/null; }
# operator_cidr 只影响谁能连集群 API；destroy 时也要传（变量是必填的），值是什么不重要
operator_cidr() { echo "$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')/32"; }
