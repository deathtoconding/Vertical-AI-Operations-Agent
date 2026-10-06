#!/usr/bin/env bash
# Local mirror of the pull-request pipeline (.github/workflows/ci.yml).
# Every stage is blocking. Run with `make ci`.
set -euo pipefail

cd "$(dirname "$0")/.."
BIN="${VIRTUAL_ENV:-.venv}/bin"
PY="${BIN}/python"
PYTEST="${BIN}/pytest"
RUFF="${BIN}/ruff"
MYPY="${BIN}/mypy"

stage() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

stage "1/9 pre-commit hygiene (large files, conflict markers, secrets)"
bash scripts/check_repo_hygiene.sh

stage "2/9 lint"
"$RUFF" format --check app tests evals scripts
"$RUFF" check app tests evals scripts

stage "3/9 type checking"
"$MYPY"

stage "4/9 plan contract (backlog + documentation)"
"$PYTEST" tests/unit/planning/test_backlog.py tests/unit/planning/test_documentation.py -q

stage "5/9 unit tests"
"$PYTEST" tests/unit -q

stage "6/9 integration tests (real PostgreSQL)"
"$PYTEST" tests/integration -q

stage "7/9 security tests"
"$PYTEST" tests/security -q

stage "8/9 end-to-end tests"
"$PYTEST" tests/e2e -q

stage "9/9 AI evaluation (with regression gate)"
"$PY" evals/runner.py --config evals/config.yaml --check-regression

printf '\n\033[1;32mCI PASSED\033[0m\n'
