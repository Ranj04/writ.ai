#!/usr/bin/env bash
set -euo pipefail
PYTHON_BIN="${PYTHON_BIN:-python3}"

PYTHONPATH=backend "$PYTHON_BIN" -m pytest
"$PYTHON_BIN" scripts/ci/coverage_floors.py
"$PYTHON_BIN" -m ruff check backend
"$PYTHON_BIN" -m mypy backend
"$PYTHON_BIN" -m compileall -q backend
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
