#!/usr/bin/env bash
#
# The human acknowledgement beat.
#
# An assignment invalidated outright — no corrected plan to hand over — is denied
# on *every* tool call until a person acknowledges the decision. That is rule 5
# of the verdict endpoint, and it is deliberate: without a correction to deliver,
# releasing the session automatically would be writ.ai deciding on its own.
#
# So this script is a person's decision, made once, out loud. `up.sh` and
# `fire.sh` never call it. It reads which sessions are actually blocked from the
# service and acknowledges only those — it never invents a decision id, and it
# never acknowledges a decision that is not blocking anyone.
set -euo pipefail

# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

ASSUME_YES=0
TARGETS=()

usage() {
  cat <<'USAGE'
usage: scripts/demo/ack.sh [SESSION_ID ...] [--yes]

  With no SESSION_ID, acknowledges every session the service reports as
  currently blocked and awaiting a human.

  --yes   Skip the confirmation. For a rehearsal script, not for stage.

After the acknowledgement the session's next tool call is allowed, and the agent
can apply the correction to the work it has already written.
USAGE
}

while (( $# > 0 )); do
  case "$1" in
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; exit 2 ;;
    *) TARGETS+=("$1"); shift ;;
  esac
done

banner "ACKNOWLEDGE — release a blocked session"

HELPER_PYTHON="$(helper_python)"

if ! health_ok "$AGENT_URL"; then
  bad "The agent service is not answering. Nothing to acknowledge."
  exit 1
fi

mkdir -p "$STATE_DIR"
sessions_file="$STATE_DIR/registered.tsv"
if ! "$HELPER_PYTHON" "$DEMO_API" sessions "$AGENT_URL" > "$sessions_file" 2>/dev/null; then
  bad "Could not read the session list from the agent service."
  exit 1
fi

# Only sessions the service says are blocked. The decision id comes from the
# service, never from this script.
blocked=()
while IFS=$'\037' read -r session_id task_id assignment_id source cwd decision_id; do
  [[ -n "$decision_id" ]] || continue
  if (( ${#TARGETS[@]} > 0 )); then
    wanted=0
    for target in "${TARGETS[@]}"; do
      [[ "$target" == "$session_id" ]] && wanted=1
    done
    (( wanted )) || continue
  fi
  blocked+=("$session_id"$'\037'"$task_id"$'\037'"$decision_id")
done < "$sessions_file"

if (( ${#blocked[@]} == 0 )); then
  if (( ${#TARGETS[@]} > 0 )); then
    warn "None of the named sessions is currently blocked."
  else
    warn "No session is waiting on an acknowledgement."
  fi
  note "A session interrupted with a corrected plan is denied once and continues"
  note "on its own; only an outright-invalidated one waits for a person."
  exit 0
fi

for entry in "${blocked[@]}"; do
  IFS=$'\037' read -r session_id task_id decision_id <<< "$entry"
  say "$task_id  blocked by $decision_id  (session $session_id)"
done
printf '\n'

if (( ASSUME_YES == 0 )); then
  read -r -p "   Acknowledge ${#blocked[@]} session(s)? [y/N] " answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) say "Nothing was acknowledged."; exit 0 ;;
  esac
fi

failures=0
for entry in "${blocked[@]}"; do
  IFS=$'\037' read -r session_id task_id decision_id <<< "$entry"
  if run_writai dev ack "$session_id" >/dev/null 2>&1; then
    ok "$task_id acknowledged $decision_id — its next tool call is allowed"
  else
    bad "$task_id could not be acknowledged. Try: writai dev ack $session_id"
    failures=$((failures + 1))
  fi
done

printf '\n'
if (( failures > 0 )); then
  banner "PARTIALLY ACKNOWLEDGED — $failures failed"
  exit 1
fi
banner "ACKNOWLEDGED — the agents can correct their own work now"
exit 0
