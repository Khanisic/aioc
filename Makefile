# AIOC task runner.
#
# Windows note: GNU Make is not installed on this machine. Every recipe below
# is a single command you can paste directly into PowerShell if you'd rather
# not install it. To get `make` itself:
#     winget install ezwinports.make
#
# `make chaos-<mode>` is named as a done-when condition in EXECUTION_PLAN.md
# (Day 4), which is why this file exists rather than a scripts/ folder alone.

.DEFAULT_GOAL := help
SHELL := /bin/sh

COMPOSE := docker compose
PSQL := $(COMPOSE) exec -T postgres psql -U $${POSTGRES_USER:-aioc} -d $${POSTGRES_DB:-aioc}

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

# -----------------------------------------------------------------------------
# Environment
# -----------------------------------------------------------------------------

.PHONY: install
install: ## Create the venv and install all dependencies
	uv sync --all-groups

.PHONY: up
up: ## Start the local stack and wait for health
	$(COMPOSE) up -d --wait

.PHONY: down
down: ## Stop the stack, keeping data
	$(COMPOSE) down

.PHONY: logs
logs: ## Tail stack logs
	$(COMPOSE) logs -f

.PHONY: db-reset
db-reset: ## DESTRUCTIVE. Drop volumes and re-run docker/postgres/init
	$(COMPOSE) down -v
	$(COMPOSE) up -d --wait

.PHONY: psql
psql: ## Open a psql shell
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-aioc} -d $${POSTGRES_DB:-aioc}

.PHONY: redis-cli
redis-cli: ## Open a redis shell
	$(COMPOSE) exec redis redis-cli

# -----------------------------------------------------------------------------
# Verification
# -----------------------------------------------------------------------------

.PHONY: verify
verify: ## Stack usable: pgvector present, redis answering, demo app exposing metrics, Prometheus scraping
	@$(COMPOSE) ps --format '{{.Service}}\t{{.Status}}'
	@$(PSQL) -tAc "SELECT 'pgvector ' || extversion FROM pg_extension WHERE extname='vector'" \
		| grep -q pgvector && echo "OK   pgvector installed" || (echo "FAIL pgvector missing" && exit 1)
	@$(COMPOSE) exec -T redis redis-cli ping | grep -q PONG \
		&& echo "OK   redis responding" || (echo "FAIL redis not responding" && exit 1)
	@$(COMPOSE) exec -T checkout-api python -c "import urllib.request; assert b'http_requests_total' in urllib.request.urlopen('http://localhost:8000/metrics', timeout=5).read()" \
		&& echo "OK   checkout-api exposing metrics" || (echo "FAIL checkout-api metrics missing" && exit 1)
	@$(COMPOSE) exec -T payments-api python -c "import urllib.request; assert b'http_requests_total' in urllib.request.urlopen('http://localhost:8000/metrics', timeout=5).read()" \
		&& echo "OK   payments-api exposing metrics" || (echo "FAIL payments-api metrics missing" && exit 1)
	@$(COMPOSE) exec -T inventory-api python -c "import urllib.request; assert b'http_requests_total' in urllib.request.urlopen('http://localhost:8000/metrics', timeout=5).read()" \
		&& echo "OK   inventory-api exposing metrics" || (echo "FAIL inventory-api metrics missing" && exit 1)
	@$(COMPOSE) exec -T prometheus wget -qO- 'http://localhost:9090/api/v1/query?query=up{job=\"demo-app\"}' \
		| grep -q '"value"' && echo "OK   prometheus scraping demo-app" || (echo "FAIL prometheus not scraping demo-app" && exit 1)
	@$(PSQL) -tAc "SELECT count(*) FROM incidents" 2>/dev/null | grep -qE '^1[5-9]|^20$$' \
		&& echo "OK   incident corpus seeded" \
		|| (echo "FAIL incident corpus missing or short - docker/postgres/init/ only runs on an empty volume; make db-reset" && exit 1)
	@$(PSQL) -tAc "SELECT count(DISTINCT true_failure_mode) FROM incidents" 2>/dev/null | grep -q '^5$$' \
		&& echo "OK   corpus covers every failure mode" \
		|| (echo "FAIL corpus does not cover all 5 FailureMode members - the Day 19 eval cannot score the missing one" && exit 1)

.PHONY: lint
lint: ## Lint and type-check
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

.PHONY: fmt
fmt: ## Auto-format
	uv run ruff format .
	uv run ruff check --fix .

.PHONY: test
test: ## Run unit tests (skips integration)
	uv run pytest -m "not integration"

.PHONY: test-all
test-all: ## Run everything, including tests that need the stack up
	uv run pytest

# -----------------------------------------------------------------------------
# Chaos injection  (Day 4)
# -----------------------------------------------------------------------------
# The argument passed to --mode is the exact FailureMode enum member from
# CONTRACTS.md section 4.1. That 1:1 mapping is what lets the Day 19 eval
# harness score agent output against injected ground truth, so these strings
# are not free to drift.
#
# demo-app/chaos/inject.py does not exist until Day 4.

.PHONY: chaos-resource-exhaustion
chaos-resource-exhaustion: ## Inject a memory leak / pool exhaustion
	uv run python demo-app/chaos/inject.py --mode resource_exhaustion

.PHONY: chaos-bad-config-deploy
chaos-bad-config-deploy: ## Inject a bad configuration deploy
	uv run python demo-app/chaos/inject.py --mode bad_config_deploy

.PHONY: chaos-downstream-latency
chaos-downstream-latency: ## Inject a slow downstream dependency
	uv run python demo-app/chaos/inject.py --mode downstream_latency

.PHONY: chaos-code-regression
chaos-code-regression: ## Inject a 500-spike tied to a specific commit
	uv run python demo-app/chaos/inject.py --mode code_regression

.PHONY: chaos-reset
chaos-reset: ## Return the demo app to a healthy baseline
	uv run python demo-app/chaos/inject.py --reset
