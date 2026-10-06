# =============================================================================
# Vertical AI Operations Agent — developer entry points
# -----------------------------------------------------------------------------
# Everything CI runs is available here as a single command, so "works on my
# machine" and "works in CI" cannot diverge (DEV-002 acceptance criteria).
# =============================================================================
SHELL := /bin/bash
PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
PIP := $(BIN)/pip
PYTEST := $(BIN)/pytest

.DEFAULT_GOAL := help
.PHONY: help install venv fmt lint typecheck test test-plan test-unit test-integration \
        test-security test-e2e test-cov eval eval-update-baseline security sbom pg-stop \
        migrate run dev docker-build up down ci verify-release rollback-drill export-backlog \
        clean docs

help: ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

venv: ## create the virtualenv
	$(PY) -m venv $(VENV)
	$(PIP) install --upgrade pip

install: venv ## install runtime + dev dependencies (pinned)
	$(PIP) install -e ".[dev,localdb]"

fmt: ## auto-format and auto-fix
	$(BIN)/ruff format app tests evals scripts
	$(BIN)/ruff check --fix app tests evals scripts

lint: ## lint (fails the build on findings)
	$(BIN)/ruff format --check app tests evals scripts
	$(BIN)/ruff check app tests evals scripts

typecheck: ## strict static typing
	$(BIN)/mypy

test-plan: ## validate the plan: backlog structure + documentation contract
	$(PYTEST) tests/unit/planning/test_backlog.py tests/unit/planning/test_documentation.py -q

test-unit: ## fast deterministic unit tests
	$(PYTEST) tests/unit -q -m "unit or not integration"

test-integration: ## integration tests against a real PostgreSQL
	$(PYTEST) tests/integration -q

test-security: ## authorization, prompt injection, secrets
	$(PYTEST) tests/security -q

test-e2e: ## full incident lifecycle over the API
	$(PYTEST) tests/e2e -q

test: ## every test layer
	$(PYTEST) tests -q

test-cov: ## tests with coverage report
	$(PYTEST) tests -q --cov=app --cov-report=term-missing --cov-report=xml

eval: ## run the AI evaluation harness
	$(BIN)/python evals/runner.py --config evals/config.yaml

eval-check: ## run evals and fail on regression past thresholds
	$(BIN)/python evals/runner.py --config evals/config.yaml --check-regression

eval-update-baseline: ## intentionally update the eval baseline (requires review)
	$(BIN)/python evals/runner.py --config evals/config.yaml --update-baseline

security: ## SAST, dependency, and secret scanning
	mkdir -p artifacts
	$(BIN)/bandit -q --exit-zero -c pyproject.toml -r app scripts -f json -o artifacts/bandit.json
	$(BIN)/python scripts/check_bandit.py artifacts/bandit.json
	$(BIN)/pip-audit --strict --requirement requirements.lock || $(BIN)/pip-audit -l
	bash scripts/scan_secrets.sh

sbom: ## generate the software bill of materials
	$(BIN)/python scripts/generate_sbom.py --output artifacts/sbom.json

export-backlog: ## regenerate the Jira CSV and sprint board from the backlog
	$(BIN)/python scripts/export_backlog.py --csv docs/backlog/jira-import.csv \
	    --markdown docs/backlog/SPRINT_BOARD.md

pg: ## start a local PostgreSQL for integration tests and the demo profile
	bash scripts/pg.sh start

pg-stop: ## stop the local PostgreSQL
	bash scripts/pg.sh stop

migrate: ## apply database migrations
	$(BIN)/alembic -c app/persistence/alembic.ini upgrade head

run: ## run the API (development)
	$(BIN)/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

run-prod: ## run the API as the container would
	$(BIN)/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2

openapi: ## dump the OpenAPI contract (used to detect accidental API breaks)
	$(BIN)/python scripts/dump_openapi.py > docs/architecture/openapi.json

docker-build: ## build the container image
	docker build -f infra/docker/Dockerfile -t aiops-agent:$$(git rev-parse --short HEAD) .

up: ## start the full local stack (app + postgres + prometheus)
	docker compose -f infra/compose/docker-compose.yml up --build -d

down: ## stop the local stack
	docker compose -f infra/compose/docker-compose.yml down -v

verify-release: ## run the release verification against a running instance
	$(BIN)/python scripts/verify_release.py --base-url $${BASE_URL:-http://localhost:8000}

rollback-drill: ## prove the rollback procedure works (DEV-004)
	$(BIN)/python scripts/rollback_drill.py

ci: ## exactly what the PR pipeline runs
	bash scripts/ci.sh

clean: ## remove caches and build output
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov coverage.xml artifacts
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
