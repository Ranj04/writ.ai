#!/usr/bin/env bash
set -euo pipefail
PYTHONPATH=backend "${PYTHON_BIN:-python3}" -m dragback.demo
