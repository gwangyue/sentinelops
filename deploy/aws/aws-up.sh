#!/usr/bin/env bash
# 在 AWS（悉尼）上建出完整环境（01 §5.2，08 M4 D15–D16）。
# 负责：state 桶、terraform apply、kubeconfig、镜像构建和推送、ECK operator、Helm chart、Gemini/LangSmith 的 Secret、种子数据。
# 不负责：跑调查（aws-patrol.sh）、清理（aws-down.sh）。Chaos Mesh 不装：D16 只用 allocation_disabled，靠改集群设置注入。
# 每一步可以重复执行。开始计费：控制面 + 3 台节点 + NAT + 端点约 $0.95/小时（spike-results S6），用完务必 make aws-down。
set -euo pipefail
source "$(dirname "$0")/_common.sh"

preflight() {
  local t
  for t in terraform kubectl helm docker; do command -v "$t" >/dev/null || die "缺少 $t"; done
  # kubectl 只保证和相差一个小版本的集群兼容；集群是 1.37
  local kv; kv=$(kubectl version --client -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["clientVersion"]["minor"])')
  [ "${kv//[^0-9]/}" -ge 36 ] || die "kubectl 版本太旧（1.$kv），先 brew upgrade kubectl"
  [ -n "${GOOGLE_API_KEY:-}" ] || die "缺少 GOOGLE_API_KEY（诊断和 embedding 要用）"
  [ -n "${LANGSMITH_API_KEY:-}" ] || die "缺少 LANGSMITH_API_KEY"
  # 镜像里的代码必须对应一个提交（ADR-012）；Dockerfile 里也会再检查一次
  local dirty; dirty=$(git -C "$REPO_ROOT" status --porcelain -- src config uv.lock pyproject.toml)
  [ -z "$dirty" ] || die "影响行为的文件有未提交的改动，先提交：$dirty"
  log "账号 $ACCOUNT，区域 $REGION"
}

state_bucket() {
  # state 桶一直保留（几乎不花钱）：放在 terraform 外面建，避免"用 terraform 管 terraform 自己的 state"
  if aws s3api head-bucket --bucket "$STATE_BUCKET" --region "$REGION" 2>/dev/null; then
    log "state 桶 $STATE_BUCKET 已存在"
    return
  fi
  aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" --versioning-configuration Status=Enabled
  aws s3api put-public-access-block --bucket "$STATE_BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  log "state 桶 $STATE_BUCKET 已创建（开启版本控制，禁止公开访问）"
}

infra() {
  tf_init
  # 不加 -auto-approve：apply 前看一眼计划再输入 yes；AUTO_APPROVE=1 可以跳过
  tf apply -input=false -var "operator_cidr=$(operator_cidr)" ${AUTO_APPROVE:+-auto-approve}
  aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER" --alias "$CTX" >/dev/null
  kc get nodes
}

image() {
  local repo tag
  repo=$(tf output -raw ecr_repository_url)
  # tag 用提交号：仓库设成不可变 tag，同一个提交只推一次
  tag=$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${repo%%/*}" >/dev/null
  if aws ecr describe-images --region "$REGION" --repository-name sentinelops --image-ids imageTag="$tag" >/dev/null 2>&1; then
    log "镜像 $tag 已在 ECR，跳过构建"
  else
    # Mac 是 arm64，节点是 x86（m6i）
    docker build --platform linux/amd64 -f "$REPO_ROOT/deploy/docker/Dockerfile" -t "$repo:$tag" "$REPO_ROOT"
    docker push "$repo:$tag"
  fi
  IMAGE_REPO=$repo IMAGE_TAG=$tag
}

workloads() {
  hm repo add elastic https://helm.elastic.co >/dev/null 2>&1 || true
  if ! hm status elastic-operator -n elastic-system >/dev/null 2>&1; then
    hm repo update elastic >/dev/null
    log "安装 ECK operator $ECK_CHART_VERSION"
    hm install elastic-operator elastic/eck-operator --version "$ECK_CHART_VERSION" \
      -n elastic-system --create-namespace --wait
  fi
  # release 装在 default：chart 自己创建四个命名空间（namespaces.yaml）
  log "安装 / 升级 chart（镜像 $IMAGE_TAG）"
  hm upgrade --install sentinel "$CHART" -n default -f "$CHART/values-aws.yaml" \
    --set image.repository="$IMAGE_REPO" --set image.tag="$IMAGE_TAG"
  # key 不经过 helm（不进 release 记录）。命名空间由上一步的 chart 创建；Secret 晚到几秒，kubelet 会自己重试
  kc -n sentinel-system create secret generic sentinel-llm \
    --from-literal=GOOGLE_API_KEY="$GOOGLE_API_KEY" --from-literal=LANGSMITH_API_KEY="$LANGSMITH_API_KEY" \
    --dry-run=client -o yaml | kc apply -f - >/dev/null
}

wait_ready() {
  local ns name h ok
  for pair in targets/es-target sentinel-data/es-memory; do
    ns=${pair%/*} name=${pair#*/} ok=
    for _ in $(seq 1 120); do
      h=$(kc -n "$ns" get elasticsearch "$name" -o jsonpath='{.status.health}' 2>/dev/null || true)
      # es-memory 是单节点，有副本的索引会让它停在 yellow，也算可用
      if [ "$h" = green ] || { [ "$name" = es-memory ] && [ "$h" = yellow ]; }; then ok=1; break; fi
      sleep 5
    done
    [ -n "$ok" ] || die "$ns/$name 10 分钟内没有就绪（当前：${h:-无}）"
    log "$ns/$name: $h"
  done
  kc -n sentinel-data rollout status statefulset/postgres --timeout=300s
  kc -n sentinel-system rollout status deploy/agent-api --timeout=300s
  kc -n sentinel-exec rollout status deploy/executor --timeout=300s
}

preflight
state_bucket
infra
image
workloads
wait_ready
CTX=$CTX "$REPO_ROOT/deploy/chaos/es/seed.sh"
log "完成。跑一次调查：make aws-patrol；用完务必：make aws-down"
