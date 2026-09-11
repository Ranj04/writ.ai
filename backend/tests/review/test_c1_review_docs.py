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


def test_finding_f2_the_no_hexclave_baseline_path_arms_the_documented_workspace() -> None:
    """FINDING F2 (fails today).

    Both documents replace ``approve-baseline`` with ``scripts/demo/up.sh`` inside
    the ``refund-operations`` walkthrough. ``scripts/demo/lib.sh`` hard-codes
    ``WORKSPACE_ID="csv-exports"`` and ``WORKSPACE_FIXTURE="examples/writai-five-sessions.yaml"``,
    so ``up.sh`` imports and approves a different workspace, then launches Claude
    Code sessions. ``refund-operations`` stays ``imported`` and the next documented
    line, ``writai workspace authorize refund-operations``, cannot succeed.
    """

    lib = (REPO_ROOT / "scripts/demo/lib.sh").read_text(encoding="utf-8")
    demo_workspace = re.search(r'^WORKSPACE_ID="([^"]+)"', lib, re.MULTILINE)
    assert demo_workspace is not None
    for relative in DOC_PATHS:
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        for block in _fenced_blocks(text):
            if "scripts/demo/up.sh" not in block:
                continue
            authorized = re.findall(r"writai workspace authorize (\S+)", block)
            for workspace_id in authorized:
                assert workspace_id == demo_workspace.group(1), (
                    f"{relative}: the block tells the reader to run scripts/demo/up.sh "
                    f"and then authorize {workspace_id!r}, but up.sh only approves "
                    f"the baseline of {demo_workspace.group(1)!r}"
                )


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
