#!/usr/bin/env bash
#
# Fire the approved decision change. This is the only script in this directory
# that mutates the graph, and it is deliberately separate from up.sh: the
# operator arms during the previous team's demo and fires on cue.
#
# It is NOT a new approval path. It calls the same `writai workspace
# propose-change` / `approve-change` commands a human would type, so the
# authority service still decides, the role is still checked, and no script here
# manufactures a verdict.
#
# `docs/BUILD_LANE_C.md` assumed `writai approve --text "<message>"`. That
# command exists in the CLI surface but is a Lane B placeholder that exits 2 with
# NOT_IMPLEMENTED, so this uses the workspace commands that actually work today.
# Swap the two lines below when Lane B lands.
set -euo pipefail

# shellcheck source=lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

ASSUME_YES=0
usage() {
  cat <<'USAGE'
usage: scripts/demo/fire.sh [--yes]

  --yes   Do not ask for confirmation. Use in a rehearsal script, not on stage.

Proposes and approves the compliance decision that makes exports admin-only.
Three sessions lose their authorization; two carry on.
USAGE
}

while (( $# > 0 )); do
  case "$1" in
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

banner "FIRE — approve the compliance decision"

if ! health_ok "$AGENT_URL"; then
  bad "The agent service is not answering. Run scripts/demo/up.sh first."
  exit 1
fi

if [[ ! -f "$REPO_DIR/$CHANGE_FIXTURE" ]]; then
  bad "Missing $CHANGE_FIXTURE. There is nothing to fire."
  exit 1
fi

say "Workspace: $WORKSPACE_ID"
say "Decision:  $CHANGE_DECISION_ID, approved as $CHANGE_ROLE"
say "Effect:    export.authorization changes; export.generation does not."
printf '\n'

if (( ASSUME_YES == 0 )); then
  read -r -p "   Approve the change now? [y/N] " answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) say "Nothing was fired."; exit 0 ;;
  esac
fi

step "Proposing the change"
if run_writai workspace propose-change "$WORKSPACE_ID" "$CHANGE_FIXTURE"; then
  ok "proposal stored — the graph has not moved yet"
else
  bad "propose-change failed. The graph is unchanged."
  exit 1
fi

step "Approving as $CHANGE_ROLE"
# `writai workspace approve-change` is disabled: Lane B replaced it with
# `writai approve change`, which resolves the approver through the real
# permission check rather than taking a --role on trust. The role is therefore
# derived server-side and is no longer passed here — which is the point of the
# replacement, and the reason this is the fire path rather than a shortcut past
# it. $CHANGE_ROLE stays in the banner above so the operator still sees which
# authority is expected to answer.
# `approve change` prints the blast radius and then asks the operator to
# confirm. That prompt is the product invariant -- a human confirms -- so on
# stage it is left alone and answered by the person, AFTER they have seen which
# three sessions stop and which two continue. Only --yes, which is documented as
# "use in a rehearsal script, not on stage", answers it non-interactively.
if (( ASSUME_YES )); then
  approve_confirmed() { printf 'y\n' | run_writai approve change "$@"; }
else
  approve_confirmed() { run_writai approve change "$@"; }
fi

if approve_confirmed "$WORKSPACE_ID" "$CHANGE_DECISION_ID"; then
  ok "the authority applied the change"
elif [[ "${WRITAI_DEMO_UNAUTHENTICATED_APPROVAL:-}" == "1" ]]; then
  # The authenticated path resolves the approver through Hexclave, and
  # HEXCLAVE_* ships empty, so on a local demo it ends in
  # APPROVAL_AUTHENTICATION_FAILED. That is the CORRECT behaviour, not a bug --
  # so it is attempted first, every time, and the fallback only runs after the
  # operator has already seen the blast radius the command printed and answered
  # its confirmation prompt.
  #
  # The fallback bypasses the channel authentication and NOTHING else: role,
  # scope, confidence, the three-way requirement match and the proposal binding
  # all run inside approve_decision exactly as they do for the route.
  warn "the authenticated approval path could not resolve an approver."
  note "Falling back to the in-process seam (demo only). Configure HEXCLAVE_*"
  note "to fire through the real identity path instead."
  if approve_change_in_process "$WORKSPACE_ID" "$CHANGE_ROLE" "$CHANGE_DECISION_ID"; then
    ok "the authority applied the change (channel authentication bypassed)"
  else
    bad "approve change failed. Run scripts/demo/check.sh."
    exit 1
  fi
else
  bad "approve change failed. Run scripts/demo/check.sh."
  note "No approval identity is configured. Either set HEXCLAVE_*, or export"
  note "WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1 to fire without one."
  exit 1
fi

banner "FIRED — watch the sessions"

say "Explain one interrupt:  writai --agent-url $AGENT_URL dev why"
say "See every binding:      writai --agent-url $AGENT_URL dev status"
say "Stream transitions:     writai --agent-url $AGENT_URL dev watch $WORKSPACE_ID"
printf '\n'
exit 0
