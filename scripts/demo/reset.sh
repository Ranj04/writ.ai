#!/usr/bin/env bash
#
# writ.ai demo reset. Ten seconds, idempotent, safe to run twice.
#
# Stops the agent sessions, stops the recorder, stops the three services, removes
# the demo directories, and clears the writ.ai state files so the next `up.sh`
# reseeds `graph-v17` from the shipped fixture.
#
# Rehearsal count is what makes a demo good, and reset time is what caps
# rehearsal count. Nothing in here waits on the network for more than a moment.
set -euo pipefail

# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

KEEP_RECORDINGS=1
usage() {
  cat <<'USAGE'
usage: scripts/demo/reset.sh [--recordings]

  --recordings   Also delete scripts/demo/recordings/. Off by default: a
                 recording is the backup demo, and reset runs a lot.
USAGE
}

while (( $# > 0 )); do
  case "$1" in
    --recordings) KEEP_RECORDINGS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

banner "WRITAI DEMO — RESET"

# Set whenever this run cannot guarantee the next rehearsal starts clean. A
# reset that exits 0 while assignment state survives is the exact failure this
# script exists to prevent, so the exit code carries the guarantee.
RESET_INCOMPLETE=0

# --------------------------------------------------------------------------- #
# 1. Agent sessions
# --------------------------------------------------------------------------- #

step "Agent sessions"

if have tmux && tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
  say "Killed tmux session $TMUX_SESSION."
fi
stop_pids "$AGENT_PID_FILE" "agent session(s)"
say "No demo agent sessions remain."

# --------------------------------------------------------------------------- #
# 2. Recorder — SIGINT so screencapture finalises the file instead of losing it
# --------------------------------------------------------------------------- #

step "Screen recorder"

if [[ -f "$RECORDER_PID_FILE" ]]; then
  recorder_pid="$(head -n 1 "$RECORDER_PID_FILE" 2>/dev/null || true)"
  if [[ -n "$recorder_pid" ]] && alive "$recorder_pid"; then
    kill -INT "$recorder_pid" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      alive "$recorder_pid" || break
      sleep 0.5
    done
    alive "$recorder_pid" && kill -9 "$recorder_pid" 2>/dev/null || true
    say "Stopped the recorder and saved its file."
  fi
  rm -f "$RECORDER_PID_FILE"
fi
say "No recorder is running."

# --------------------------------------------------------------------------- #
# 3. Services
# --------------------------------------------------------------------------- #

step "Services"

stop_pids "$SERVICE_PID_FILE" "service(s) this launcher started"

# A stray uvicorn from an earlier crash still owns the port. Kill it only when
# its command line proves it is a writ.ai service — never a random process
# that happens to be listening on 8001.
for port in "$AUTHORITY_PORT" "$AGENT_PORT" "$EXECUTOR_PORT"; do
  for pid in $(port_pids "$port"); do
    if is_writai_service "$pid"; then
      kill "$pid" 2>/dev/null || true
      say "Stopped a stray writ.ai service on port $port (pid $pid)."
    else
      warn "Port $port is held by pid $pid, which is not a writ.ai service. Left alone."
    fi
  done
done

# Wait for the ports to go quiet rather than assuming they have. A uvicorn that
# has been signalled keeps answering /health for a moment, and up.sh treating a
# dying service as "already healthy" means the rehearsal runs against the state
# still in that process's memory — the store on disk is already gone, so the
# workspace it serves is the OLD one. That is nondeterministic and it looks
# exactly like the demo being broken.
for entry in "authority:$AUTHORITY_URL" "agent:$AGENT_URL" "executor:$EXECUTOR_URL"; do
  label="${entry%%:*}"; url="${entry#*:}"
  waited=0
  while health_ok "$url" && (( waited < 40 )); do
    sleep 0.25
    waited=$((waited + 1))
  done
  if health_ok "$url"; then
    bad "The $label service is STILL answering after 10s."
    say "Its in-memory graph survives, so the next rehearsal would not be clean."
    RESET_INCOMPLETE=1
  fi
done
if (( RESET_INCOMPLETE == 0 )); then
  say "Ports $AUTHORITY_PORT / $AGENT_PORT / $EXECUTOR_PORT are clear."
fi

# --------------------------------------------------------------------------- #
# 4. Demo directories and git worktrees
# --------------------------------------------------------------------------- #

step "Demo directories"

# Any git worktree registered under the demo root, removed through git so the
# repository's worktree list does not rot.
if have git && [[ -d "$REPO_DIR/.git" ]]; then
  while read -r line; do
    [[ "$line" == worktree\ * ]] || continue
    worktree_path="${line#worktree }"
    case "$worktree_path" in
      "$DEMO_ROOT"/*)
        git -C "$REPO_DIR" worktree remove --force "$worktree_path" 2>/dev/null \
          && say "Removed git worktree $worktree_path." || true
        ;;
    esac
  done < <(git -C "$REPO_DIR" worktree list --porcelain 2>/dev/null || true)
  git -C "$REPO_DIR" worktree prune 2>/dev/null || true
fi

# Superset owns its worktrees, so they are removed through Superset — never with
# rm -rf, which would leave its host server holding a workspace whose directory
# has vanished. Ids come from the ledger up.sh wrote; nothing is guessed.
if [[ -f "$SUPERSET_WORKSPACE_FILE" ]]; then
  if have superset; then
    removed_workspaces=0
    while read -r workspace_id; do
      [[ -n "$workspace_id" ]] || continue
      if superset workspaces delete "$workspace_id" >/dev/null 2>&1; then
        removed_workspaces=$((removed_workspaces + 1))
      else
        warn "Could not delete superset workspace $workspace_id."
        say "Remove it yourself: superset workspaces delete $workspace_id"
        RESET_INCOMPLETE=1
      fi
    done < "$SUPERSET_WORKSPACE_FILE"
    (( removed_workspaces > 0 )) && say "Deleted $removed_workspaces superset workspace(s)."
  else
    warn "Superset workspaces were provisioned but the CLI is gone:"
    while read -r workspace_id; do
      [[ -n "$workspace_id" ]] && say "  $workspace_id"
    done < "$SUPERSET_WORKSPACE_FILE"
    RESET_INCOMPLETE=1
  fi
  rm -f "$SUPERSET_WORKSPACE_FILE"
fi

if is_demo_root; then
  # Guarded: only a directory carrying the marker this launcher wrote is ever
  # removed recursively, and the path was constructed in lib.sh.
  rm -rf "$DEMO_ROOT"
  say "Removed $DEMO_ROOT."
elif [[ -d "$DEMO_ROOT" ]]; then
  warn "$DEMO_ROOT exists but has no $DEMO_ROOT_MARKER marker. Left untouched."
else
  say "No demo directories to remove."
fi

# --------------------------------------------------------------------------- #
# 5. writ.ai state — named files, never a blind recursive delete
#
# This section is what makes a second rehearsal work. Deny-once is per
# ASSIGNMENT, not per session: once an assignment has been interrupted and its
# decision_snapshot advanced, it stays advanced, and re-running the demo against
# surviving state silently ALLOWS every session — which on stage reads as the
# product failing. So the workspace store must be gone before up.sh re-imports,
# and this script says out loud whether it is.
# --------------------------------------------------------------------------- #

step "writ.ai state"

# The store is configurable, and a store this script does not know about is
# exactly how a partial reset happens. Resolve it the way the services will:
# environment first, then the repo's .env, then the documented default.
store_relative=".writai/live-workspaces.json"
store_source="default"
if [[ -n "${WRITAI_WORKSPACE_STORE:-}" ]]; then
  store_relative="$WRITAI_WORKSPACE_STORE"
  store_source="WRITAI_WORKSPACE_STORE"
elif [[ -f "$REPO_DIR/.env" ]]; then
  # One key, read with a literal pattern — never eval the .env file.
  configured="$(grep -E '^[[:space:]]*WRITAI_WORKSPACE_STORE=' "$REPO_DIR/.env" 2>/dev/null \
    | tail -n 1 | cut -d= -f2- | tr -d '"'"'"' \r' || true)"
  if [[ -n "$configured" ]]; then
    store_relative="$configured"
    store_source=".env"
  fi
fi

removed=0
for relative in \
  "$store_relative" \
  ".writai/live-workspaces.json" \
  ".writai/hook-verdict-cache.json" \
  ".writai/callwright-attempts.json" \
  ".writai/attach" \
  ".writai/task"; do
  case "$relative" in
    /*) target="$relative" ;;
    *)  target="$REPO_DIR/$relative" ;;
  esac
  # Compare canonical paths: `../../elsewhere` starts with "$REPO_DIR/" as a
  # string while pointing outside the repository entirely.
  target="$(canonical "$target")"
  if [[ "$target" != "$(canonical "$REPO_DIR")/"* ]]; then
    # An absolute store outside the repo is the operator's own path; naming it is
    # safer than deleting it, and a stale one there is the partial-reset trap.
    if [[ -e "$target" ]]; then
      bad "Workspace store is outside the repo and was NOT deleted:"
      say "  $target"
      say "Delete it yourself, or the next rehearsal reuses its assignment state."
      RESET_INCOMPLETE=1
    fi
    continue
  fi
  if [[ -e "$target" ]]; then
    rm -f "$target"
    removed=$((removed + 1))
  fi
done

say "Cleared $removed writ.ai state file(s) (store from $store_source)."

# Anything still in .writai/ is state this script does not recognise. Say so
# rather than deleting it blind — and rather than pretending the reset was total.
if [[ -d "$REPO_DIR/.writai" ]]; then
  leftovers="$(ls -A "$REPO_DIR/.writai" 2>/dev/null || true)"
  if [[ -n "$leftovers" ]]; then
    warn "Unrecognised state left in .writai/ — check it before rehearsing:"
    while IFS= read -r entry; do
      [[ -n "$entry" ]] && say "  .writai/$entry"
    done <<< "$leftovers"
  fi
  rmdir "$REPO_DIR/.writai" 2>/dev/null || true
fi

# The guarantee, verified rather than asserted.
case "$store_relative" in
  /*) store_check="$store_relative" ;;
  *)  store_check="$REPO_DIR/$store_relative" ;;
esac
if [[ -e "$store_check" ]]; then
  bad "The workspace store still exists: $store_check"
  bad "The next rehearsal would reuse advanced assignments and silently allow."
  RESET_INCOMPLETE=1
else
  ok "Workspace store gone — up.sh re-seeds graph-v17 and fresh assignments."
fi

rm -f "$SESSION_MANIFEST"
rm -rf "$STATE_DIR"
if [[ -d "$LOG_DIR" ]]; then
  rm -rf "$LOG_DIR"
  say "Cleared session logs."
fi

if (( KEEP_RECORDINGS == 0 )); then
  if [[ -d "$RECORDING_DIR" ]]; then
    find "$RECORDING_DIR" -type f ! -name ".gitkeep" -delete 2>/dev/null || true
    say "Deleted recordings."
  fi
else
  count=0
  if [[ -d "$RECORDING_DIR" ]]; then
    count="$(find "$RECORDING_DIR" -type f ! -name ".gitkeep" 2>/dev/null | wc -l | tr -d ' ' || true)"
  fi
  say "Kept $count recording(s). Delete them with --recordings."
fi

if (( RESET_INCOMPLETE )); then
  banner "RESET INCOMPLETE — do not rehearse yet"
  say "Clear what is listed above, then run scripts/demo/reset.sh again."
  exit 1
fi

banner "RESET COMPLETE — run scripts/demo/up.sh"
exit 0
