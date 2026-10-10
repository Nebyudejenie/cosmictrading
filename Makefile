SHELL := /bin/bash
.DEFAULT_GOAL := help
ENV_LOAD := set -a; [ -f .env ] && source .env; set +a;
COMPOSE_DEV := docker compose --env-file .env -f compose.dev.yaml

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

setup: ## Install dependencies and create .env with random local secrets
	uv sync
	./scripts/init-env.sh

db-up: ## Start development PostgreSQL (127.0.0.1:55442)
	$(COMPOSE_DEV) up -d --wait postgres

db-down: ## Stop development PostgreSQL (data volume kept)
	$(COMPOSE_DEV) stop postgres

migrate: ## Bootstrap roles/database and apply migrations
	$(ENV_LOAD) uv run cosmic db bootstrap && uv run cosmic db migrate

run-web: ## Run the portal/API on 127.0.0.1:8742
	$(ENV_LOAD) uv run cosmic web

run-worker: ## Run the background worker
	$(ENV_LOAD) uv run cosmic worker

fmt: ## Format code
	uv run ruff format src tests
	uv run ruff check --fix src tests

lint:
	uv run ruff format --check src tests
	uv run ruff check src tests

typecheck:
	uv run mypy

secret-scan: ## Scan tracked files for credentials
	uv run python scripts/secret_scan.py

test-unit: ## Tests that need no database
	$(ENV_LOAD) uv run pytest -m "not db and not live_data" -q

test: ## Full test suite (needs make db-up)
	$(ENV_LOAD) uv run pytest -m "not live_data" -q

scan: ## Dependency and static security scans
	uv run pip-audit --strict --progress-spinner off
	uv run bandit -q -r src -c pyproject.toml

check: lint typecheck secret-scan test ## Everything required before a commit

.PHONY: help setup db-up db-down migrate run-web run-worker fmt lint typecheck secret-scan test-unit test scan check

smoke-live-data: ## Real keyless calls to Coinbase/Bitstamp/status (network required; opt-in)
	uv run pytest -m live_data -q tests/live
.PHONY: smoke-live-data
