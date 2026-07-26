#!/usr/bin/env bash
#
# Dragback demo launcher — arm everything, then wait.
#
# Starts the three services, seeds the five-session workspace, creates one
# directory per session with its `.dragback/task` binding and its Claude Code
# hook configuration, launches a Claude Code session in each with a canned
# prompt, and then STOPS.
#
# It never triggers the change. Arming and firing stay separate so the operator
# can arm during the previous team's demo and fire on cue. The fire command is
# printed at the end; running it is a deliberate second act.
set -euo pipefail

# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

SESSIONS="$DEFAULT_SESSIONS"
RECORD=0
LAUNCH_AGENTS=1

usage() {
  cat <<'USAGE'
usage: scripts/demo/up.sh [--sessions N] [--record] [--no-agents]

  --sessions N   How many Claude Code sessions to arm (default 5).
                 Three land on export.authorization and two on
                 export.generation, scaled if N is not 5.
  --record       Start a screen recording alongside the launch (macOS).
  --no-agents    Arm the services, workspace and directories, but start no
                 Claude Code sessions. Useful for a dry preflight.

up.sh never approves the decision change. It prints the fire command and stops.
USAGE
}

while (( $# > 0 )); do
  case "$1" in
    --sessions)
      if (( $# < 2 )); then echo "--sessions needs a number." >&2; usage >&2; exit 2; fi
      SESSIONS="$2"; shift 2 ;;
    --sessions=*) SESSIONS="${1#*=}"; shift ;;
    --record) RECORD=1; shift ;;
    --no-agents) LAUNCH_AGENTS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if ! [[ "$SESSIONS" =~ ^[0-9]+$ ]] || (( SESSIONS < 1 )); then
  echo "--sessions must be a positive whole number." >&2
  exit 2
fi

banner "DRAGBACK DEMO — ARMING $SESSIONS SESSION(S)"

mkdir -p "$STATE_DIR" "$LOG_DIR" "$RECORDING_DIR"

# --------------------------------------------------------------------------- #
# 0. Refuse to arm on top of a live rehearsal
# --------------------------------------------------------------------------- #

if (( $(live_pids_in "$AGENT_PID_FILE") > 0 )) \
  || { have tmux && tmux has-session -t "$TMUX_SESSION" 2>/dev/null; }; then
  bad "Demo sessions from an earlier run are still alive."
  say "Run scripts/demo/reset.sh first. It takes about ten seconds."
  exit 1
fi

# --------------------------------------------------------------------------- #
# 1. Preflight
# --------------------------------------------------------------------------- #

step "Preflight"

PYTHON_BIN="$(service_python)"
HELPER_PYTHON="$(helper_python)"
# The hooks run inside the demo sessions, whose PATH is not necessarily this
# shell's. Embed an absolute interpreter rather than the bare name `python3`.
HOOK_PYTHON="$(command -v "$HELPER_PYTHON" 2>/dev/null || printf '%s' "$HELPER_PYTHON")"

preflight_failed=0
if ! have curl; then bad "curl is missing."; preflight_failed=1; else ok "curl"; fi
if ! "$HELPER_PYTHON" -c "import json,urllib.request" >/dev/null 2>&1; then
  bad "python3 cannot run the demo helper."
  preflight_failed=1
else
  ok "python3 ($HELPER_PYTHON)"
fi
if "$PYTHON_BIN" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  ok "service python ($PYTHON_BIN)"
else
  bad "$PYTHON_BIN cannot import fastapi/uvicorn. Run: $PYTHON_BIN -m pip install -e '.[dev]'"
  preflight_failed=1
fi
# Not a pass/fail check — lib.sh always resolves a usable key. It is here so the
# operator can see WHICH key is about to authenticate the hooks, because the
# failure it prevents (503 on every session route, five sessions denied) reads
# on stage as the product being broken rather than as a missing variable.
case "$HOOK_API_KEY_SOURCE" in
  environment) ok "hook api key from the shell environment" ;;
  .env)        ok "hook api key from .env" ;;
  *)           ok "hook api key: local demo default (never leaves this machine)" ;;
esac
if [[ -f "$REPO_DIR/$WORKSPACE_FIXTURE" ]]; then
  ok "fixture $WORKSPACE_FIXTURE"
else
  bad "missing $WORKSPACE_FIXTURE"
  preflight_failed=1
fi
HOOKS_PRESENT=1
for hook_script in dragback_session_start.py dragback_pre_tool_use.py dragback_session_end.py; do
  [[ -f "$REPO_DIR/hooks/$hook_script" ]] || HOOKS_PRESENT=0
done
if (( HOOKS_PRESENT )); then
  ok "hook scripts in hooks/"
else
  # Degrading means running WITHOUT enforcement, not writing a settings file that
  # names a script which does not exist: Claude Code then reports a hook error on
  # every tool call and the session cannot do anything at all. Verified on a
  # checkout without Lane A merged — five sessions started and were bricked.
  warn "hooks/ is incomplete on this checkout."
  warn "Sessions will run with NO Dragback enforcement, and no hook config."
fi
if have claude; then ok "claude $(claude --version 2>/dev/null | head -n 1)"; else
  warn "claude is not on PATH. Directories will be armed but no session starts."
  LAUNCH_AGENTS=0
fi
if have tmux; then ok "tmux (five agent panes plus an operator pane)"; else
  note "tmux is absent. Sessions run in the background and log to scripts/demo/logs/."
fi
if superset_available; then
  ok "superset — sessions get isolated worktrees from project $SUPERSET_PROJECT"
elif have superset; then
  warn "superset is installed but DRAGBACK_DEMO_SUPERSET_PROJECT is not set."
  note "Set it to use isolated worktrees; using plain directories for now."
else
  note "superset is absent. Sessions get plain directories under the demo root."
fi

if (( preflight_failed )); then
  bad "Preflight failed. Nothing was started."
  exit 1
fi

# --------------------------------------------------------------------------- #
# 2. Services
# --------------------------------------------------------------------------- #

step "Services"

start_service() {
  local name="$1" port="$2" url="$3"
  if health_ok "$url"; then
    ok "$name already healthy on $port"
    return 0
  fi
  # `exec` inside the group so the recorded pid IS uvicorn, and so the group's
  # redirection covers every descendant. Without both, the launcher leaves a
  # forked shell holding this script's stdout open and `| tail` never returns.
  (
    cd "$REPO_DIR" || exit 1
    exec nohup env \
      "DRAGBACK_AUTHORITY_URL=$AUTHORITY_URL" \
      "DRAGBACK_AGENT_URL=$AGENT_URL" \
      "DRAGBACK_EXECUTOR_URL=$EXECUTOR_URL" \
      "DRAGBACK_HOOK_API_KEY=$DRAGBACK_HOOK_API_KEY" \
      PYTHONPATH=backend \
      "$PYTHON_BIN" -m uvicorn "dragback.services.${name}_api:app" \
      --host "$DEMO_HOST" --port "$port"
  ) > "$LOG_DIR/$name.log" 2>&1 < /dev/null &
  record_pid "$SERVICE_PID_FILE" "$!"
  if wait_for_health "$name" "$url" 40; then
    ok "$name healthy on $port"
    return 0
  fi
  bad "$name did not answer $url/health within 40s. See scripts/demo/logs/$name.log"
  return 1
}

services_ready=1
start_service authority "$AUTHORITY_PORT" "$AUTHORITY_URL" || services_ready=0
start_service agent "$AGENT_PORT" "$AGENT_URL" || services_ready=0
start_service executor "$EXECUTOR_PORT" "$EXECUTOR_URL" || services_ready=0

if (( services_ready == 0 )); then
  bad "Not every service came up. Arming stops here; run check.sh for detail."
  exit 1
fi

# --------------------------------------------------------------------------- #
# 3. Seed the workspace — import, approve the baseline, authorize the plan
# --------------------------------------------------------------------------- #

step "Workspace $WORKSPACE_ID"

workspace_status() {
  "$HELPER_PYTHON" "$DEMO_API" workspace "$AGENT_URL" "$WORKSPACE_ID" 2>/dev/null \
    | sed -n 's/^status=//p'
}

workspace_present() {
  "$HELPER_PYTHON" "$DEMO_API" workspace "$AGENT_URL" "$WORKSPACE_ID" >/dev/null 2>&1
}

arm_step() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    ok "$label"
    return 0
  fi
  bad "$label failed. Run it by hand to see why:"
  say "  dragback --agent-url $AGENT_URL ${*:3}"
  return 1
}

if ! workspace_present; then
  arm_step "imported $WORKSPACE_FIXTURE" \
    run_dragback workspace import "$WORKSPACE_FIXTURE" || exit 1
else
  ok "workspace already present"
fi

status="$(workspace_status || true)"
case "$status" in
  imported)
    # NOT `dragback workspace approve-baseline`: that command is disabled on
    # purpose, because Lane B routed every approval through an
    # ApprovalAttemptEnvelope carrying a Hexclave-resolvable token so the
    # approver's permission is genuinely verified. Its replacement is the
    # authenticated Workspace UI, which needs a browser and a token no local
    # demo has — so the launcher uses the same in-process seam the seeder uses.
    # It bypasses the channel authentication and no authority check, and it
    # refuses unless DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1 is set.
    #
    # Safe to write the store from a second process here specifically: the
    # repository re-reads the file on every call and replaces it atomically,
    # and at arm time nothing else is writing — no sessions are bound yet and
    # no interrupt can fire. The store is single-writer in general; this is
    # one write in the one window where that holds.
    arm_step "baseline approved as $BASELINE_ROLE" \
      approve_baseline_in_process "$WORKSPACE_ID" "$BASELINE_ROLE" || exit 1
    status="$(workspace_status || true)"
    ;;
esac
case "$status" in
  baseline-approved)
    arm_step "initial plan authorized against graph-v17" \
      run_dragback workspace authorize "$WORKSPACE_ID" || exit 1
    status="$(workspace_status || true)"
    ;;
esac

case "$status" in
  authorized)
    ok "workspace armed and waiting (status: authorized)"
    ;;
  change-proposed|change-applied|initial-grant-rejected|plan-updated|reauthorized|complete)
    # Refuse rather than arm on top of it. Deny-once is per ASSIGNMENT, so these
    # assignments already carry advanced decision_snapshots: firing again would
    # allow every session, which on stage is indistinguishable from the product
    # not working. reset.sh is one second; a silent demo failure is not.
    bad "This workspace has ALREADY been fired (status: $status)."
    say "Arming on top of it would produce a demo where nothing gets interrupted."
    say "Run scripts/demo/reset.sh, then scripts/demo/up.sh."
    exit 1
    ;;
  *)
    # Fail closed on a state we cannot verify. Arming against an unreadable
    # status risks the silent-allow rehearsal this launcher exists to prevent.
    bad "Workspace status is '${status:-unreadable}', which this launcher cannot verify."
    say "Run scripts/demo/reset.sh, then scripts/demo/up.sh."
    exit 1
    ;;
esac

# --------------------------------------------------------------------------- #
# 4. Read the real task ids out of the seeded graph
# --------------------------------------------------------------------------- #

step "Session plan"

ASSIGNMENTS_TSV="$STATE_DIR/assignments.tsv"
if ! "$HELPER_PYTHON" "$DEMO_API" assignments "$AGENT_URL" "$WORKSPACE_ID" \
  > "$ASSIGNMENTS_TSV" 2>/dev/null; then
  bad "Could not read supervisor assignments from the agent service."
  say "The workspace may not have been seeded. Run check.sh."
  exit 1
fi

if ! "$HELPER_PYTHON" "$DEMO_API" plan "$SESSIONS" "$ASSIGNMENTS_TSV" \
  > "$STATE_DIR/plan.tsv" 2>/dev/null; then
  bad "No assignments to bind sessions to."
  exit 1
fi

planned="$(wc -l < "$STATE_DIR/plan.tsv" | tr -d ' ')"
if (( planned < SESSIONS )); then
  warn "Asked for $SESSIONS sessions; the workspace only offers $planned assignments."
  SESSIONS="$planned"
fi

: > "$SESSION_MANIFEST"
rm -f "$SUPERSET_WORKSPACE_FILE"
# The marker authorises reset.sh to delete this tree. Claim it even when
# Superset is provisioning, because any session it cannot provision falls back
# to a plain directory underneath it.
mark_demo_root

index=0
while IFS=$'\037' read -r assignment_id task_id agent_name state scopes snapshot title requirement; do
  [[ -n "$task_id" ]] || continue
  index=$((index + 1))
  directory=""
  if superset_available; then
    # One isolated worktree per session. A failure here is never fatal: the
    # session falls back to a plain directory and the reason is printed once.
    if provisioned="$(provision_superset_workspace "$index" "$task_id")"; then
      IFS=$'\037' read -r superset_id superset_path <<< "$provisioned"
      if [[ -n "$superset_path" && -d "$superset_path" ]]; then
        directory="$superset_path"
        printf '%s\n' "$superset_id" >> "$SUPERSET_WORKSPACE_FILE"
        say "session-$index  superset worktree $superset_id"
      fi
    fi
    if [[ -z "$directory" ]]; then
      warn "superset could not provision session-$index; using a plain directory."
      note "Is the host server running (superset start) and the CLI signed in?"
    fi
  fi
  if [[ -z "$directory" ]]; then
    directory="$(session_dir "$index")"
  fi
  # Which canned prompt this session gets: the task's rank in the FULL
  # assignment list, not its rank in the selection. With --sessions 3 that keeps
  # TASK-203 on the prompt written for TASK-203 instead of shifting everyone.
  prompt_index="$(awk -F'\037' -v want="$task_id" '$2 == want { print NR; exit }' \
    "$ASSIGNMENTS_TSV")"
  [[ -n "$prompt_index" ]] || prompt_index="$index"
  printf '%s\037%s\037%s\037%s\037%s\037%s\037%s\037%s\037%s\n' \
    "$index" "$task_id" "$assignment_id" "$agent_name" "$scopes" "$directory" \
    "$prompt_index" "$title" "$requirement" \
    >> "$SESSION_MANIFEST"
  if [[ ",$scopes," == *",$INTERRUPTED_SCOPE,"* ]]; then
    say "session-$index  $agent_name  $task_id  $scopes  [will be interrupted]"
  else
    say "session-$index  $agent_name  $task_id  $scopes  [must survive]"
  fi
done < "$STATE_DIR/plan.tsv"

# --------------------------------------------------------------------------- #
# 5. Session directories: task binding, hook config, canned prompt
# --------------------------------------------------------------------------- #

step "Session directories"

write_settings() {
  # Generated as real JSON by the helper, never assembled here: a checkout path
  # containing a quote or a backslash would silently corrupt the file that
  # configures enforcement, and the interpreter and script path each have to
  # survive as one shell word.
  #
  # Absolute hook paths on purpose — $CLAUDE_PROJECT_DIR points at the session
  # directory, not at the DragBack checkout the hooks live in. This file is
  # written only inside a generated session directory, which is outside every
  # checkout: nothing here touches a repo .claude/settings.json or the user's.
  local target="$1"
  "$HELPER_PYTHON" "$DEMO_API" settings \
    "$target" "$REPO_DIR" "$AGENT_URL" "$HOOK_PYTHON" \
    ".dragback/hook-verdict-cache.json" "$HOOKS_PRESENT"
}

while IFS=$'\037' read -r index task_id assignment_id agent_name scopes directory prompt_index title requirement; do
  # Never recursively delete a Superset worktree — that path was constructed by
  # Superset, not here, and `superset workspaces delete` is how it goes away.
  case "$directory" in
    "$DEMO_ROOT"/*) rm -rf "$directory" ;;
  esac
  mkdir -p "$directory/.dragback" "$directory/.claude" "$directory/data"

  # The binding itself. The client reports this file; the server resolves it.
  printf '%s\n' "$task_id" > "$directory/.dragback/task"
  write_settings "$directory/.claude/settings.json"

  # The objective and the requirement are quoted from the seeded graph, in the
  # graph's own words. The deny payload names the task by this title and the
  # constraint by this key and value, so a redirect reads as a correction to
  # the work in hand rather than as something misrouted from another task.
  cat > "$directory/NOTES.md" <<NOTES
# $agent_name — $task_id

Objective: $title
Scope: $scopes
Current approved requirement: $requirement
Assignment: $assignment_id
Workspace: $WORKSPACE_ID  (ticket TICKET-100, plan PLAN-027)

Every file in this directory implements that objective under that requirement.
If the approved requirement changes, the files below are what has to change.

Every tool call made here is checked by the Dragback PreToolUse hook first.
NOTES

  cat > "$directory/data/accounts.csv" <<'CSV'
account_id,email,plan,created_at
1001,ada@example.com,pro,2026-01-04
1002,grace@example.com,free,2026-02-11
1003,alan@example.com,pro,2026-03-19
1004,katherine@example.com,team,2026-04-02
CSV

  printf '' > "$directory/progress.log"

  # Prompt files are literal and identical every run. Only the three identity
  # placeholders change, and they come from the seeded graph.
  prompt_source="$PROMPT_DIR/session-$(( (prompt_index - 1) % 5 + 1 )).txt"
  if [[ -f "$prompt_source" ]]; then
    # Literal replacement in the helper. These values come from the workspace
    # API, and a `|`, `&` or backslash in one of them would corrupt a sed
    # program rather than land in the prompt.
    "$HELPER_PYTHON" "$DEMO_API" render-prompt \
      "$prompt_source" "$directory/prompt.txt" \
      "$agent_name" "$task_id" "$scopes" "$title" "$requirement"
  else
    warn "Missing $prompt_source; session-$index gets a minimal prompt."
    printf 'Work on %s (%s) in this directory, under the approved requirement %s. Read NOTES.md, then append one line to progress.log every step.\n' \
      "$task_id" "$title" "$requirement" > "$directory/prompt.txt"
  fi

  ok "session-$index  $directory"
done < "$SESSION_MANIFEST"

# --------------------------------------------------------------------------- #
# 6. Optional screen recording
# --------------------------------------------------------------------------- #

if (( RECORD )); then
  step "Screen recording"
  if [[ "$OSTYPE" == darwin* ]] && have screencapture; then
    stamp="$(date +%Y%m%d-%H%M%S)"
    recording="$RECORDING_DIR/dragback-demo-$stamp.mov"
    nohup screencapture -v "$recording" > "$LOG_DIR/recorder.log" 2>&1 < /dev/null &
    printf '%s\n' "$!" > "$RECORDER_PID_FILE"
    sleep 1
    if alive "$(cat "$RECORDER_PID_FILE")"; then
      ok "recording to $recording"
      note "reset.sh stops it cleanly. Screen Recording permission is required."
    else
      warn "screencapture exited immediately. Grant Screen Recording permission in"
      warn "System Settings > Privacy & Security, then rerun with --record."
      rm -f "$RECORDER_PID_FILE"
    fi
  else
    warn "--record needs macOS screencapture. Recording skipped; everything else is armed."
  fi
fi

# --------------------------------------------------------------------------- #
# 7. Launch the sessions
# --------------------------------------------------------------------------- #

step "Claude Code sessions"

CLAUDE_EXTRA_ARGS="${DRAGBACK_DEMO_CLAUDE_ARGS:-}"

write_launcher() {
  local index="$1" directory="$2" mode="$3" launcher="$STATE_DIR/launch-$index.sh"
  {
    printf '#!/usr/bin/env bash\n'
    printf 'cd %q || exit 1\n' "$directory"
    if [[ "$mode" == "interactive" ]]; then
      printf 'exec claude --permission-mode acceptEdits %s "$(cat %q)"\n' \
        "$CLAUDE_EXTRA_ARGS" "$directory/prompt.txt"
    else
      # No terminal to attach to, so print mode: the session still starts, still
      # fires SessionStart and PreToolUse, and its transcript lands in a log.
      printf 'exec claude -p --permission-mode acceptEdits %s "$(cat %q)"\n' \
        "$CLAUDE_EXTRA_ARGS" "$directory/prompt.txt"
    fi
  } > "$launcher"
  chmod +x "$launcher"
  printf '%s\n' "$launcher"
}

if (( LAUNCH_AGENTS == 0 )); then
  warn "No sessions launched (--no-agents, or claude is not installed)."
  note "Directories are armed. Start one by hand with:"
  note "  cd $(session_dir 1) && claude --permission-mode acceptEdits \"\$(cat prompt.txt)\""
elif have tmux; then
  tmux new-session -d -s "$TMUX_SESSION" -c "$REPO_DIR" 2>/dev/null || true
  # Pane 0 is the operator. It is the only pane that can fire, and it is
  # deliberately not one of the agents.
  tmux send-keys -t "$TMUX_SESSION" \
    "clear; echo 'OPERATOR PANE — the fire command is typed below, press Enter on cue'" C-m
  panes=0
  while IFS=$'\037' read -r index task_id assignment_id agent_name scopes directory prompt_index title requirement; do
    launcher="$(write_launcher "$index" "$directory" interactive)"
    # Re-tile after every split: tmux refuses a split when the target pane is
    # already too small, and one refusal must not abort the launch.
    if tmux split-window -t "$TMUX_SESSION" -c "$directory" \
      "$(printf '%q' "$launcher")" 2>/dev/null; then
      panes=$((panes + 1))
    else
      warn "tmux would not open a pane for session-$index. Make the window bigger,"
      warn "or start it by hand: $launcher"
    fi
    tmux select-layout -t "$TMUX_SESSION" tiled >/dev/null 2>&1 || true
  done < "$SESSION_MANIFEST"
  tmux set-option -t "$TMUX_SESSION" -w pane-border-status top >/dev/null 2>&1 || true
  tmux set-option -t "$TMUX_SESSION" -w pane-border-format \
    ' #{pane_index} #{pane_current_path} ' >/dev/null 2>&1 || true
  tmux select-layout -t "$TMUX_SESSION" tiled >/dev/null 2>&1 || true
  tmux select-pane -t "$TMUX_SESSION.0" >/dev/null 2>&1 || true
  # Typed, not executed. Arming and firing stay separate all the way to the key.
  tmux send-keys -t "$TMUX_SESSION.0" \
    "$(printf '%q' "$DEMO_DIR/fire.sh")" 2>/dev/null || true
  ok "tmux session '$TMUX_SESSION' has one operator pane and $panes agent pane(s)."
  say "Attach with:  tmux attach -t $TMUX_SESSION"
  # Every rehearsal hits this: reset.sh deletes the session directories and this
  # script recreates them, so Claude Code asks to trust each one again. Five
  # panes sitting on a security prompt is how the demo starts badly. A person
  # answers it — this only removes the fumbling.
  printf '\n'
  warn "Each agent pane is waiting on Claude Code's folder-trust prompt."
  say "Accept them one by one, or all at once with:"
  trust_command="tmux"
  for pane in $(seq 1 "$panes"); do
    trust_command="$trust_command send-keys -t $TMUX_SESSION.$pane Enter \\;"
  done
  say "  ${trust_command% \\;}"
else
  while IFS=$'\037' read -r index task_id assignment_id agent_name scopes directory prompt_index title requirement; do
    launcher="$(write_launcher "$index" "$directory" background)"
    # stdin from /dev/null: a print-mode session that inherits a terminal can
    # sit waiting on stdin instead of starting.
    nohup "$launcher" > "$LOG_DIR/session-$index.log" 2>&1 < /dev/null &
    record_pid "$AGENT_PID_FILE" "$!"
    ok "session-$index ($agent_name, $task_id) -> scripts/demo/logs/session-$index.log"
  done < "$SESSION_MANIFEST"
  note "tmux is not installed, so sessions run in print mode in the background."
fi

# --------------------------------------------------------------------------- #
# 8. Armed. Stop here.
# --------------------------------------------------------------------------- #

if (( HOOKS_PRESENT )); then
  banner "ARMED — NOTHING HAS BEEN FIRED"
else
  banner "ARMED WITHOUT ENFORCEMENT — hooks/ is missing on this checkout"
  say "Sessions will run, but no tool call is checked and firing interrupts nobody."
  say "Merge the lane that owns hooks/, then reset.sh and up.sh again."
fi

say "Check readiness:   scripts/demo/check.sh"
say "Fire the change:   scripts/demo/fire.sh"
say "Reset everything:  scripts/demo/reset.sh"
printf '\n'
note "The first launch in a fresh directory may show Claude Code's one-time"
note "folder-trust prompt. Accept it once; the paths are stable across resets."
printf '\n'
exit 0
