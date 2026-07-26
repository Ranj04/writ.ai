from __future__ import annotations

import json
from argparse import Namespace
from datetime import UTC, datetime

from writai import cli_approve
from writai.intake.approval import pending_from_workspace


def _workspace() -> dict[str, object]:
    return {
        "id": "csv-exports",
        "pending_proposal_instance_id": "proposal-instance-1",
        "supervisor": {
            "assignments": [
                {
                    "id": "ASSIGNMENT-1",
                    "agent_name": "Priya",
                    "task_title": "Restrict exports",
                },
                {
                    "id": "ASSIGNMENT-2",
                    "agent_name": "Marcus",
                    "task_title": "Keep CSV calculations",
                },
            ]
        },
        "pending_mutation": {
            "decision": {
                "id": "DEC-018",
                "kind": "Decision",
                "title": "Admin-only exports",
                "text": "Exports must be admin-only.",
                "scopes": ["export.authorization"],
                "approval_status": "proposal",
                "authority_role": "approve_compliance",
                "confidence": 0.97,
                "effective_at": datetime(
                    2026, 7, 25, 4, 5, tzinfo=UTC
                ).isoformat(),
                "source_ref": "slack://T1/C1/1",
                "attributes": {
                    "requirements": {
                        "export.authorization": {
                            "audience": "admin_only"
                        }
                    }
                },
            },
            "supersedes_id": "DEC-004",
            "affected_scopes": ["export.authorization"],
        },
    }


def _preview() -> dict[str, object]:
    pending = pending_from_workspace(_workspace())
    assert pending is not None
    return {
        "pending": pending.model_dump(mode="json"),
        "interrupted_assignment_ids": ["ASSIGNMENT-1"],
        "preserved_assignment_ids": ["ASSIGNMENT-2"],
        "interrupted_count": 1,
        "preserved_count": 1,
        "total_assignment_count": 2,
    }


class Client:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def request(self, route, **kwargs):
        self.calls.append(
            {
                "method": route.method,
                "path": route.path,
                **kwargs,
            }
        )
        return self.responses.pop(0)


def test_pending_lists_untrusted_workspace_proposals(
    capsys,
) -> None:
    exit_code = cli_approve.run(
        client=Client([{"workspaces": [_workspace()]}]),
        args=Namespace(
            command="pending",
            workspace=None,
            json=False,
        ),
    )
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DEC-018" in output
    assert "approve_compliance" in output


def test_cli_change_uses_authenticated_proposal_bound_server_path(
    monkeypatch,
    capsys,
) -> None:
    workspace = _workspace()
    pending = pending_from_workspace(workspace)
    assert pending is not None
    approved_workspace = {
        **workspace,
        "pending_mutation": None,
        "supervisor": {
            "assignments": [
                {"id": "ASSIGNMENT-1", "state": "interrupted"},
                {"id": "ASSIGNMENT-2", "state": "running"},
            ]
        },
    }
    client = Client([workspace, _preview(), approved_workspace])
    monkeypatch.setenv(
        "HEXCLAVE_APPROVER_USER_API_KEY",
        "authenticated-user-api-key",
    )
    monkeypatch.setattr("builtins.input", lambda: "y")
    exit_code = cli_approve.run(
        client=client,
        args=Namespace(
            command="change",
            workspace_id="csv-exports",
            decision_id="DEC-018",
            json=False,
        ),
    )

    assert exit_code == 0
    approval_body = client.calls[2]["body"]
    assert isinstance(approval_body, dict)
    assert approval_body["approval_token"] == "authenticated-user-api-key"
    assert approval_body["confirmed_proposal_fingerprint"] == (
        pending.proposal_fingerprint
    )
    assert approval_body["confirmed_proposal_instance_id"] == (
        pending.proposal_instance_id
    )
    assert "approver_user_id" not in approval_body
    assert "actor_role" not in approval_body
    output = capsys.readouterr().out
    assert output.index("⏺ Source: slack://T1/C1/1") < output.index(
        "⏺ Decision delta"
    )
    assert output.index("⏺ Decision delta") < output.index("⏺ Blast radius")
    assert "  Stopping (1)" in output
    assert "  Priya · Restrict exports" in output
    assert "\x1b[32mContinuing (1)\x1b[0m" in output
    assert "\x1b[32mMarcus · Keep CSV calculations\x1b[0m" in output
    assert "Decision change approved" in output


def test_cli_change_fails_closed_without_authenticated_credential(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("HEXCLAVE_APPROVER_USER_API_KEY", raising=False)
    monkeypatch.setenv("WRITAI_HOOK_API_KEY", "hook-auth-is-not-human-auth")
    client = Client([_workspace()])

    exit_code = cli_approve.run(
        client=client,
        args=Namespace(
            command="change",
            workspace_id="csv-exports",
            decision_id="DEC-018",
            json=False,
        ),
    )

    assert exit_code == 2
    assert len(client.calls) == 1
    error = capsys.readouterr().err
    assert "HEXCLAVE_APPROVER_USER_API_KEY" in error
    assert "APPROVAL_AUTHENTICATION_REQUIRED" in error


def test_cli_change_uses_server_preview_counts_and_honors_default_no(
    monkeypatch,
    capsys,
) -> None:
    server_preview = {
        **_preview(),
        "interrupted_count": 7,
        "preserved_count": 2,
        "total_assignment_count": 9,
    }
    client = Client([_workspace(), server_preview])
    monkeypatch.setenv(
        "HEXCLAVE_APPROVER_USER_API_KEY",
        "authenticated-user-api-key",
    )
    monkeypatch.setattr("builtins.input", lambda: "")

    exit_code = cli_approve.run(
        client=client,
        args=Namespace(
            command="change",
            workspace_id="csv-exports",
            decision_id="DEC-018",
            json=False,
        ),
    )

    assert exit_code == 0
    assert len(client.calls) == 2
    output = capsys.readouterr().out
    assert "\x1b[38;5;214m7 of 9 stopping\x1b[0m" in output
    assert "\x1b[32m2 continuing\x1b[0m" in output
    assert output.index("  Stopping (7)") < output.index(
        "\x1b[32mContinuing (2)\x1b[0m"
    )
    assert "Approve this decision? [y/N] " in output
    assert output.endswith("Approval cancelled.\n")


def test_cli_json_pending_is_machine_readable(capsys) -> None:
    exit_code = cli_approve.run(
        client=Client([{"workspaces": [_workspace()]}]),
        args=Namespace(
            command="pending",
            workspace=None,
            json=True,
        ),
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["decision_id"] == "DEC-018"
