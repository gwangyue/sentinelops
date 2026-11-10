# 常用命令（CLAUDE.md"命令"一节）。
.PHONY: check lint typecheck imports test demo-check compose-up compose-down kind-up kind-down

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
