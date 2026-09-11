#!/usr/bin/env python3
"""Per-module coverage floors for the modules that carry the product's guarantees.

The total floor lives in ``[tool.coverage.report] fail_under`` and stops the suite
from decaying as a whole. It cannot stop one load-bearing module from decaying while
tests pile up elsewhere, so the seven below get their own floor. Each floor is the
percentage measured when this gate was introduced, minus two, rounded down.

Runs on the ``coverage.json`` the pytest ``addopts`` already write, so ``scripts/check.sh``
and CI enforce identically. Lower a floor only with an entry in
``outputs/OPEN-ITEMS-REGISTER.md`` naming the module and the reason.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

COVERAGE_JSON = Path("coverage.json")

#: Module path suffix -> minimum line coverage, in percent.
FLOORS: dict[str, int] = {
    "writai/grants.py": 95,
    "writai/authority/engine.py": 92,
    "writai/config.py": 97,
    "writai/workspaces/repository.py": 90,
    "writai/workspaces/session_enforcement.py": 89,
    "writai/services/support.py": 86,
    "writai/services/supervisor_api.py": 88,
}


def _percent_for(files: dict[str, dict], module: str) -> float | None:
    """coverage.json keys files by the path pytest saw, e.g. ``backend/writai/grants.py``."""

    for path, entry in files.items():
        if path == module or path.endswith("/" + module):
            return float(entry["summary"]["percent_covered"])
    return None


def main() -> int:
    if not COVERAGE_JSON.exists():
        print(f"FLOOR: {COVERAGE_JSON} not found; run pytest first", file=sys.stderr)
        return 1
    files = json.loads(COVERAGE_JSON.read_text())["files"]
    breached: list[str] = []
    for module, floor in FLOORS.items():
        measured = _percent_for(files, module)
        if measured is None:
            breached.append(f"FLOOR: {module} not present in {COVERAGE_JSON}")
        elif measured < floor:
            breached.append(f"FLOOR: {module} {measured:.1f}% < {floor}%")
    for line in breached:
        print(line)
    if breached:
        return 1
    print(f"coverage floors OK ({len(FLOORS)} modules)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
