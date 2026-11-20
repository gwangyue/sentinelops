# 常用命令（CLAUDE.md"命令"一节）。
.PHONY: check lint typecheck imports test demo-check compose-up compose-down kind-up kind-down aws-up aws-patrol aws-down

COMPOSE := docker compose -f deploy/compose/docker-compose.yml

# lint + 类型检查 + import 检查 + interrupt 检查 + 测试（08 §6.4）
check: lint typecheck imports test

lint:
	uv run ruff check src tests tools
	uv run ruff format --check src tests tools
	uv run python -m tools.checks.interrupt_lint src

typecheck:
	uv run mypy src

imports:
	uv run lint-imports

test:
	uv run pytest

# golden-demo.md 中已打开的步骤（G6）
demo-check:
	uv run python -m tools.demo_check

compose-up:
	$(COMPOSE) up -d --wait

compose-down:
	$(COMPOSE) down

kind-up:
	deploy/kind/kind-up.sh

kind-down:
	kind delete cluster --name sentinel

# AWS（悉尼，01 §5.2）：只在演示时开，约 $0.95/小时（spike-results S6）。用完务必 make aws-down
aws-up:
	deploy/aws/aws-up.sh

# D16 验收：在 EKS 上注入一个故障，跑一次调查（不跑评测）
aws-patrol:
	deploy/aws/aws-patrol.sh

# 先删 PVC（EBS 卷不在 terraform state 里），再 destroy，最后按标签查残留
aws-down:
	deploy/aws/aws-down.sh
