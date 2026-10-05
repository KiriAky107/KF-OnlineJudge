SHELL := /bin/bash
CARGO ?= $(if $(wildcard $(HOME)/.cargo/bin/cargo),$(HOME)/.cargo/bin/cargo,cargo)
UV ?= uv
PNPM ?= pnpm

.PHONY: help install check architecture check-backend check-frontend check-judge contracts format api frontend coordinator sandbox-probe worker-check infra-config infra-up infra-down

help:
	@echo 'install          安装锁定依赖并编译 Rust workspace'
	@echo 'check            架构、契约、静态检查、测试与前端构建'
	@echo 'contracts        更新 JSON Schema、OpenAPI 与前端类型'
	@echo 'format           格式化 Python、Rust 与前端源码'
	@echo 'api / frontend   启动平台 API / 前端开发服务'
	@echo 'coordinator      启动协调器 HTTP 骨架'
	@echo 'sandbox-probe    只读探测当前 Linux 环境'
	@echo 'worker-check     验证三个后台进程入口，不消费任务'
	@echo 'infra-config     检查本地 PostgreSQL / RabbitMQ 配置'
	@echo 'infra-up/down    启动/停止本地开发依赖（保留数据卷）'

install:
	cd backend && $(UV) sync --locked
	cd frontend && $(PNPM) install --frozen-lockfile
	cd judge && $(CARGO) build --workspace --locked

check: architecture check-backend check-judge check-frontend infra-config

architecture:
	python3 scripts/check_architecture.py

check-backend:
	cd backend && $(UV) run --locked ruff check . ../scripts
	cd backend && $(UV) run --locked ruff format --check . ../scripts
	cd backend && $(UV) run --locked pytest
	cd backend && $(UV) run --locked python -m kf_oj.contracts.export --check

check-judge:
	cd judge && $(CARGO) fmt --all -- --check
	cd judge && $(CARGO) clippy --workspace --all-targets --locked -- -D warnings
	cd judge && $(CARGO) test --workspace --locked

check-frontend:
	cd frontend && $(PNPM) check:api
	cd frontend && $(PNPM) lint
	cd frontend && $(PNPM) format:check
	cd frontend && $(PNPM) typecheck
	cd frontend && $(PNPM) test
	cd frontend && $(PNPM) build

contracts:
	cd backend && $(UV) run --locked python -m kf_oj.contracts.export
	cd frontend && $(PNPM) generate:api

format:
	cd backend && $(UV) run --locked ruff check --fix . ../scripts
	cd backend && $(UV) run --locked ruff format . ../scripts
	cd judge && $(CARGO) fmt --all
	cd frontend && $(PNPM) format

api:
	cd backend && $(UV) run --locked uvicorn kf_oj.api.app:create_app --factory --reload --host 127.0.0.1 --port 8000

frontend:
	cd frontend && $(PNPM) dev

coordinator:
	cd judge && $(CARGO) run --locked -p kf-coordinator

sandbox-probe:
	cd judge && $(CARGO) run --locked -p kf-sandbox -- probe

worker-check:
	cd backend && $(UV) run --locked kf-learning-worker --check
	cd backend && $(UV) run --locked kf-outbox-publisher --check
	cd judge && $(CARGO) run --locked -p kf-worker -- --check

infra-config:
	docker compose --env-file deploy/.env.example -f deploy/compose.dev.yml config --quiet

infra-up:
	docker compose --env-file deploy/.env -f deploy/compose.dev.yml up -d --wait

infra-down:
	docker compose --env-file deploy/.env -f deploy/compose.dev.yml down
