SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE       ?= docker compose
TEST_COMPOSE  := docker compose -f docker-compose.test.yml
PYTHON        ?= python3
VENV          := api/.venv
BIN           := $(abspath $(VENV))/bin
TEST_DB_URL   := postgresql+psycopg://jellyrent:test@127.0.0.1:55432/jellyrent
FIXTURES      := $(abspath api/tests/.fixtures)

.PHONY: help env venv media up down nuke logs ps psql bootstrap sync \
        lint fmt typecheck test test-unit test-integration test-concurrency demo

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ----------------------------------------------------------------- setup
env: ## Create .env with random secrets (keeps an existing one)
	@scripts/init_env.sh

venv: ## Create the Python dev environment (api/.venv)
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e 'api[dev]'

media: ## Generate the test films and lay them out per node
	@scripts/prepare_media.sh

# ----------------------------------------------------------------- stack
up: ## Build and start the whole stack
	$(COMPOSE) up -d --build

down: ## Stop the stack (keeps data)
	$(COMPOSE) down

nuke: ## Stop the stack and delete all volumes (Jellyfin config, database)
	$(COMPOSE) down -v

logs: ## Follow application logs
	$(COMPOSE) logs -f bootstrap api watchdog

ps: ## Show service status
	$(COMPOSE) ps

psql: ## Open psql on the central database (owner role)
	$(COMPOSE) exec central-db psql -U jellyrent -d jellyrent

bootstrap: ## Re-run the (idempotent) Jellyfin provisioning job
	$(COMPOSE) run --rm bootstrap

demo: ## Run the demo scenarios against the running stack
	@scripts/demo.sh

# ----------------------------------------------------------------- quality
lint: ## Lint and check formatting
	cd api && $(BIN)/ruff check . && $(BIN)/ruff format --check .

fmt: ## Auto-format
	cd api && $(BIN)/ruff check --fix . && $(BIN)/ruff format .

typecheck: ## Static type check (mypy --strict)
	cd api && $(BIN)/mypy app

test: test-unit test-integration ## Run all tests

test-unit: ## Unit tests (no services needed)
	cd api && $(BIN)/pytest tests/unit

test-integration: ## Integration tests against a throwaway database container
	@mkdir -p $(FIXTURES)
	$(TEST_COMPOSE) up -d --build --wait
	cd api && TEST_DATABASE_URL=$(TEST_DB_URL) TEST_FDW_FIXTURE_DIR=$(FIXTURES) \
		$(BIN)/pytest tests/integration; status=$$?; \
		cd .. && $(TEST_COMPOSE) down -v; exit $$status

test-concurrency: ## Only the concurrency tests, with outcome printout (spec scenario 5)
	@mkdir -p $(FIXTURES)
	$(TEST_COMPOSE) up -d --build --wait
	cd api && TEST_DATABASE_URL=$(TEST_DB_URL) $(BIN)/pytest -s tests/integration/test_concurrency.py; \
		status=$$?; cd .. && $(TEST_COMPOSE) down -v; exit $$status
