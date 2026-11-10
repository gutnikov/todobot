# Every target runs through uv against the locked dependencies (uv.lock); `make ci` is exactly what CI runs.
UV := uv
RUN := $(UV) run --locked
# `make dev`, `migrate` and `serve` read .env when there is one (see .env.example); the environment wins otherwise.
ENV_FILE := $(if $(wildcard .env),--env-file .env)

.PHONY: help ci lint format-check typecheck test test-unit test-acceptance format migrate serve dev

help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-16s %s\n", $$1, $$2}'

ci: lint format-check typecheck test ## what CI runs: lint, format check, mypy, all tests

lint: ## ruff check
	$(RUN) ruff check

format-check: ## ruff format --check
	$(RUN) ruff format --check

typecheck: ## mypy (strict) on src/
	$(RUN) mypy src

test: ## all tests: unit and acceptance
	$(RUN) pytest

test-unit: ## unit tests only
	$(RUN) pytest tests/unit

test-acceptance: ## acceptance tests only (they start the app as a process)
	$(RUN) pytest tests/acceptance

format: ## apply ruff's formatting and lint fixes
	$(RUN) ruff format
	$(RUN) ruff check --fix

migrate: ## apply the migrations to DATABASE_URL
	$(RUN) $(ENV_FILE) python -m todobot migrate

serve: ## serve the webhook and /healthz on 0.0.0.0:$PORT
	$(RUN) $(ENV_FILE) python -m todobot serve

dev: migrate serve ## migrate, then serve
