"""Hook wiring must stay demo-local.

Installing Dragback's hooks into the user-level settings file would point every
project on the machine at a service that only runs for the demo. Committing them
to the tracked `.claude/settings.json` would enforce for every teammate who pulls.
Both are deliberate decisions to make after the demo, so they are checks, not
comments.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / "hooks"


def _tracked(path: str) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", path],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def test_no_hook_config_is_committed_to_shared_claude_settings() -> None:
    """Not until after the demo. Promoting it is an explicit, separate decision."""

    assert not _tracked(".claude/settings.json")
    assert not _tracked(".claude/settings.local.json")


def test_local_settings_cannot_be_committed_by_accident() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", ".claude/settings.local.json"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0, ".claude/settings.local.json must be gitignored"


def test_the_readme_never_tells_anyone_to_use_user_level_settings() -> None:
    """The user-level path may be named, but only to forbid it."""

    readme = (HOOKS / "README.md").read_text(encoding="utf-8")
    assert ".claude/settings.local.json" in readme
    assert "Never install this into the user-level" in readme

    # No shell instruction may write to a user-level settings file.
    in_block = False
    for line in readme.splitlines():
        if line.startswith("```"):
            in_block = not in_block
            continue
        if not in_block:
            continue
        assert "~/.claude/settings.json" not in line, line
        assert "$HOME/.claude/settings.json" not in line, line
        # And nothing may write to the tracked, shared project settings file.
        if line.strip().startswith(("cp ", "mv ", "tee ", "cat ")):
            assert ".claude/settings.json" not in line, line


def test_the_example_is_settings_shaped_but_installs_nothing() -> None:
    """The examples are documentation. Nothing in the repo applies them."""

    example = json.loads((HOOKS / "settings.example.json").read_text(encoding="utf-8"))
    assert set(example) == {"hooks"}, example.keys()
    managed = json.loads(
        (HOOKS / "managed-settings.example.json").read_text(encoding="utf-8")
    )
    assert managed["allowManagedHooksOnly"] is True


def _hook_commands(name: str) -> list[str]:
    settings = json.loads((HOOKS / name).read_text(encoding="utf-8"))
    return [
        hook["command"]
        for entries in settings["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    ]


def test_every_shipped_example_installs_the_three_lifecycle_hooks() -> None:
    """Both examples must cover SessionStart, PreToolUse and SessionEnd.

    Two hook implementations currently ship (see ASSUMPTIONS.md A-2) and the two
    examples deliberately install different ones, so this pins coverage rather
    than the choice: a managed configuration missing SessionStart registers no
    session, and an unregistered session is allowed everything.
    """

    for name in ("settings.example.json", "managed-settings.example.json"):
        settings = json.loads((HOOKS / name).read_text(encoding="utf-8"))
        assert set(settings["hooks"]) == {
            "SessionStart",
            "PreToolUse",
            "SessionEnd",
        }, name


def test_the_installed_hooks_run_on_the_oldest_supported_python() -> None:
    """A hook that cannot start emits nothing, and Claude Code then ALLOWS.

    These scripts run in the developer's environment, not ours, so `python3` may
    be 3.9. Anything that needs a newer interpreter fails open in exactly the
    place enforcement is supposed to be non-removable.
    """

    referenced = {
        match.group(0)
        for name in ("settings.example.json", "managed-settings.example.json")
        for command in _hook_commands(name)
        for match in [re.search(r"[\w.]+\.py", command)]
        if match
    }
    assert referenced, "no hook scripts referenced by either example"

    # Whichever implementation is installed, it has to be able to START. Compile
    # each one against the oldest interpreter the product supports; a syntax or
    # symbol it cannot parse means no output, and no output means allow.
    oldest = shutil.which("python3.9") or shutil.which("python3")
    for script in sorted(referenced):
        path = HOOKS / script
        assert path.exists(), script
        # IMPORT it, do not merely compile it. `from datetime import UTC` is
        # valid 3.9 syntax and fails only at import — precisely the class of
        # break that leaves a managed hook silently dead.
        loader = (
            "import importlib.util,sys;"
            f"sys.path.insert(0, {str(HOOKS)!r});"
            f"spec=importlib.util.spec_from_file_location('h', {str(path)!r});"
            "m=importlib.util.module_from_spec(spec);"
            # dataclasses resolves field types through sys.modules[__module__];
            # exec'ing an unregistered module makes that lookup return None.
            "sys.modules['h']=m;"
            "spec.loader.exec_module(m)"
        )
        result = subprocess.run(
            [oldest or sys.executable, "-c", loader],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"{script} cannot start on {oldest}: {result.stderr[-400:]}"
        )
