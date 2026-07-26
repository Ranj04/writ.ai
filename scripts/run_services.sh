#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"

PYTHONPATH=backend "$PYTHON_BIN" -m uvicorn dragback.services.authority_api:app --port 8001 &
AUTH_PID=$!
PYTHONPATH=backend "$PYTHON_BIN" -m uvicorn dragback.services.agent_api:app --port 8002 &
AGENT_PID=$!
PYTHONPATH=backend "$PYTHON_BIN" -m uvicorn dragback.services.executor_api:app --port 8003 &
EXECUTOR_PID=$!

cleanup() {
  kill "$AUTH_PID" "$AGENT_PID" "$EXECUTOR_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
wait
