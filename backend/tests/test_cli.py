from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from writai.cli import run
from writai.domain import AgentPlan
from writai.workspaces.models import (
    LiveWorkspaceImportRequest,
    WorkspaceProposalRequest,
)

WORKSPACE_ID = "refund workspace"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _response(payload: dict[str, object], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def test_import_reads_yaml_and_never_prints_a_raw_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = tmp_path / "workspace.yaml"
    fixture.write_text(
        """
id: refund-workspace
name: Refund controls
description: Live refund policy
""".strip(),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/live-workspaces/import"
        assert json.loads(request.content) == {
            "id": "refund-workspace",
            "name": "Refund controls",
            "description": "Live refund policy",
        }
        return _response(
            {
                "id": "refund-workspace",
                "name": "Refund controls",
                "status": "draft",
                "signed_grant": {
                    "payload": {"authorization_id": "AUTH-1"},
                    "token": "must-not-leak",
                },
            },
            status_code=201,
        )

    exit_code = run(
        ["workspace", "import", str(fixture)],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Workspace imported" in output
    assert "refund-workspace" in output
    assert "must-not-leak" not in output


def test_json_output_is_redacted_and_options_work_after_the_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "agent.internal"
        return _response(
            {
                "id": "refund-workspace",
                "grant_token": "secret-grant",
                "grant": {
                    "payload": {"authorization_id": "AUTH-1"},
                    "token": "secret-token",
                },
            }
        )

    exit_code = run(
        [
            "workspace",
            "show",
            "refund-workspace",
            "--agent-url",
            "https://agent.internal",
            "--json",
        ],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["grant_token"] == "[REDACTED]"
    assert payload["grant"]["token"] == "[REDACTED]"
    assert payload["grant"]["payload"]["authorization_id"] == "AUTH-1"


@pytest.mark.parametrize(
    ("arguments", "method", "path", "expected_body"),
    [
        (
            ["workspace", "authorize", WORKSPACE_ID],
            "POST",
            "/live-workspaces/refund%20workspace/authorize",
            {},
        ),
    ],
)
def test_mutating_commands_use_the_public_contract(
    arguments: list[str],
    method: str,
    path: str,
    expected_body: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == method
        assert request.url.raw_path.decode() == path
        assert json.loads(request.content) == expected_body
        return _response({"id": WORKSPACE_ID, "status": "ready"})

    assert run(arguments, transport=httpx.MockTransport(handler)) == 0
    assert "ready" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["workspace", "approve-baseline", WORKSPACE_ID],
        [
            "workspace",
            "approve-baseline",
            WORKSPACE_ID,
            "--role",
            "finance-admin",
        ],
        ["workspace", "approve-change", WORKSPACE_ID, "DEC/002"],
        [
            "workspace",
            "approve-change",
            WORKSPACE_ID,
            "DEC/002",
            "--role",
            "finance-admin",
        ],
    ],
)
def test_legacy_role_only_approval_commands_are_disabled_without_http(
    arguments: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        pytest.fail("the deprecated approval command must not make HTTP requests")

    exit_code = run(arguments, transport=httpx.MockTransport(handler))

    assert exit_code == 2
    assert requests == []
    error = capsys.readouterr().err
    assert (
        "authenticated Workspace approval UI" in error
        or "writai approve change" in error
    )
    assert "COMMAND_DEPRECATED" in error


def test_propose_change_sends_the_document_without_rewriting_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = tmp_path / "change.json"
    fixture.write_text(
        json.dumps(
            {
                "decision": {"id": "DEC-002", "kind": "Decision"},
                "supersedes_id": "DEC-001",
                "affected_scopes": ["payments.refunds.execution"],
            }
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/decisions/propose")
        assert json.loads(request.content)["supersedes_id"] == "DEC-001"
        return _response({"id": WORKSPACE_ID, "pending_decision_id": "DEC-002"})

    assert (
        run(
            ["workspace", "propose-change", WORKSPACE_ID, str(fixture)],
            transport=httpx.MockTransport(handler),
        )
        == 0
    )
    assert "DEC-002" in capsys.readouterr().out


def test_cancel_change_deletes_the_pending_proposal_without_a_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == (
            "/live-workspaces/refund-workspace/decisions/pending"
        )
        assert request.content == b""
        return _response(
            {
                "id": "refund-workspace",
                "name": "Refund controls",
                "status": "authorized",
                "graph_version": "graph-v17",
                "pending_mutation": None,
                "current_plan": {"id": "PLAN-001"},
            }
        )

    exit_code = run(
        ["workspace", "cancel-change", "refund-workspace"],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Pending decision change canceled" in output
    assert "Status: authorized" in output
    assert "Graph: graph-v17" in output


def test_cancel_change_supports_redacted_json_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(
        ["workspace", "cancel-change", "refund-workspace", "--json"],
        transport=httpx.MockTransport(
            lambda _request: _response(
                {
                    "id": "refund-workspace",
                    "status": "authorized",
                    "pending_mutation": None,
                    "grant_token": "must-not-leak",
                }
            )
        ),
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "authorized"
    assert payload["pending_mutation"] is None
    assert payload["grant_token"] == "[REDACTED]"


def test_update_plan_wraps_the_plan_then_requests_reauthorization(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = tmp_path / "plan.yaml"
    fixture.write_text(
        """
id: PLAN-002
ticket_id: PAY-104
objective: Add approval
actions: []
""".strip(),
        encoding="utf-8",
    )
    requests: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.method, request.url.path, body))
        if request.method == "PUT":
            return _response({"id": WORKSPACE_ID, "plan_id": "PLAN-002"})
        return _response(
            {
                "id": WORKSPACE_ID,
                "replacement_authorization": {
                    "verdict": "ALLOW",
                    "payload": {"authorization_id": "AUTH-2"},
                },
            }
        )

    exit_code = run(
        ["workspace", "update-plan", WORKSPACE_ID, str(fixture)],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 0
    assert requests == [
        (
            "PUT",
            "/live-workspaces/refund workspace/plan",
            {
                "plan": {
                    "id": "PLAN-002",
                    "ticket_id": "PAY-104",
                    "objective": "Add approval",
                    "actions": [],
                }
            },
        ),
        (
            "POST",
            "/live-workspaces/refund workspace/reauthorize",
            {},
        ),
    ]
    assert "ALLOW" in capsys.readouterr().out


def test_verify_returns_zero_only_for_valid_and_applied(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/grants/replacement/verify")
        assert json.loads(request.content) == {}
        return _response(
            {
                "initial_verification": {
                    "applied": False,
                    "verification_code": "STALE_SNAPSHOT",
                    "reason": "The original grant is stale.",
                },
                "replacement_verification": {
                    "applied": True,
                    "verification_code": "VALID",
                    "reason": "Replacement grant accepted.",
                },
                "graph_version": "graph-v18",
            }
        )

    exit_code = run(
        ["workspace", "verify", "refund-workspace", "--grant", "replacement"],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "VALID — execution applied" in output
    assert "graph-v18" in output


@pytest.mark.parametrize(
    "payload",
    [
        {
            "initial_verification": {
                "applied": False,
                "verification_code": "STALE_SNAPSHOT",
                "reason": "The graph snapshot changed.",
            }
        },
        {
            "initial_verification": {
                "applied": False,
                "verification_code": "VALID",
                "reason": "Verification did not apply the action.",
            }
        },
    ],
)
def test_verify_returns_one_for_deterministic_rejection_or_non_application(
    payload: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(
        ["workspace", "verify", "refund-workspace"],
        transport=httpx.MockTransport(lambda _request: _response(payload)),
    )

    assert exit_code == 1
    assert "REJECTED" in capsys.readouterr().out


def test_verify_maps_a_deterministic_http_error_to_exit_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(
        ["workspace", "verify", "refund-workspace", "--json"],
        transport=httpx.MockTransport(
            lambda _request: _response(
                {
                    "error": {
                        "code": "STALE_SNAPSHOT",
                        "message": "The stored grant is stale.",
                    },
                    "correlation_id": "corr-1",
                },
                status_code=409,
            )
        ),
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "STALE_SNAPSHOT"


def test_transport_failure_returns_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    exit_code = run(
        ["workspace", "list"],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Could not reach" in captured.err
    assert "TRANSPORT_ERROR" in captured.err


def test_invalid_input_returns_two_without_making_a_request(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = tmp_path / "invalid.yaml"
    fixture.write_text("- not\n- an\n- object\n", encoding="utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid input must not make a request")

    exit_code = run(
        ["workspace", "import", str(fixture)],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 2
    assert "top level" in capsys.readouterr().err


def test_distributed_examples_validate_and_preserve_selective_provenance() -> None:
    workspace = LiveWorkspaceImportRequest.model_validate(
        yaml.safe_load(
            (REPO_ROOT / "examples/writai-workspace.yaml").read_text(encoding="utf-8")
        )
    )
    proposal = WorkspaceProposalRequest.model_validate(
        yaml.safe_load(
            (REPO_ROOT / "examples/writai-change.yaml").read_text(encoding="utf-8")
        )
    )
    corrected_plan = AgentPlan.model_validate_json(
        (REPO_ROOT / "examples/writai-corrected-plan.json").read_text(
            encoding="utf-8"
        )
    )

    assert workspace.id == "refund-operations"
    assert proposal.supersedes_id == workspace.baseline_decision.id
    assert proposal.affected_scopes == {"payments.refunds.execution"}
    assert corrected_plan.ticket_id == workspace.ticket.id
    edge_pairs = {
        (edge.source_id, edge.target_id) for edge in workspace.graph_edges()
    }
    assert ("TASK-REFUND-CALCULATE", "PLAN-REFUND-001") in edge_pairs
    assert ("TASK-REFUND-ISSUE", "PLAN-REFUND-001") in edge_pairs


def test_composite_action_installs_and_runs_the_cli_verification_gate() -> None:
    action = yaml.safe_load(
        (
            REPO_ROOT / ".github/actions/writai-verify/action.yml"
        ).read_text(encoding="utf-8")
    )

    assert action["runs"]["using"] == "composite"
    steps = action["runs"]["steps"]
    assert any("pip install" in step.get("run", "") for step in steps)
    verify_command = steps[-1]["run"]
    assert "writai" in verify_command
    assert "workspace verify" in verify_command
    assert "--grant" in verify_command
    assert "token" not in json.dumps(action).casefold()
