# SentinelOps 镜像（01 §5：三种部署形态用同一套镜像）。
# 负责：agent-api（uvicorn）、执行器（python -m sentinel.exec）、CLI（sentinel ...）共用一个镜像，靠启动命令区分。
# 不负责：凭证（运行时由 K8s Secret 注入环境变量）；被管系统和数据组件（Helm chart 里的 ECK / Postgres）。
#
# 为什么镜像里带 .git：版本哈希和"工作区干净"检查（ADR-012）调用 git，读的是 HEAD 里 src/ config/ uv.lock
# pyproject.toml 的对象哈希。带上 .git（约 5MB），容器里算出的 code_sha 和本机完全一样，core 不用为容器改代码。
# 构建脚本（deploy/aws/build-push.sh）先确认这几个路径没有未提交的改动，否则镜像里的代码对应不到一个提交。
#
# 版本写死，只用发布满两周的版本（spike-results.md 的规则）：python 3.12.15（2026-10-22）、uv 0.11.17（和本机一致，
# 锁文件格式不会不兼容）。Mac 是 arm64、EKS 节点是 x86（m6i），所以构建时指定 --platform linux/amd64。
FROM ghcr.io/astral-sh/uv:0.11.17 AS uv

FROM python:3.12.15-slim-trixie
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# 以非 root 运行；git 会拒绝读不属于当前用户的仓库（dubious ownership），所以文件归这个用户所有
RUN useradd --create-home --uid 10001 sentinel && mkdir /app && chown sentinel:sentinel /app
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never

# 先只装依赖：依赖不变时这一层走缓存，改代码只重建后面几层
COPY --chown=sentinel:sentinel pyproject.toml uv.lock ./
USER sentinel
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=sentinel:sentinel . .
RUN uv sync --frozen --no-dev \
    # 构建时就确认能算出版本哈希、工作区是干净的：在这里失败，比部署之后巡检被拒绝好查
    && .venv/bin/python -c "from sentinel.core.versioning.components import behavior_sha, behavior_dirty; d = behavior_dirty(); assert not d, d; print('code_sha', behavior_sha())"

ENV PATH=/app/.venv/bin:$PATH
# 观测记录、审计锚点等写在 var/ 下（相对 /app）；K8s 上挂 emptyDir
CMD ["uvicorn", "sentinel.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
