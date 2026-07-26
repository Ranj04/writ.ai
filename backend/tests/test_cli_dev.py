from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from writai.cli import run

WORKSPACE_ID = "refund-operations"
SESSION_ID = "SESSION-7f2a"
ASSIGNMENT_ID = "ASSIGNMENT-TASK-102"
TASK_ID = "TASK-102"


def _response(payload: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _bound_session(**overrides: Any) -> dict[str, Any]:
    session: dict[str, Any] = {
        "session_id": SESSION_ID,
        "source": "explicit-attach",
        "workspace_id": WORKSPACE_ID,
        "assignment_id": ASSIGNMENT_ID,
        "task_id": TASK_ID,
        "cwd": "/repo",
        "branch": "feat/TASK-102-csv-export",
        "registered_at": "2026-07-24T21:00:00Z",
        "acknowledged_decision_ids": [],
    }
    session.update(overrides)
    return session


def _unbound_session(**overrides: Any) -> dict[str, Any]:
    session: dict[str, Any] = {
        "session_id": "SESSION-loose",
        "source": "unbound",
        "workspace_id": None,
        "assignment_id": None,
        "task_id": None,
        "cwd": "/elsewhere",
        "branch": "main",
        "registered_at": "2026-07-24T21:05:00Z",
        "acknowledged_decision_ids": [],
    }
    session.update(overrides)
    return session


def _assignment(**overrides: Any) -> dict[str, Any]:
    assignment: dict[str, Any] = {
        "id": ASSIGNMENT_ID,
        "task_id": TASK_ID,
        "task_title": "Export refunds to CSV",
        "agent_name": "csv-export-agent",
        "runtime_provider": "claude-code",
        "execution_mode": "simulated",
        "run_id": "RUN-102-17",
        "state": "interrupted",
        "scopes": ["payments.refunds.execution", "payments.refunds.reporting"],
        "authorized_actions": ["Export the approved refund rows."],
        "plan_id": "PLAN-REFUND-001",
        "decision_snapshot": "graph-v17",
        "interrupt_reason": "The approved refund execution policy changed.",
        "redirect_instruction": "Re-export using the graph-v18 refund rules.",
        "provenance_path": [
            "DEC-REFUND-002",
            "SPEC-REFUND",
            "TICKET-PAY-104",
            TASK_ID,
        ],
        "interrupt_enforced": True,
    }
    assignment.update(overrides)
    return assignment


def _invalidation_report() -> dict[str, Any]:
    return {
        "graph_version": "graph-v18",
        "changed_decision_id": "DEC-REFUND-002",
        "superseded_decision_id": "DEC-REFUND-001",
        "affected_scopes": ["payments.refunds.execution"],
        "invalidated_task_ids": [TASK_ID],
        "evidence_refs": ["EVIDENCE-DEC-REFUND-002", "EVIDENCE-SLACK-88"],
    }


def _workspace(*assignments: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    workspace: dict[str, Any] = {
        "id": WORKSPACE_ID,
        "status": "change-applied",
        "graph_version": "graph-v18",
        "invalidation_report": _invalidation_report(),
        "supervisor": {"state": "interrupting", "assignments": list(assignments)},
    }
    workspace.update(overrides)
    return workspace


def _never_called(request: httpx.Request) -> httpx.Response:
    pytest.fail(f"unexpected HTTP request: {request.method} {request.url}")


def test_attach_writes_the_local_file_without_any_http_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = run(
        ["dev", "attach", ASSIGNMENT_ID, "--workspace", WORKSPACE_ID],
        transport=httpx.MockTransport(_never_called),
    )

    attach_file = tmp_path / ".writai" / "attach"
    assert exit_code == 0
    assert attach_file.read_text(encoding="utf-8") == f"{ASSIGNMENT_ID}\n"
    output = capsys.readouterr().out
    assert str(attach_file) in output
    assert WORKSPACE_ID in output


def test_attach_rejects_a_control_character_identifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    exit_code = run(
        ["dev", "attach", "ASSIGNMENT\nTASK-102", "--workspace", WORKSPACE_ID],
        transport=httpx.MockTransport(_never_called),
    )

    assert exit_code == 2
    assert not (tmp_path / ".writai").exists()
    assert "INVALID_ASSIGNMENT_ID" in capsys.readouterr().err


def _sessions_and_workspace(
    *sessions: dict[str, Any],
    assignments: tuple[dict[str, Any], ...] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/supervisor/sessions":
            return _response({"sessions": list(sessions)})
        assert request.url.path == f"/live-workspaces/{WORKSPACE_ID}"
        return _response(_workspace(*(assignments or (_assignment(),))))

    return httpx.MockTransport(handler)


def test_status_shows_an_unbound_session_marked_unbound(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(
        ["dev", "status"],
        transport=_sessions_and_workspace(_bound_session(), _unbound_session()),
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    # docs/TERMINAL_OUTPUT_SPEC.md section 4: leaders, not columns.
    assert "2 sessions" in output
    assert f"\u23f9  {SESSION_ID}" in output          # stopped
    assert f"{TASK_ID}" in output
    assert "interrupted" in output
    assert "\u2014  SESSION-loose" in output           # unbound marker
    assert "unbound" in output
    assert "1 of 2 unbound: registered, visible, not enforced." in output


def test_status_survives_a_workspace_that_cannot_be_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/supervisor/sessions":
            return _response({"sessions": [_bound_session(), _unbound_session()]})
        return _response(
            {"error": {"code": "NOT_FOUND", "message": "Unknown workspace."}},
            status_code=404,
        )

    exit_code = run(["dev", "status"], transport=httpx.MockTransport(handler))

    assert exit_code == 0
    output = capsys.readouterr().out
    # The unreadable workspace costs the state column, not the command.
    assert SESSION_ID in output
    assert TASK_ID in output
    assert "\u2014  SESSION-loose" in output
    assert "unbound" in output


def test_status_workspace_filter_still_shows_unbound_sessions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = _bound_session(
        session_id="SESSION-other",
        workspace_id="other-workspace",
        task_id="TASK-999",
        assignment_id="ASSIGNMENT-TASK-999",
    )

    exit_code = run(
        ["dev", "status", "--workspace", WORKSPACE_ID],
        transport=_sessions_and_workspace(
            _bound_session(), other, _unbound_session()
        ),
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert SESSION_ID in output
    assert "SESSION-loose" in output
    assert "SESSION-other" not in output


def test_status_accepts_a_wrapped_binding_payload(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(
        ["dev", "status"],
        transport=httpx.MockTransport(
            lambda _request: _response(
                {
                    "bindings": [
                        {
                            "binding": _unbound_session(),
                            "assignment": None,
                        }
                    ]
                }
            )
        ),
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "\u2014  SESSION-loose" in output
    assert "unbound" in output


def test_status_reports_an_unusable_response_as_an_api_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(
        ["dev", "status"],
        transport=httpx.MockTransport(lambda _request: _response({"unexpected": True})),
    )

    assert exit_code == 2
    assert "INVALID_RESPONSE" in capsys.readouterr().err


def test_why_renders_the_multi_hop_provenance_path_with_arrows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/supervisor/sessions":
            return _response({"sessions": [_bound_session(), _unbound_session()]})
        assert request.url.path == f"/live-workspaces/{WORKSPACE_ID}"
        return _response(_workspace(_assignment()))

    exit_code = run(
        ["dev", "why", SESSION_ID],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 0
    assert requested == ["/supervisor/sessions", f"/live-workspaces/{WORKSPACE_ID}"]
    output = capsys.readouterr().out
    # docs/TERMINAL_OUTPUT_SPEC.md section 3: the path is labelled "Which reached".
    assert "DEC-REFUND-002 → SPEC-REFUND → TICKET-PAY-104 → TASK-102" in output
    assert "Which reached" in output
    assert "Because" in output
    assert "payments.refunds.execution" in output
    # Both the changed scope and this task's own scopes: the intersection is
    # what makes selective invalidation legible rather than a blanket claim.
    assert "payments.refunds.reporting" in output
    assert "The approved refund execution policy changed." in output
    assert "EVIDENCE-DEC-REFUND-002" in output


def test_why_json_output_carries_the_path_and_the_scopes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(
        ["dev", "why", SESSION_ID, "--json"],
        transport=_sessions_and_workspace(_bound_session()),
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provenance_path"][0] == "DEC-REFUND-002"
    assert payload["provenance_path"][-1] == TASK_ID
    assert payload["affected_scopes"] == ["payments.refunds.execution"]
    assert payload["evidence_ref"] == "EVIDENCE-DEC-REFUND-002"
    assert payload["session_id"] == SESSION_ID


def test_why_uses_an_inline_assignment_and_the_only_bound_session(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/supervisor/sessions":
            return _response(
                {
                    "sessions": [
                        {
                            "binding": _bound_session(),
                            "assignment": _assignment(
                                evidence_ref="EVIDENCE-DEC-REFUND-002"
                            ),
                        },
                        {"binding": _unbound_session()},
                    ]
                }
            )
        # `why` also reads the workspace: the invalidation report and task titles
        # are what make the invalidated-beside-still-valid ending possible, and
        # they are never inline on a session entry.
        assert request.url.path == f"/live-workspaces/{WORKSPACE_ID}"
        return _response(_workspace(_assignment()))

    exit_code = run(["dev", "why"], transport=httpx.MockTransport(handler))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert " → " in output
    assert "Re-export using the graph-v18 refund rules." in output


def test_why_refuses_to_explain_an_unbound_session(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(
        ["dev", "why", "SESSION-loose"],
        transport=httpx.MockTransport(
            lambda _request: _response({"sessions": [_unbound_session()]})
        ),
    )

    assert exit_code == 2
    assert "SESSION_UNBOUND" in capsys.readouterr().err


def test_ack_posts_the_decision_id_the_service_reported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    posted: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == "/supervisor/sessions"
            return _response(
                {"sessions": [_bound_session(decision_id="DEC-REFUND-002")]}
            )
        posted.append((request.url.path, json.loads(request.content)))
        return _response(
            {
                "binding": _bound_session(
                    acknowledged_decision_ids=["DEC-REFUND-002"]
                ),
                "correlation_id": "corr-9",
            }
        )

    exit_code = run(
        ["dev", "ack", SESSION_ID],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 0
    # The service route is /acknowledge and resolves the blocking decision
    # itself, so the CLI sends no body.
    assert posted == [
        (f"/supervisor/sessions/{SESSION_ID}/acknowledge", {}),
    ]
    output = capsys.readouterr().out
    assert "Acknowledged DEC-REFUND-002" in output
    assert "Acknowledged decisions: DEC-REFUND-002" in output


def test_ack_refuses_to_invent_a_decision_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/supervisor/sessions"
        return _response({"sessions": [_bound_session()]})

    exit_code = run(
        ["dev", "ack", SESSION_ID],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 2
    error = capsys.readouterr().err
    assert "DECISION_ID_UNRESOLVED" in error
    assert "not blocked by an unacknowledged decision" in error


def test_json_output_is_redacted_and_never_prints_a_grant_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == "/supervisor/sessions"
            return _response({"sessions": [_bound_session(decision_id="DEC-018")]})
        return _response(
            {
                "binding": _bound_session(acknowledged_decision_ids=["DEC-018"]),
                "grant_token": "must-not-leak",
                "grant": {"token": "also-secret", "authorization_id": "AUTH-9"},
            }
        )

    exit_code = run(
        ["dev", "ack", SESSION_ID, "--json"],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 0
    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert payload["grant_token"] == "[REDACTED]"
    assert payload["grant"]["token"] == "[REDACTED]"
    assert payload["grant"]["authorization_id"] == "AUTH-9"
    assert "must-not-leak" not in captured
    assert "also-secret" not in captured


def test_status_json_output_lists_every_session(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(
        ["dev", "status", "--json"],
        transport=httpx.MockTransport(
            lambda _request: _response(
                {"sessions": [_bound_session(), _unbound_session()]}
            )
        ),
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [session["session_id"] for session in payload["sessions"]] == [
        SESSION_ID,
        "SESSION-loose",
    ]
    assert payload["sessions"][1]["source"] == "unbound"


def test_watch_prints_supervisor_transitions_as_they_arrive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = {
        "event": "live-workspace.supervisor.snapshot",
        "data": _workspace(_assignment(state="running", interrupt_reason=None)),
    }
    interrupted = {
        "event": "live-workspace.supervisor.changed",
        "data": _workspace(_assignment()),
    }
    stream = (
        f"data: {json.dumps(snapshot)}\n\n" f"data: {json.dumps(interrupted)}\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/live-workspaces/{WORKSPACE_ID}/events"
        return httpx.Response(
            200,
            text=stream,
            headers={"Content-Type": "text/event-stream"},
        )

    exit_code = run(
        ["dev", "watch", WORKSPACE_ID],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert f"{TASK_ID}\t— → running" in output
    assert f"{TASK_ID}\trunning → interrupted" in output
    assert "reason: The approved refund execution policy changed." in output
    assert "supervisor\t— → interrupting" in output


def test_watch_json_output_emits_one_transition_per_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = {
        "event": "live-workspace.supervisor.snapshot",
        "data": _workspace(_assignment(state="running")),
    }

    exit_code = run(
        ["dev", "watch", WORKSPACE_ID, "--json"],
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                text=f"data: {json.dumps(snapshot)}\n\n",
                headers={"Content-Type": "text/event-stream"},
            )
        ),
    )

    assert exit_code == 0
    records = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line
    ]
    assert [record["kind"] for record in records] == ["supervisor", "assignment"]
    assert records[1]["task_id"] == TASK_ID
    assert records[1]["previous_state"] is None
    assert records[1]["state"] == "running"


def test_transport_failure_returns_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    exit_code = run(
        ["dev", "status"],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "TRANSPORT_ERROR" in captured.err


def test_api_error_returns_two_with_the_service_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = run(
        ["dev", "why", SESSION_ID],
        transport=httpx.MockTransport(
            lambda _request: _response(
                {
                    "error": {
                        "code": "SESSIONS_UNAVAILABLE",
                        "message": "The session registry is unavailable.",
                    }
                },
                status_code=503,
            )
        ),
    )

    assert exit_code == 2
    assert "SESSIONS_UNAVAILABLE" in capsys.readouterr().err
