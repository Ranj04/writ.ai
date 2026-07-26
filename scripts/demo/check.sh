#!/usr/bin/env bash
#
# writ.ai demo readiness checklist.
#
# One short line per item, green or red, readable at a glance under pressure.
# Exits non-zero only when something is red — a warning is a thing to know, not
# a thing that stops a demo.
#
# This exists so failures are discovered backstage instead of on stage.
set -euo pipefail

# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

FAILURES=0
WARNINGS=0

# The three silent demo-killers, tracked separately from everything else so the
# verdict can name them. Each leaves a stage that LOOKS armed and then does
# nothing, which on the night is indistinguishable from the product not working.
KILLER_UNBOUND=0
KILLER_SNAPSHOT=0
KILLER_SPENT=0
KILLER_LINES=()

pass() { ok "$1"; }
fail() { bad "$1"; FAILURES=$((FAILURES + 1)); }
soft() { warn "$1"; WARNINGS=$((WARNINGS + 1)); }

# A killer is a failure AND is repeated in the closing block. Never a warning.
killer() { fail "$1"; KILLER_LINES+=("$1"); }

HELPER_PYTHON="$(helper_python)"

# reset.sh removes state/, and a checklist that dies on its own scratch
# directory is worse than useless.
mkdir -p "$STATE_DIR"

banner "WRITAI DEMO — READINESS"

# --------------------------------------------------------------------------- #
# 1. Services
# --------------------------------------------------------------------------- #

step "Services"

check_service() {
  local label="$1" url="$2" port="$3" graph
  if graph="$("$HELPER_PYTHON" "$DEMO_API" health "$url" 2>/dev/null)"; then
    if [[ -n "$graph" ]]; then
      pass "$label on $port  (graph $graph)"
    else
      pass "$label on $port"
    fi
    return 0
  fi
  fail "$label is not answering $url/health"
  return 1
}

check_service "authority" "$AUTHORITY_URL" "$AUTHORITY_PORT" || true
agent_up=1
check_service "agent    " "$AGENT_URL" "$AGENT_PORT" || agent_up=0
check_service "executor " "$EXECUTOR_URL" "$EXECUTOR_PORT" || true

# --------------------------------------------------------------------------- #
# 2. Graph and workspace
# --------------------------------------------------------------------------- #

step "Graph"

workspace_status=""
workspace_graph=""
assignment_count=0
invalidated_count=0
# Whether an approved change has already been applied to this workspace. It
# changes what a snapshot mismatch MEANS: leftover state before the fire,
# expected and correct after it.
workspace_fired="no"

if (( agent_up )); then
  workspace_facts="$("$HELPER_PYTHON" "$DEMO_API" workspace "$AGENT_URL" "$WORKSPACE_ID" 2>/dev/null || true)"
  if [[ "$workspace_facts" == *"found=yes"* ]]; then
    workspace_status="$(sed -n 's/^status=//p' <<< "$workspace_facts")"
    workspace_graph="$(sed -n 's/^graph_version=//p' <<< "$workspace_facts")"
    assignment_count="$(sed -n 's/^assignment_count=//p' <<< "$workspace_facts")"
    invalidated_count="$(sed -n 's/^invalidated_count=//p' <<< "$workspace_facts")"
    pass "workspace $WORKSPACE_ID seeded at ${workspace_graph:-unknown}  (status: ${workspace_status:-unknown})"
    if (( assignment_count > 0 )); then
      pass "$assignment_count supervisor assignment(s)"
    else
      fail "the workspace carries no supervisor assignments"
    fi
    case "$workspace_status" in
      authorized) pass "initial plan authorized and waiting" ;;
      imported|baseline-approved)
        fail "workspace is only '$workspace_status' — run scripts/demo/up.sh" ;;
      "") soft "workspace status is unknown" ;;
      *)
        # Red, not amber. Deny-once is per ASSIGNMENT: an already-fired workspace
        # carries advanced decision_snapshots, so firing again allows every
        # session and the demo reads as broken. Catch it backstage.
        workspace_fired="yes"
        fail "workspace is at '$workspace_status' — it has ALREADY been fired"
        say "        Rehearsing from here silently allows every session."
        say "        Run scripts/demo/reset.sh, then up.sh."
        if (( invalidated_count > 0 )); then
          say "        $invalidated_count task(s) are already invalidated."
        fi
        ;;
    esac
  else
    fail "workspace $WORKSPACE_ID is not seeded — run scripts/demo/up.sh"
  fi
else
  fail "cannot read the graph: the agent service is down"
fi

# --------------------------------------------------------------------------- #
# 2b. Assignments — the two silent demo-killers
#
# Both of these leave a workspace that LOOKS armed and then does nothing on
# stage, which is indistinguishable from the product being broken:
#
#   snapshot mismatch  an assignment pinned to an older graph version than the
#                      workspace is state that survived a previous run.
#   deny already spent deny-once is per ASSIGNMENT. An assignment that has
#                      already been interrupted or redirected will not be
#                      interrupted again, so firing allows it straight through.
#
# Neither is a warning. Both are red.
# --------------------------------------------------------------------------- #

step "Assignments"

if (( agent_up )) && [[ -n "$workspace_graph" ]]; then
  assignments_file="$STATE_DIR/assignments-check.tsv"
  if "$HELPER_PYTHON" "$DEMO_API" assignments "$AGENT_URL" "$WORKSPACE_ID" \
    > "$assignments_file" 2>/dev/null; then
    stale=0
    spent=0
    ready=0
    while IFS=$'\037' read -r assignment_id task_id agent_name state scopes snapshot title requirement; do
      [[ -n "$task_id" ]] || continue
      # A snapshot behind the graph is only evidence of leftover state BEFORE
      # anything is fired. After the fire it is the correct and expected shape:
      # the workspace moved to graph-v18 and the interrupted assignments are
      # still pinned to v17, which is exactly what makes them deny.
      #
      # `continuing` is exempt in both cases. `_apply_supervisor_invalidation`
      # moves a scope-preserved sibling RUNNING -> CONTINUING *without*
      # advancing its snapshot, so a preserved sibling is behind BY DESIGN.
      # Flagging it sends the operator into a re-seed to fix the one thing that
      # is working -- and it is the survivor the whole proof rests on.
      if [[ -n "$snapshot" && "$snapshot" != "$workspace_graph" \
            && "$state" != "continuing" && "$workspace_fired" != "yes" ]]; then
        killer "$task_id is pinned to $snapshot but the graph is $workspace_graph"
        say "        Nothing has been fired yet, so this is state from an earlier run."
        stale=$((stale + 1))
        KILLER_SNAPSHOT=$((KILLER_SNAPSHOT + 1))
        continue
      fi
      case "$state" in
        queued|running|continuing)
          ready=$((ready + 1))
          ;;
        interrupted)
          # Armed, not spent: the hook denies on this every time and only then
          # advances to `redirected`. Pre-fire it means somebody already fired,
          # which the workspace status above has already reported.
          ready=$((ready + 1))
          ;;
        redirected|resumed)
          killer "$task_id is '$state' — its deny-once is already spent"
          say "        Firing again would allow this session straight through."
          spent=$((spent + 1))
          KILLER_SPENT=$((KILLER_SPENT + 1))
          ;;
        completed)
          killer "$task_id is already '$state' — this workspace has been used"
          spent=$((spent + 1))
          KILLER_SPENT=$((KILLER_SPENT + 1))
          ;;
        *)
          killer "$task_id is in unexpected state '$state'"
          spent=$((spent + 1))
          KILLER_SPENT=$((KILLER_SPENT + 1))
          ;;
      esac
    done < "$assignments_file"

    if (( stale == 0 && spent == 0 )); then
      pass "$ready assignment(s) pinned to $workspace_graph with an unspent deny"
    else
      say "        Run scripts/demo/reset.sh, then up.sh. Nothing else clears this."
    fi
  else
    fail "could not read supervisor assignments to check their snapshots"
  fi
else
  soft "assignments not checked: no graph version to compare against"
fi

# --------------------------------------------------------------------------- #
# 3. Sessions
# --------------------------------------------------------------------------- #

step "Sessions"

expected=0
if [[ -f "$SESSION_MANIFEST" ]]; then
  expected="$(wc -l < "$SESSION_MANIFEST" | tr -d ' ')"
else
  soft "no session manifest — scripts/demo/up.sh has not armed this machine"
fi

registered_file="$STATE_DIR/registered.tsv"
registered=0
if (( agent_up )); then
  mkdir -p "$STATE_DIR"
  if "$HELPER_PYTHON" "$DEMO_API" sessions "$AGENT_URL" > "$registered_file" 2>/dev/null; then
    registered="$(wc -l < "$registered_file" | tr -d ' ')"
    if (( expected > 0 )) && (( registered >= expected )); then
      pass "$registered session(s) registered (expected $expected)"
    elif (( expected > 0 )); then
      fail "$registered session(s) registered, expected $expected"
    else
      soft "$registered session(s) registered"
    fi
  else
    fail "GET /supervisor/sessions did not answer with a session list"
    : > "$registered_file"
  fi
else
  : > "$registered_file"
fi

if (( expected > registered )) \
  && grep -qs "monthly spend limit" "$LOG_DIR"/session-*.log 2>/dev/null; then
  fail "Claude Code refused the demo sessions: monthly spend limit reached"
  note "Raise/reset the Claude Code usage limit, then reset.sh and up.sh."
fi

# The three silent demo-killers, checked per REGISTERED SESSION — including
# sessions this launcher did not create, and sessions bound into a workspace
# this check is not otherwise looking at.
#
# The state comes from `GET /supervisor/sessions`, which reads it through the
# same gateway the PreToolUse hook reads. Inferring it here instead would let
# the readiness check and the hook disagree, and the hook is the one that
# decides on the night.
while IFS=$'\037' read -r session_id task_id assignment_id source cwd \
    decision_id bound snapshot_current deny_spent state snap graph; do
  [[ -n "$session_id" ]] || continue
  where="${cwd:-unknown}"

  # 1. Unbound. Allowed everything, never interrupted — a session that silently
  #    ignores the approved change while looking exactly like a working one.
  if [[ -z "$assignment_id" || "$bound" == "no" ]]; then
    killer "session $session_id is UNBOUND — it is allowed everything"
    say "        cwd: $where"
    say "        Nothing will interrupt it. Bind it or stop it before the demo."
    KILLER_UNBOUND=$((KILLER_UNBOUND + 1))
    continue
  fi

  # 2. Deny already spent. Deny-once is per assignment: this session has been
  #    through the cycle and the next fire allows it straight through. This is
  #    the one that is genuinely invisible — the session looks alive and simply
  #    never stops.
  #
  #    `interrupted` is NOT in this set. It is the armed state: the hook denies
  #    on it every time, and only then advances to `redirected`. Verified
  #    against a running service.
  if [[ "$deny_spent" == "yes" ]]; then
    killer "session $session_id has already spent its deny (${state:-unknown})"
    say "        cwd: $where"
    say "        Firing would allow it straight through. Re-seed before the demo."
    KILLER_SPENT=$((KILLER_SPENT + 1))
    continue
  fi

  # 3. Snapshot mismatch before anything has been fired. This launcher arms with
  #    every assignment pinned to the current graph version, so a mismatch here
  #    is state that survived an earlier run and the blast radius will be wrong.
  #
  #    After a fire a mismatch is EXPECTED and correct — the workspace has moved
  #    to graph-v18 and the interrupted assignments are still pinned to v17,
  #    which is precisely what makes them deny. So this only applies pre-fire.
  #    (scripts/demo/seed.py seeds and fires in one step, so a stage armed that
  #    way is post-fire from the start and lands here legitimately.)
  if [[ "$snapshot_current" == "no" && "$workspace_fired" != "yes" ]]; then
    killer "session $session_id is pinned to ${snap:-an unknown snapshot}, graph is ${graph:-unknown}"
    say "        cwd: $where"
    say "        Nothing has been fired yet, so this is state from an earlier run."
    say "        Only a full re-seed clears it: reset.sh, then up.sh."
    KILLER_SNAPSHOT=$((KILLER_SNAPSHOT + 1))
  fi
done < "$registered_file"

# Blank columns mean an older service that does not report the state. Silence is
# not evidence, so say so rather than letting the checklist read green on it.
if (( registered > 0 )) && ! cut -d$'\037' -f7 "$registered_file" | grep -q '[a-z]'; then
  soft "the service did not report per-session binding state; only the assignment scan applied"
fi

if [[ -f "$SESSION_MANIFEST" ]] && (( expected > 0 )); then
  while IFS=$'\037' read -r index task_id directory verdict detail; do
    case "$verdict" in
      bound) pass "session-$index bound to $task_id  ($detail)" ;;
      wrong-task) fail "session-$index should be $task_id but $detail" ;;
      unbound) fail "session-$index is $detail — it is allowed everything" ;;
      missing) fail "session-$index ($task_id) has not registered" ;;
      *) soft "session-$index: $verdict $detail" ;;
    esac
  done < <("$HELPER_PYTHON" "$DEMO_API" match "$SESSION_MANIFEST" "$registered_file" 2>/dev/null || true)
  # `match` exits non-zero when a session is missing, which the loop above has
  # already reported. Silence with no rows at all is different: it means the
  # helper produced nothing, and a checklist that stays green on no evidence is
  # worse than one that fails.
  matched="$("$HELPER_PYTHON" "$DEMO_API" match "$SESSION_MANIFEST" "$registered_file" 2>/dev/null | wc -l | tr -d ' ' || true)"
  if [[ -z "$matched" || "$matched" == "0" ]]; then
    fail "could not check any session binding against the manifest"
  fi
fi

# --------------------------------------------------------------------------- #
# 4. Hook configuration
# --------------------------------------------------------------------------- #

step "Enforcement"

missing_scripts=0
for script in writai_session_start.py writai_pre_tool_use.py writai_session_end.py; do
  [[ -f "$REPO_DIR/hooks/$script" ]] || missing_scripts=$((missing_scripts + 1))
done
if (( missing_scripts == 0 )); then
  if "$HELPER_PYTHON" -m py_compile "$REPO_DIR"/hooks/writai_*.py >/dev/null 2>&1; then
    pass "hook scripts present and importable by python3"
  else
    fail "hooks/ does not compile under $HELPER_PYTHON"
  fi
else
  fail "$missing_scripts hook script(s) missing from hooks/"
fi

if [[ -f "$SESSION_MANIFEST" ]] && (( expected > 0 )); then
  configured=0
  bound_files=0
  while IFS=$'\037' read -r index task_id assignment_id agent_name scopes directory prompt_index title requirement; do
    [[ -f "$directory/.claude/settings.json" ]] && configured=$((configured + 1))
    [[ -f "$directory/.writai/task" ]] && bound_files=$((bound_files + 1))
  done < "$SESSION_MANIFEST"
  if (( configured == expected )); then
    pass "hook config present in all $expected session directories"
  else
    fail "hook config missing in $((expected - configured)) of $expected session directories"
  fi
  if (( bound_files == expected )); then
    pass ".writai/task written in all $expected session directories"
  else
    fail ".writai/task missing in $((expected - bound_files)) of $expected session directories"
  fi
fi

# --------------------------------------------------------------------------- #
# 5. The approval path
# --------------------------------------------------------------------------- #

step "Approval"

cli="$(writai_cli)"
if [[ -n "$cli" ]]; then
  pass "writai CLI at $cli"
else
  soft "no writai console script; falling back to python -m writai.cli"
fi

if run_writai approve --help >/dev/null 2>&1; then
  approve_output="$(run_writai approve pending 2>&1 || true)"
  if [[ "$approve_output" == *"NOT_IMPLEMENTED"* ]]; then
    soft "writai approve exists but is not implemented yet (Lane B)"
    note "This was true when Lane C was written; it is implemented now."
  else
    pass "writai approve is runnable"
  fi
else
  fail "writai approve is not runnable"
fi

# The fire path is `writai approve change`, NOT `workspace approve-change`.
# The latter is disabled on purpose: it took --role on trust, and Lane B
# replaced it with a command that resolves the approver through the real
# permission check. Probing the disabled one would report the fire path as
# broken exactly when it is correct.
if run_writai approve change --help >/dev/null 2>&1; then
  pass "fire path available: workspace propose-change + approve change"
else
  fail "the fire path (approve change) is not runnable"
fi

# The baseline has no local authenticated path at all, so arming depends on the
# in-process seam and its opt-in. A missing opt-in stops up.sh dead, and finding
# that out backstage is the entire point of this checklist.
if [[ -x "$DEMO_DIR/approve_in_process.py" || -f "$DEMO_DIR/approve_in_process.py" ]]; then
  if [[ "${WRITAI_DEMO_UNAUTHENTICATED_APPROVAL:-}" == "1" ]]; then
    pass "baseline approval opt-in set (channel auth bypassed, demo only)"
  else
    soft "WRITAI_DEMO_UNAUTHENTICATED_APPROVAL is not set"
    note "up.sh cannot approve the baseline without it. export it before arming."
  fi
else
  fail "scripts/demo/approve_in_process.py is missing — up.sh cannot arm"
fi

if [[ -f "$REPO_DIR/$CHANGE_FIXTURE" ]]; then
  pass "change fixture $CHANGE_FIXTURE"
else
  fail "missing $CHANGE_FIXTURE — there is nothing to fire"
fi

# --------------------------------------------------------------------------- #
# 6. CrustData fallback
# --------------------------------------------------------------------------- #

step "CrustData fallback"

crustdata_output="$(
  cd "$REPO_DIR" \
    && PYTHONPATH=backend "$HELPER_PYTHON" -m writai.crustdata_demo 2>&1
)" || crustdata_output=""
if [[ "$crustdata_output" == *"DOCUMENTATION-RECONSTRUCTED REPLAY, NOT LIVE"* ]] \
  && [[ "$crustdata_output" == *"CrustData API called: no"* ]] \
  && [[ "$crustdata_output" == *"Graph mutated: no"* ]]; then
  pass "deterministic CrustData replay is ready and cannot be mistaken for live"
else
  fail "CrustData fallback rehearsal failed its honesty contract"
fi

# --------------------------------------------------------------------------- #
# 7. Backup
# --------------------------------------------------------------------------- #

step "Backup"

recording_count=0
if [[ -d "$RECORDING_DIR" ]]; then
  recording_count="$(find "$RECORDING_DIR" -type f ! -name '.gitkeep' 2>/dev/null | wc -l | tr -d ' ' || true)"
fi
if (( recording_count > 0 )); then
  pass "$recording_count backup recording(s) — scripts/demo/fallback.sh plays the newest"
else
  soft "no backup recording yet. Record the first clean run: up.sh --record"
fi

# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #

KILLERS=$((KILLER_UNBOUND + KILLER_SNAPSHOT + KILLER_SPENT))

# The killers get the last word, repeated after everything else, because the
# whole point is that they are invisible on stage. A checklist that buries them
# among twenty other lines has not really reported them.
if (( KILLERS > 0 )); then
  banner "STOP — $KILLERS SILENT DEMO-KILLER(S)"
  say "These do not look like failures on stage. They look like the product"
  say "not working. Nothing here is a warning."
  printf '\n'
  if (( KILLER_UNBOUND > 0 )); then
    bad "UNBOUND SESSIONS ($KILLER_UNBOUND) — allowed everything, never interrupted"
  fi
  if (( KILLER_SNAPSHOT > 0 )); then
    bad "SNAPSHOT MISMATCH ($KILLER_SNAPSHOT) — pinned to a superseded graph version"
  fi
  if (( KILLER_SPENT > 0 )); then
    bad "DENY ALREADY SPENT ($KILLER_SPENT) — firing allows it straight through"
  fi
  printf '\n'
  for line in "${KILLER_LINES[@]}"; do
    note "$line"
  done
  printf '\n'
  say "Fix: scripts/demo/reset.sh, then scripts/demo/up.sh, then run this again."
  say "A partial reset is what produces all three. Do not skip the re-seed."
fi

if (( FAILURES == 0 )); then
  banner "READY  ($WARNINGS warning(s))"
  exit 0
fi

banner "NOT READY — $FAILURES red, $WARNINGS warning(s), $KILLERS silent killer(s)"
exit 1
