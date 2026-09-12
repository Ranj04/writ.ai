"""Track C round-1 adversarial review tests for C4/C5 documentation truth.

Tests whose name starts with ``test_finding_`` are committed failing tests for
findings in ``.review/c/1/findings.json``. The others are positive controls that
prove the builder's documentation test actually goes red when it should.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import test_cli
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC_PATHS = ("README.md", "docs/LIVE_WORKSPACE_CLI.md")


def _doc_copy_with(tmp_path: Path, extra_readme_lines: str) -> Path:
    (tmp_path / "docs").mkdir()
    for relative in DOC_PATHS:
        shutil.copy(REPO_ROOT / relative, tmp_path / relative)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\n```bash\n" + extra_readme_lines + "\n```\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.parametrize(
    "poison",
    [
        "writai workspace approve-baseline foo --role bar",
        "writai workspace approve-change foo DEC-1 --role bar",
        "writai workspace frobnicate foo",
        "writai approve frobnicate foo",
        "writai agent frobnicate --workspace foo",
    ],
)
def test_the_c4_documentation_test_goes_red_on_a_bad_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poison: str
) -> None:
    monkeypatch.setattr(test_cli, "REPO_ROOT", _doc_copy_with(tmp_path, poison))
    with pytest.raises(AssertionError):
        test_cli.test_documented_workspace_commands_are_not_deprecated()


def test_the_c4_documentation_test_is_green_on_the_real_docs() -> None:
    test_cli.test_documented_workspace_commands_are_not_deprecated()


def _fenced_blocks(text: str) -> list[str]:
    return re.findall(r"```(?:bash|text|sh)?\n(.*?)```", text, re.DOTALL)


_IMPORT_COMMAND = re.compile(r"^\s*writai workspace import (\S+)\s*$", re.MULTILINE)
_BASELINE_APPROVAL_COMMAND = re.compile(
    r"^\s*(?:PYTHONPATH=\S+\s+)?python3?\s+(\S+approve\S*\.py)\s+(\S+)\s+(\S+)\s*$",
    re.MULTILINE,
)
_AUTHORIZE_COMMAND = re.compile(r"^\s*writai workspace authorize (\S+)\s*$", re.MULTILINE)


def _joined_continuations(block: str) -> str:
    return re.sub(r"\\\n\s*", " ", block)


def _no_hexclave_baseline_blocks(text: str) -> list[str]:
    return [
        _joined_continuations(block)
        for block in _fenced_blocks(text)
        if _IMPORT_COMMAND.search(block) and "Without Hexclave" in block
    ]


def test_finding_f2_the_no_hexclave_baseline_path_arms_the_documented_workspace() -> None:
    """FINDING F2 (fixed; RC-2 re-armed the test).

    As found: both documents replaced ``approve-baseline`` with ``scripts/demo/up.sh``
    inside the ``refund-operations`` walkthrough. ``scripts/demo/lib.sh`` hard-codes
    ``WORKSPACE_ID="csv-exports"`` and ``WORKSPACE_FIXTURE="examples/writai-five-sessions.yaml"``,
    so ``up.sh`` imported and approved a different workspace, then launched Claude
    Code sessions. ``refund-operations`` stayed ``imported`` and the next documented
    line, ``writai workspace authorize refund-operations``, could not succeed.

    RC-2: the first version of this test searched the walkthroughs for
    ``scripts/demo/up.sh``. The fix removed every mention, so the test passed while
    asserting nothing. It now asserts the right thing instead of the absence of
    the wrong string: every documented no-Hexclave baseline block must import a
    fixture that exists, approve the baseline of the workspace that fixture
    declares with a script that exists, and authorize that same workspace.
    """

    for relative in DOC_PATHS:
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        blocks = _no_hexclave_baseline_blocks(text)
        assert blocks, f"{relative} no longer documents a no-Hexclave baseline path"
        for block in blocks:
            import_match = _IMPORT_COMMAND.search(block)
            assert import_match is not None
            fixture = REPO_ROOT / import_match.group(1)
            assert fixture.is_file(), f"{relative}: imports {import_match.group(1)!r}, missing"
            imported_id = yaml.safe_load(fixture.read_text(encoding="utf-8"))["id"]

            approvals = _BASELINE_APPROVAL_COMMAND.findall(block)
            assert approvals, (
                f"{relative}: the no-Hexclave block imports {imported_id!r} but gives "
                "no command that approves its baseline"
            )
            for script, workspace_id, _role in approvals:
                assert (REPO_ROOT / script).is_file(), (
                    f"{relative}: the baseline approval command runs {script!r}, "
                    "which does not exist"
                )
                assert workspace_id == imported_id, (
                    f"{relative}: the reader imported {imported_id!r} but the baseline "
                    f"approval command targets {workspace_id!r}"
                )
            for workspace_id in _AUTHORIZE_COMMAND.findall(block):
                assert workspace_id == imported_id, (
                    f"{relative}: the reader imported {imported_id!r} but the block "
                    f"authorizes {workspace_id!r}"
                )


def _repo_copy_with_baseline_target(tmp_path: Path, workspace_id: str) -> Path:
    for relative in (
        *DOC_PATHS,
        "examples/writai-workspace.yaml",
        "scripts/demo/approve_in_process.py",
    ):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / relative, tmp_path / relative)
    readme = tmp_path / "README.md"
    original = readme.read_text(encoding="utf-8")
    poisoned = original.replace(
        "  refund-operations finance-admin\n", f"  {workspace_id} finance-admin\n"
    )
    assert poisoned != original
    readme.write_text(poisoned, encoding="utf-8")
    return tmp_path


def test_the_f2_test_goes_red_when_the_baseline_path_names_an_unimported_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        globals(), "REPO_ROOT", _repo_copy_with_baseline_target(tmp_path, "csv-exports")
    )
    with pytest.raises(AssertionError, match="imported 'refund-operations' but the baseline"):
        test_finding_f2_the_no_hexclave_baseline_path_arms_the_documented_workspace()


def test_finding_f3_documented_approve_change_names_its_required_credential() -> None:
    """FINDING F3 (fails today).

    ``writai approve change`` returns exit 2 with
    ``APPROVAL_AUTHENTICATION_REQUIRED`` unless ``HEXCLAVE_APPROVER_USER_API_KEY``
    is set (``backend/writai/cli_approve.py:791-798``). The walkthrough introduces
    it directly under a "Without Hexclave" path and never names the variable, so
    the reader following the credential-free path hits a wall at step 4.
    """

    for relative in DOC_PATHS:
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        if "writai approve change" not in text:
            continue
        assert "HEXCLAVE_APPROVER_USER_API_KEY" in text, (
            f"{relative} documents `writai approve change` without naming the "
            "credential it exits 2 without"
        )
