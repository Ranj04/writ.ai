"""The demo seeder's unauthenticated-approval path is opt-in, not accidental.

`scripts/demo/seed.py` approves the baseline and the change by calling the
orchestrator directly, so the channel authentication in front of the HTTP
approval routes never runs (ASSUMPTIONS A-5). Every *authority* check still runs
— role, scope, confidence, the three-way requirement match, the proposal binding
— but nobody authenticated the approver, so the path must not be reachable by
running one command in the wrong directory.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = REPO_ROOT / "scripts" / "demo" / "seed.py"


@pytest.fixture(scope="module")
def seed() -> Iterator[ModuleType]:
    """Import the seeder by path. It is a script, not a package.

    Importing it rewrites ``WRITAI_WORKSPACE_STORE`` to the stage store — it
    has to, because `agent_api` builds its repository at import time — so the
    environment is restored afterwards rather than leaked into the rest of the
    suite.
    """

    before = dict(os.environ)
    spec = importlib.util.spec_from_file_location("writai_demo_seed", SEED_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        os.environ.clear()
        os.environ.update(before)
        sys.modules.pop(spec.name, None)


def test_seeding_refuses_without_the_explicit_opt_in(
    seed: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(seed.SEED_UNAUTHENTICATED_APPROVAL_ENV, raising=False)

    with pytest.raises(SystemExit) as raised:
        seed._require_unauthenticated_approval_optin()

    message = str(raised.value)
    assert "refusing to seed" in message
    # The refusal has to be actionable and honest: name the variable, say what
    # is bypassed, and say what is not.
    assert seed.SEED_UNAUTHENTICATED_APPROVAL_ENV in message
    assert "authority check still runs" in message
    assert "nobody authenticated the approver" in message


@pytest.mark.parametrize("value", ["", "0", "no", "true", "yes", " "])
def test_only_an_exact_opt_in_is_accepted(
    seed: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    """A half-set variable must not read as consent."""

    monkeypatch.setenv(seed.SEED_UNAUTHENTICATED_APPROVAL_ENV, value)
    with pytest.raises(SystemExit):
        seed._require_unauthenticated_approval_optin()


def test_the_opt_in_allows_seeding(
    seed: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(seed.SEED_UNAUTHENTICATED_APPROVAL_ENV, "1")
    seed._require_unauthenticated_approval_optin()


def test_the_gate_runs_before_the_stage_is_destroyed(seed: ModuleType) -> None:
    """`seed_graph` deletes the stage directory. The refusal must come first.

    Read from the source rather than executed, because executing it is exactly
    the destructive act under test.
    """

    source = SEED_PATH.read_text(encoding="utf-8")
    body = source.split("def seed_graph(", 1)[1]
    gate = body.index("_require_unauthenticated_approval_optin()")
    destroy = body.index("shutil.rmtree(STAGE)")
    assert gate < destroy


def test_the_documented_commands_carry_the_opt_in() -> None:
    """A runbook that omits it just teaches everyone to hit the refusal."""

    for name in ("docs/STAGE_RUNBOOK.md", "HANDOFF.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines()):
            if "scripts/demo/seed.py" not in line or not line.startswith("  PYTHON"):
                continue
            preceding = text.splitlines()[max(0, line_number - 1)]
            assert "WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1" in preceding, (
                f"{name}:{line_number + 1} invokes the seeder without the opt-in"
            )
