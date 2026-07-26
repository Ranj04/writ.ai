#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  STACK_PYTHON="$PYTHON_BIN"
elif [[ -x ".venv/bin/python" ]]; then
  STACK_PYTHON=".venv/bin/python"
else
  STACK_PYTHON="python3"
fi

for command_name in curl npm; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required command: $command_name" >&2
    exit 1
  fi
done

if ! "$STACK_PYTHON" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "Python dependencies are missing. Run: $STACK_PYTHON -m pip install -e '.[dev]'" >&2
  exit 1
fi

if [[ ! -d "frontend/node_modules" ]]; then
  echo "Frontend dependencies are missing. Run: npm --prefix frontend install" >&2
  exit 1
fi

STACK_HOST="127.0.0.1"
AUTHORITY_PORT="8001"
AGENT_PORT="8002"
EXECUTOR_PORT="8003"
FRONTEND_PORT="5173"
AUTHORITY_URL="http://$STACK_HOST:$AUTHORITY_PORT"
AGENT_URL="http://$STACK_HOST:$AGENT_PORT"
EXECUTOR_URL="http://$STACK_HOST:$EXECUTOR_PORT"
FRONTEND_URL="http://$STACK_HOST:$FRONTEND_PORT"

STACK_BACKEND_ENV=(
  "DRAGBACK_AUTHORITY_URL=$AUTHORITY_URL"
  "DRAGBACK_AGENT_URL=$AGENT_URL"
  "DRAGBACK_EXECUTOR_URL=$EXECUTOR_URL"
  "DRAGBACK_DEMO_RESET_ENABLED=true"
)

env "${STACK_BACKEND_ENV[@]}" PYTHONPATH=backend \
  "$STACK_PYTHON" -m uvicorn dragback.services.authority_api:app \
  --host "$STACK_HOST" --port "$AUTHORITY_PORT" &
AUTHORITY_PID=$!
env "${STACK_BACKEND_ENV[@]}" PYTHONPATH=backend \
  "$STACK_PYTHON" -m uvicorn dragback.services.agent_api:app \
  --host "$STACK_HOST" --port "$AGENT_PORT" &
AGENT_PID=$!
env "${STACK_BACKEND_ENV[@]}" PYTHONPATH=backend \
  "$STACK_PYTHON" -m uvicorn dragback.services.executor_api:app \
  --host "$STACK_HOST" --port "$EXECUTOR_PORT" &
EXECUTOR_PID=$!
env \
  "VITE_AUTHORITY_URL=$AUTHORITY_URL" \
  "VITE_AGENT_URL=$AGENT_URL" \
  "VITE_EXECUTOR_URL=$EXECUTOR_URL" \
  npm --prefix frontend run dev -- \
  --host "$STACK_HOST" --port "$FRONTEND_PORT" --strictPort &
FRONTEND_PID=$!

cleanup() {
  kill "$AUTHORITY_PID" "$AGENT_PID" "$EXECUTOR_PID" "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait_for_url() {
  local label="$1"
  local url="$2"
  local pid="$3"
  local attempt
  for attempt in {1..120}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "$label exited before becoming ready." >&2
      return 1
    fi
    if curl --fail --silent --show-error "$url" >/dev/null 2>&1; then
      sleep 0.1
      if kill -0 "$pid" 2>/dev/null; then
        return 0
      fi
      echo "$label exited while claiming its port." >&2
      return 1
    fi
    sleep 0.25
  done
  echo "$label did not become ready at $url" >&2
  return 1
}

wait_for_url "Intent authority" "$AUTHORITY_URL/health" "$AUTHORITY_PID"
wait_for_url "Agent service" "$AGENT_URL/health" "$AGENT_PID"
wait_for_url "Executor" "$EXECUTOR_URL/health" "$EXECUTOR_PID"
wait_for_url "Frontend" "$FRONTEND_URL/" "$FRONTEND_PID"

echo "Dragback is ready: $FRONTEND_URL/"

while true; do
  for process in \
    "Intent authority:$AUTHORITY_PID" \
    "Agent service:$AGENT_PID" \
    "Executor:$EXECUTOR_PID" \
    "Frontend:$FRONTEND_PID"; do
    label="${process%%:*}"
    pid="${process##*:}"
    if ! kill -0 "$pid" 2>/dev/null; then
      if wait "$pid"; then
        status=0
      else
        status=$?
      fi
      echo "$label exited unexpectedly with status $status." >&2
      exit 1
    fi
  done
  sleep 1
done
