#!/usr/bin/env bash
#
# Run the pull-request authorization backstop locally, before pushing.
#
# This is the same logic the GitHub check runs — same binding rules, same
# verdict order, same failure text — so a branch that passes here passes there,
# against the same service state.
#
# It fails closed. An unreachable agent service is a failure, not a skip: the
# PreToolUse hook already fails open, and this check exists to close that hole.
#
#   scripts/ci/verify.sh                        # check the current branch
#   scripts/ci/verify.sh --branch feat/X        # check another branch name
#   scripts/ci/verify.sh --json                 # machine-readable verdict
#   scripts/ci/verify.sh --require-binding      # treat an unbound branch as failure
#   scripts/ci/verify.sh --allow-missing-grant  # tolerate a bound branch with no grant
#   scripts/ci/verify.sh --self-test            # run this check's own tests
#
# A bound branch whose workspace has issued no grant FAILS by default. An
# unbound branch still passes: it returns before the grant is consulted, so a
# docs PR is unaffected.
#
# Configuration (environment):
#   WRITAI_AGENT_URL          base URL of the agent service (default :8002)
#   WRITAI_CI_TIMEOUT_SECONDS HTTP timeout, seconds (default 10)
#   WRITAI_CI_API_KEY         optional API key, sent as a request header
#
# Exit codes: 0 authorized · 1 not authorized · 2 unreachable · 3 usage.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK="${SCRIPT_DIR}/writai_ci_check.py"
TESTS="${SCRIPT_DIR}/test_writai_ci_check.py"

# Prefer a real interpreter over whatever "python" happens to mean today. The
# check is standard-library only and 3.9-compatible, so any of these work.
PYTHON="${WRITAI_PYTHON:-}"
if [[ -z "${PYTHON}" ]]; then
  for candidate in python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      PYTHON="${candidate}"
      break
    fi
  done
fi

if [[ -z "${PYTHON}" ]]; then
  echo "writai: no python interpreter found; cannot verify. Failing closed." >&2
  exit 3
fi

if [[ ! -f "${CHECK}" ]]; then
  echo "writai: ${CHECK} is missing; cannot verify. Failing closed." >&2
  exit 3
fi

if [[ "${1:-}" == "--self-test" ]]; then
  exec "${PYTHON}" "${TESTS}"
fi

# Run from the repository root so `.writai/task` and `.writai/attach`
# resolve the way they do for a session started at the top of the checkout.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

set +e
"${PYTHON}" "${CHECK}" --repo-root "${REPO_ROOT}" "$@"
STATUS=$?
set -e

case "${STATUS}" in
  0) ;;
  1)
    echo >&2
    echo "writai: this branch is not authorized to merge. Re-plan against the" >&2
    echo "          line above, or re-authorize the workspace, then run this again." >&2
    ;;
  2)
    echo >&2
    echo "writai: could not reach the agent service, so authorization could not" >&2
    echo "          be established. Start it (make agent) or set WRITAI_AGENT_URL." >&2
    ;;
  3)
    echo >&2
    echo "writai: the check could not run. See the message above." >&2
    ;;
esac

exit "${STATUS}"
