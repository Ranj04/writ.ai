# shellcheck shell=bash
#
# Shared configuration and output helpers for the Dragback demo launcher.
#
# Sourced, never executed. Bash and python3 standard library only: no jq, no yq,
# no tmux requirement. Every optional tool is detected, never assumed.
#
# The rule that governs this whole directory: a script may report that something
# is missing, but it may not crash because something is missing. At 2am the
# operator needs to know what is armed, not a stack trace.

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$DEMO_DIR/../.." && pwd)"

STATE_DIR="$DEMO_DIR/state"
LOG_DIR="$DEMO_DIR/logs"
RECORDING_DIR="$DEMO_DIR/recordings"
PROMPT_DIR="$DEMO_DIR/prompts"

# Session directories live OUTSIDE the repository on purpose. A demo session
# started inside DragBack would inherit the repo's CLAUDE.md and start reading
# the implementation contract instead of doing its canned work.
DEMO_ROOT="${DRAGBACK_DEMO_ROOT:-$(cd "$REPO_DIR/.." && pwd)/dragback-demo}"

# reset.sh refuses to delete a directory tree that does not carry this marker,
# so a mistyped DRAGBACK_DEMO_ROOT can never take somebody's work with it.
DEMO_ROOT_MARKER=".dragback-demo-root"

SESSION_MANIFEST="$STATE_DIR/sessions.tsv"
SERVICE_PID_FILE="$STATE_DIR/service-pids"
AGENT_PID_FILE="$STATE_DIR/agent-pids"
RECORDER_PID_FILE="$STATE_DIR/recorder-pid"

# --------------------------------------------------------------------------- #
# The contract this launcher is built against
# --------------------------------------------------------------------------- #

DEMO_HOST="127.0.0.1"
# The contract fixes these three ports and the hook's default endpoint points at
# 8002, so the defaults are what the demo runs on. They are overridable only so a
# second checkout on the same machine can rehearse without stopping the first
# one's services — reset.sh only ever touches the ports it was told about.
AUTHORITY_PORT="${DRAGBACK_DEMO_AUTHORITY_PORT:-8001}"
AGENT_PORT="${DRAGBACK_DEMO_AGENT_PORT:-8002}"
EXECUTOR_PORT="${DRAGBACK_DEMO_EXECUTOR_PORT:-8003}"

AUTHORITY_URL="http://$DEMO_HOST:$AUTHORITY_PORT"
AGENT_URL="http://$DEMO_HOST:$AGENT_PORT"
EXECUTOR_URL="http://$DEMO_HOST:$EXECUTOR_PORT"

# The shipped five-session fixture, proven by backend/tests/test_five_session_demo.py.
WORKSPACE_ID="csv-exports"
WORKSPACE_FIXTURE="examples/dragback-five-sessions.yaml"
CHANGE_FIXTURE="examples/dragback-five-sessions-change.yaml"
CHANGE_DECISION_ID="DEC-018"
BASELINE_ROLE="approve_product"
CHANGE_ROLE="approve_compliance"

# The scope that must survive the change, and the scope that must not.
PRESERVED_SCOPE="export.generation"
INTERRUPTED_SCOPE="export.authorization"

DEFAULT_SESSIONS="5"
TMUX_SESSION="dragback-demo"

DEMO_API="$DEMO_DIR/demo_api.py"

# --------------------------------------------------------------------------- #
# Hook authentication
#
# Every /supervisor/sessions route authenticates and fails closed with
# 503 HOOK_AUTHENTICATION_NOT_CONFIGURED when the service holds no key
# (services/supervisor_api.py). Three processes need the SAME value and they
# read it from three different places:
#
#   the services   -> os.environ, populated from .env by load_dotenv() in
#                     config.py, which runs at import before the router is built
#   demo_api.py    -> the bare environment; it never loads .env
#   the hooks      -> the `env` block of each session's settings.json
#
# So a key present in only one of those places arms a demo in which every
# session is denied — which is safe, and indistinguishable from the product not
# working. Resolve it once here, export it, and let all three inherit it.
#
# Precedence: the shell wins, then .env, then the local demo default. reset.sh
# resolves DRAGBACK_WORKSPACE_STORE the same way and for the same reason.
DEMO_HOOK_API_KEY="dragback-demo-hook-key"
HOOK_API_KEY_SOURCE="default"

if [[ -n "${DRAGBACK_HOOK_API_KEY:-}" ]]; then
  HOOK_API_KEY_SOURCE="environment"
else
  hook_key_configured=""
  if [[ -f "$REPO_DIR/.env" ]]; then
    # One key, read with a literal pattern — never eval the .env file.
    hook_key_configured="$(grep -E '^[[:space:]]*DRAGBACK_HOOK_API_KEY=' "$REPO_DIR/.env" 2>/dev/null \
      | tail -n 1 | cut -d= -f2- | tr -d '"'"'"' \r' || true)"
  fi
  if [[ -n "$hook_key_configured" ]]; then
    DRAGBACK_HOOK_API_KEY="$hook_key_configured"
    HOOK_API_KEY_SOURCE=".env"
  else
    DRAGBACK_HOOK_API_KEY="$DEMO_HOOK_API_KEY"
  fi
  unset hook_key_configured
fi
export DRAGBACK_HOOK_API_KEY

# --------------------------------------------------------------------------- #
# Output — legible from fifteen feet, so short lines and no dense logs
# --------------------------------------------------------------------------- #

if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_YELLOW=$'\033[33m'; C_CYAN=$'\033[36m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""
  C_GREEN=""; C_RED=""; C_YELLOW=""; C_CYAN=""
fi

RULE="────────────────────────────────────────────────────────────"

banner() {
  printf '\n%s%s%s\n' "$C_CYAN" "$RULE" "$C_RESET"
  printf '%s  %s%s\n' "$C_BOLD" "$1" "$C_RESET"
  printf '%s%s%s\n\n' "$C_CYAN" "$RULE" "$C_RESET"
}

step() { printf '\n%s>> %s%s\n' "$C_BOLD" "$1" "$C_RESET"; }
say()  { printf '   %s\n' "$1"; }
note() { printf '   %s%s%s\n' "$C_DIM" "$1" "$C_RESET"; }

ok()   { printf '   %s[ OK ]%s  %s\n' "$C_GREEN" "$C_RESET" "$1"; }
warn() { printf '   %s[WARN]%s  %s\n' "$C_YELLOW" "$C_RESET" "$1"; }
bad()  { printf '   %s[FAIL]%s  %s\n' "$C_RED" "$C_RESET" "$1"; }

have() { command -v "$1" >/dev/null 2>&1; }

# --------------------------------------------------------------------------- #
# Interpreters
# --------------------------------------------------------------------------- #

# The services need python >= 3.11 and the repo ships a .venv built with 3.12.
# The helper scripts in this directory stay 3.9-compatible so they run on the
# system python of a stock macOS when the venv is missing.
service_python() {
  if [[ -n "${DRAGBACK_DEMO_PYTHON:-}" ]]; then
    printf '%s\n' "$DRAGBACK_DEMO_PYTHON"
  elif [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
    printf '%s\n' "$REPO_DIR/.venv/bin/python"
  else
    printf '%s\n' "python3"
  fi
}

helper_python() {
  if have python3; then printf 'python3\n'; else service_python; fi
}

# `dragback ...`, however it is installed here.
dragback_cli() {
  if [[ -x "$REPO_DIR/.venv/bin/dragback" ]]; then
    printf '%s\n' "$REPO_DIR/.venv/bin/dragback"
  elif have dragback; then
    command -v dragback
  else
    printf '\n'
  fi
}

# Run one dragback command against the local agent service. Never fatal on its
# own: the caller decides what a failure means.
run_dragback() {
  local cli
  cli="$(dragback_cli)"
  if [[ -z "$cli" ]]; then
    local python_bin
    python_bin="$(service_python)"
    ( cd "$REPO_DIR" && PYTHONPATH=backend "$python_bin" -m dragback.cli \
        --agent-url "$AGENT_URL" "$@" )
    return $?
  fi
  ( cd "$REPO_DIR" && "$cli" --agent-url "$AGENT_URL" "$@" )
}

# Approve the workspace baseline without a Hexclave token.
#
# `dragback workspace approve-baseline` is disabled on purpose — Lane B routes
# every approval through an authenticated envelope so the approver's permission
# is verified — and its replacement is a browser UI that needs a token no local
# demo has. `approve_in_process.py` calls the same orchestrator method the route
# calls, bypassing the channel authentication and no authority check, and it
# refuses unless DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1 is set.
approve_baseline_in_process() {
  local workspace_id="$1" role="$2" python_bin
  python_bin="$(service_python)"
  ( cd "$REPO_DIR" && PYTHONPATH=backend "$python_bin" \
      "$DEMO_DIR/approve_in_process.py" "$workspace_id" "$role" )
}

# The same seam for the pending decision change. `fire.sh` only reaches this
# after `dragback approve change` has printed the blast radius, taken the
# operator's confirmation, and failed to resolve an approver identity.
approve_change_in_process() {
  local workspace_id="$1" role="$2" decision_id="$3" python_bin
  python_bin="$(service_python)"
  ( cd "$REPO_DIR" && PYTHONPATH=backend "$python_bin" \
      "$DEMO_DIR/approve_in_process.py" "$workspace_id" "$role" "$decision_id" )
}

# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #

health_ok() {
  curl --fail --silent --show-error --max-time 2 "$1/health" >/dev/null 2>&1
}

# wait_for_health LABEL URL TIMEOUT_SECONDS
# Every wait has a timeout and says so on expiry — that is the whole point.
wait_for_health() {
  local label="$1" url="$2" timeout="$3"
  local waited=0
  while (( waited < timeout * 4 )); do
    if health_ok "$url"; then
      return 0
    fi
    sleep 0.25
    waited=$((waited + 1))
  done
  return 1
}

# A pid on one of our ports that is not one of our services must never be killed.
port_pids() {
  local port="$1"
  have lsof || return 0
  lsof -ti "tcp:$port" -sTCP:LISTEN 2>/dev/null || true
}

is_dragback_service() {
  local pid="$1" command_line
  command_line="$(ps -o command= -p "$pid" 2>/dev/null || true)"
  [[ "$command_line" == *"dragback.services"* ]]
}

# --------------------------------------------------------------------------- #
# Process bookkeeping
# --------------------------------------------------------------------------- #

record_pid() {
  local file="$1" pid="$2"
  mkdir -p "$(dirname "$file")"
  printf '%s\n' "$pid" >> "$file"
}

alive() { kill -0 "$1" 2>/dev/null; }

# stop_pids FILE LABEL — TERM, wait, then KILL. Silent about pids already gone.
stop_pids() {
  local file="$1" label="$2" stopped=0 pid
  [[ -f "$file" ]] || return 0
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    if alive "$pid"; then
      kill "$pid" 2>/dev/null || true
      stopped=$((stopped + 1))
    fi
  done < "$file"
  if (( stopped > 0 )); then
    sleep 1
    while read -r pid; do
      [[ -n "$pid" ]] || continue
      alive "$pid" && kill -9 "$pid" 2>/dev/null || true
    done < "$file"
    say "Stopped $stopped $label."
  fi
  rm -f "$file"
}

live_pids_in() {
  local file="$1" pid count=0
  [[ -f "$file" ]] || { printf '0\n'; return 0; }
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    alive "$pid" && count=$((count + 1))
  done < "$file"
  printf '%s\n' "$count"
}

# --------------------------------------------------------------------------- #
# Demo root safety
# --------------------------------------------------------------------------- #

# Resolve symlinks and `..` so a path comparison means what it looks like.
canonical() {
  "$(helper_python)" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1" \
    2>/dev/null || printf '%s' "$1"
}

# True only for a directory this launcher created and marked. Nothing else is
# ever removed recursively.
is_demo_root() {
  [[ -n "$DEMO_ROOT" && -d "$DEMO_ROOT" && -f "$DEMO_ROOT/$DEMO_ROOT_MARKER" ]]
}

# Claiming a directory as the demo root is what later authorises `rm -rf` on it,
# so the claim is where the guard belongs. DRAGBACK_DEMO_ROOT is an operator
# variable and a typo in it should cost nothing.
mark_demo_root() {
  local resolved parent_home
  resolved="$(canonical "$DEMO_ROOT")"
  parent_home="$(canonical "$HOME")"

  if [[ -z "$resolved" || "$resolved" == "/" ]]; then
    bad "DRAGBACK_DEMO_ROOT resolves to the filesystem root. Refusing."
    return 1
  fi
  if [[ "$resolved" == "$parent_home" ]]; then
    bad "DRAGBACK_DEMO_ROOT is your home directory. Refusing."
    return 1
  fi
  if [[ "$resolved" == "$(canonical "$REPO_DIR")" ]]; then
    bad "DRAGBACK_DEMO_ROOT is the repository itself. Refusing."
    return 1
  fi
  if [[ -d "$resolved" && ! -f "$resolved/$DEMO_ROOT_MARKER" ]]; then
    if [[ -n "$(ls -A "$resolved" 2>/dev/null || true)" ]]; then
      bad "DRAGBACK_DEMO_ROOT already exists and is not empty:"
      say "  $resolved"
      say "reset.sh deletes this tree recursively, so the launcher will not claim"
      say "a directory it did not create. Point it somewhere else."
      return 1
    fi
  fi

  mkdir -p "$DEMO_ROOT"
  printf 'Created by scripts/demo/up.sh. Safe to delete.\n' \
    > "$DEMO_ROOT/$DEMO_ROOT_MARKER"
}

session_dir() { printf '%s/session-%s\n' "$DEMO_ROOT" "$1"; }

# --------------------------------------------------------------------------- #
# Superset — isolated worktrees, one per session
#
# Superset's actual product is running an army of coding agents in isolated git
# worktrees on one machine, which is what makes five parallel sessions
# manageable on stage. When its CLI is present we ask it for the worktrees and
# run our sessions inside them; when it is not, plain directories under
# DEMO_ROOT do the same job with no isolation between branches.
#
# Grounded in the published CLI reference (docs.superset.sh/cli/cli-reference):
#
#   superset workspaces create --project <id> --name <name> --branch <branch>
#                              [--base-branch <b>] (--local | --host <id>) [--json]
#   superset workspaces delete <id...>
#
# `--project` has no default, so a project id must be supplied. `--agent` and
# `--prompt` are deliberately NOT used: Superset would launch the agent itself,
# bypassing this launcher's hook configuration and canned prompt. We want the
# worktree, and we start the session in it ourselves.
#
# The host server must be running (`superset start`) and the CLI authenticated
# (OAuth in ~/.superset/config.json, or SUPERSET_API_KEY).
# --------------------------------------------------------------------------- #

SUPERSET_PROJECT="${DRAGBACK_DEMO_SUPERSET_PROJECT:-}"
SUPERSET_BASE_BRANCH="${DRAGBACK_DEMO_SUPERSET_BASE_BRANCH:-main}"
SUPERSET_WORKSPACE_FILE="$STATE_DIR/superset-workspaces"

# Usable only when the CLI exists AND a project was named. Everything else
# (host down, not authenticated, project wrong) surfaces as a failed create,
# which falls back per session rather than failing the launch.
superset_available() {
  have superset && [[ -n "$SUPERSET_PROJECT" ]]
}

# provision_superset_workspace INDEX TASK_ID
# Echoes "<workspace_id><US><worktree_path>" on success; non-zero on any failure.
provision_superset_workspace() {
  local index="$1" task_id="$2" name branch output
  name="dragback-demo-session-$index"
  branch="dragback-demo/session-$index-$task_id"

  output="$(superset workspaces create \
    --project "$SUPERSET_PROJECT" \
    --name "$name" \
    --branch "$branch" \
    --base-branch "$SUPERSET_BASE_BRANCH" \
    --local \
    --json 2>/dev/null)" || return 1

  [[ -n "$output" ]] || return 1
  printf '%s' "$output" | "$(helper_python)" "$DEMO_API" superset-parse || return 1
}
