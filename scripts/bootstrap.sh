#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
# `ci`, not `install`: the lockfile is committed and CI installs from it, so a
# first-time clone must resolve the same tree rather than a newer one that turns
# `make check` red for a reason the repository did not choose.
npm --prefix frontend ci

echo "Bootstrap complete. Run: make check && make stack"
