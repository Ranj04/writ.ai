from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from writai.domain import (
    AgentPlan,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    PlanAction,
)
from writai.services import supervisor_api
from writai.services.supervisor_api import (
    HOOK_API_KEY_HEADER,
    HookCredentialVerifier,
    build_supervisor_session_router,
    parse_hook_credentials,
)
from writai.services.support import install_api_support
from writai.workspaces.models import LiveWorkspaceImportRequest, LiveWorkspaceRecord
from writai.workspaces.repository import SqliteLiveWorkspaceRepository
from writai.workspaces.runtimes.claude_code import ClaudeCodeSupervisorRuntime
from writai.workspaces.session_binding import (
    DEFAULT_HOOK_DEVELOPER_ID,
    ClaudeCodeSessionRegistry,
)
from writai.workspaces.session_enforcement import (
    ClaudeCodeSessionEnforcement,
    ClaudePreToolUseRequest,
    RepositorySupervisorAssignmentGateway,
)
from writai.workspaces.supervisor import (
    FixtureSupervisorRuntime,
    SupervisorAssignmentState,
)


def test_supervisor_assignments_carry_current_authorized_work() -> None:
    runtime = FixtureSupervisorRuntime()
    task = Artifact(
        id="TASK-102",
        kind=ArtifactKind.TASK,
        title="Call the venue",
        text="Call at the original 7:00 PM time without adding extra guests.",
        scopes={"reservation.call"},
    )
    initial_plan = AgentPlan(
        id="PLAN-017",
        ticket_id="EVENT-208",
        objective="Confirm the reservation.",
        actions=[
            PlanAction(
                id="ACTION-CALL-017",
                description="Call the venue for a 7:00 PM reservation.",
                scopes={"reservation.call"},
                attributes={"task_id": "TASK-102"},
            ),
            PlanAction(
                id="ACTION-OTHER",
                description="Prepare an unrelated guest summary.",
                scopes={"event.copy"},
                attributes={"task_id": "TASK-101"},
            ),
        ],
    )

    supervisor = runtime.create_supervisor(
        workspace_id="voyagr-reservation",
        supervisor_run_id="LIVE-VOYAGR-RESERVATION-RUN",
        tasks=[task],
        plan=initial_plan,
        decision_snapshot="graph-v17",
    )
    assignment = supervisor.assignments[0]

    assert assignment.authorized_actions == [
        "Plan action ACTION-CALL-017: Call the venue for a 7:00 PM reservation.",
    ]
    assert "unrelated guest summary" not in " ".join(
        assignment.authorized_actions
    )

    corrected_plan = AgentPlan(
        id="PLAN-018",
        ticket_id="EVENT-208",
        objective="Correct the reservation.",
        actions=[
            PlanAction(
                id="ACTION-CALL-018",
                description="Call the venue for an approved 8:30 PM reservation.",
                scopes={"reservation.call"},
                attributes={"task_id": "TASK-102"},
            )
        ],
    )
    redirected = runtime.transition(
        assignment,
        state=SupervisorAssignmentState.REDIRECTED,
        plan=corrected_plan,
        decision_snapshot="graph-v18",
        create_replacement_run=True,
    )

    assert redirected.action_ids == ["ACTION-CALL-018"]
    assert redirected.authorized_actions == [
        (
            "Plan action ACTION-CALL-018: Call the venue for an approved "
            "8:30 PM reservation."
        ),
    ]
    assert "7:00 PM" not in " ".join(redirected.authorized_actions)


# --- Per-developer hook credentials -------------------------------------------

ALICE = {HOOK_API_KEY_HEADER: "alice-secret"}
BOB = {HOOK_API_KEY_HEADER: "bob-secret"}
TWO_DEVELOPERS = HookCredentialVerifier(
    credentials={"alice-secret": "alice", "bob-secret": "bob"},
)


def _export_definition() -> LiveWorkspaceImportRequest:
    scopes = {"export.authorization", "export.generation"}
    return LiveWorkspaceImportRequest(
        id="csv-exports",
        name="CSV exports",
        authority_policy={scope: {"approve_compliance"} for scope in scopes},
        baseline_decision=Artifact(
            id="DEC-004",
            kind=ArtifactKind.DECISION,
            title="CSV export baseline",
            scopes=scopes,
            approval_status=ApprovalStatus.PROPOSAL,
            authority_role="approve_compliance",
            effective_at=datetime(2026, 7, 24, tzinfo=UTC),
            attributes={
                "requirements": {
                    "export.authorization": {"audience": "all-users"},
                    "export.generation": {"format": "csv"},
                }
            },
        ),
        specification=Artifact(
            id="SPEC-009",
            kind=ArtifactKind.SPECIFICATION,
            title="Export specification",
            scopes=scopes,
        ),
        ticket=Artifact(
            id="TICKET-100", kind=ArtifactKind.TICKET, title="Build exports", scopes=scopes
        ),
        tasks=[
            Artifact(
                id="TASK-102",
                kind=ArtifactKind.TASK,
                title="Authorize exports",
                scopes={"export.authorization"},
            ),
            Artifact(
                id="TASK-101",
                kind=ArtifactKind.TASK,
                title="Generate CSV",
                scopes={"export.generation"},
            ),
        ],
        plan=AgentPlan(
            id="PLAN-027",
            ticket_id="TICKET-100",
            objective="Build safe CSV exports.",
            actions=[
                PlanAction(
                    id="ACTION-AUTH",
                    description="Allow authenticated users to export.",
                    scopes={"export.authorization"},
                    attributes={"task_id": "TASK-102", "audience": "all-users"},
                ),
                PlanAction(
                    id="ACTION-CSV",
                    description="Generate CSV output.",
                    scopes={"export.generation"},
                    attributes={"task_id": "TASK-101", "format": "csv"},
                ),
            ],
        ),
    )


def _hook_client(tmp_path: Path, verifier: HookCredentialVerifier) -> TestClient:
    """A supervisor session router over one live workspace with running assignments."""

    definition = _export_definition()
    runtime = ClaudeCodeSupervisorRuntime()
    supervisor = runtime.create_supervisor(
        workspace_id=definition.id,
        supervisor_run_id="LIVE-CSV-EXPORTS-RUN",
        tasks=definition.tasks,
        plan=definition.plan,
        decision_snapshot="graph-v17",
    )
    supervisor.assignments = [
        runtime.transition(assignment, state=SupervisorAssignmentState.RUNNING)
        for assignment in supervisor.assignments
    ]
    repository = SqliteLiveWorkspaceRepository(tmp_path / "live-workspaces.sqlite3")
    repository.create(
        LiveWorkspaceRecord(
            definition=definition,
            context_id="live-csv-exports",
            graph_version="graph-v17",
            current_plan=definition.plan,
            supervisor=supervisor,
        )
    )
    enforcement = ClaudeCodeSessionEnforcement(
        registry=ClaudeCodeSessionRegistry(),
        assignments=RepositorySupervisorAssignmentGateway(
            repository=repository, runtime=runtime
        ),
    )
    app = FastAPI()
    install_api_support(app)
    app.include_router(
        build_supervisor_session_router(enforcement, api_key_verifier=verifier)
    )
    return TestClient(app)


def _start_bound_session(
    client: TestClient, session_id: str, headers: dict[str, str], cwd: Path
) -> dict[str, object]:
    response = client.post(
        "/supervisor/sessions/start",
        json={
            "session_id": session_id,
            "cwd": str(cwd),
            "branch": "feature/TASK-102-authorize",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    binding = response.json()["binding"]
    assert binding["assignment"]["assignment_id"], "the session did not bind"
    return dict(binding)


def _check_body(session_id: str) -> dict[str, object]:
    return {
        "session_id": session_id,
        "tool_name": "Write",
        "timestamp": "2026-07-25T00:00:00Z",
    }


def test_a_developer_cannot_acknowledge_another_developers_session(
    tmp_path: Path,
) -> None:
    client = _hook_client(tmp_path, TWO_DEVELOPERS)
    binding = _start_bound_session(client, "alice-session", ALICE, tmp_path)
    assert binding["owner_id"] == "alice"

    response = client.post("/supervisor/sessions/alice-session/acknowledge", headers=BOB)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "HOOK_SESSION_NOT_OWNED"


def test_a_developer_cannot_check_another_developers_session(tmp_path: Path) -> None:
    """Rejected, not denied: a deny would look like enforcement working."""

    client = _hook_client(tmp_path, TWO_DEVELOPERS)
    _start_bound_session(client, "alice-session", ALICE, tmp_path)

    response = client.post(
        "/supervisor/sessions/alice-session/check",
        json=_check_body("alice-session"),
        headers=BOB,
    )

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["code"] == "HOOK_SESSION_NOT_OWNED"
    assert "decision" not in body
    assert "allow" not in response.text and "deny" not in response.text

    # Alice's own credential still gets a real verdict for the same session.
    own = client.post(
        "/supervisor/sessions/alice-session/check",
        json=_check_body("alice-session"),
        headers=ALICE,
    )
    assert own.status_code == 200
    assert own.json()["decision"] == "allow"


def test_a_developer_cannot_end_another_developers_session(tmp_path: Path) -> None:
    client = _hook_client(tmp_path, TWO_DEVELOPERS)
    _start_bound_session(client, "alice-session", ALICE, tmp_path)

    response = client.post(
        "/supervisor/sessions/alice-session/end",
        json={"session_id": "alice-session"},
        headers=BOB,
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "HOOK_SESSION_NOT_OWNED"

    listed = client.get("/supervisor/sessions", headers=BOB).json()["sessions"]
    assert listed == [], "another developer's session must not be listed either"

    own = client.post(
        "/supervisor/sessions/alice-session/end",
        json={"session_id": "alice-session"},
        headers=ALICE,
    )
    assert own.status_code == 200
    assert own.json()["released"] is True


def test_the_session_list_shows_each_developer_only_their_own(tmp_path: Path) -> None:
    """B2-4: listing every developer's sessions is the ownership leak one step removed."""

    client = _hook_client(tmp_path, TWO_DEVELOPERS)
    _start_bound_session(client, "alice-session", ALICE, tmp_path)
    _start_bound_session(client, "bob-session", BOB, tmp_path)

    alice_sees = client.get("/supervisor/sessions", headers=ALICE).json()["sessions"]
    bob_sees = client.get("/supervisor/sessions", headers=BOB).json()["sessions"]

    assert [(item["session_id"], item["owner_id"]) for item in alice_sees] == [
        ("alice-session", "alice")
    ]
    assert [(item["session_id"], item["owner_id"]) for item in bob_sees] == [
        ("bob-session", "bob")
    ]


def test_a_single_shared_key_still_lists_every_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only ``WRITAI_HOOK_API_KEY`` set: every session is the default developer's."""

    monkeypatch.delenv("WRITAI_HOOK_API_KEYS", raising=False)
    monkeypatch.setenv("WRITAI_HOOK_API_KEY", "shared-key")
    client = _hook_client(tmp_path, HookCredentialVerifier.from_environment())
    headers = {HOOK_API_KEY_HEADER: "shared-key"}
    _start_bound_session(client, "first", headers, tmp_path)
    _start_bound_session(client, "second", headers, tmp_path)

    listed = client.get("/supervisor/sessions", headers=headers).json()["sessions"]

    assert sorted(item["session_id"] for item in listed) == ["first", "second"]
    assert {item["owner_id"] for item in listed} == {DEFAULT_HOOK_DEVELOPER_ID}


def test_the_owning_developer_can_acknowledge(tmp_path: Path) -> None:
    client = _hook_client(tmp_path, TWO_DEVELOPERS)
    _start_bound_session(client, "alice-session", ALICE, tmp_path)

    response = client.post("/supervisor/sessions/alice-session/acknowledge", headers=ALICE)

    assert response.status_code == 200, response.text
    assert response.json()["acknowledged"] is True


def test_a_single_shared_key_still_works_for_one_developer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only ``WRITAI_HOOK_API_KEY`` set: the full lifecycle succeeds unchanged."""

    monkeypatch.delenv("WRITAI_HOOK_API_KEYS", raising=False)
    monkeypatch.setenv("WRITAI_HOOK_API_KEY", "shared-key")
    client = _hook_client(tmp_path, HookCredentialVerifier.from_environment())
    headers = {HOOK_API_KEY_HEADER: "shared-key"}

    binding = _start_bound_session(client, "solo", headers, tmp_path)
    assert binding["owner_id"] == DEFAULT_HOOK_DEVELOPER_ID

    checked = client.post(
        "/supervisor/sessions/solo/check", json=_check_body("solo"), headers=headers
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["decision"] == "allow"

    acknowledged = client.post("/supervisor/sessions/solo/acknowledge", headers=headers)
    assert acknowledged.status_code == 200, acknowledged.text

    ended = client.post(
        "/supervisor/sessions/solo/end", json={"session_id": "solo"}, headers=headers
    )
    assert ended.status_code == 200, ended.text
    assert ended.json()["released"] is True


def test_an_unknown_key_is_rejected_without_revealing_which_keys_exist(
    tmp_path: Path,
) -> None:
    verifier = HookCredentialVerifier(
        credentials={"alice-secret": "alice", "bob-secret": "bob"},
        expected_api_key="legacy-secret",
    )
    client = _hook_client(tmp_path, verifier)

    response = client.post(
        "/supervisor/sessions/start",
        json={"session_id": "intruder", "cwd": str(tmp_path), "branch": ""},
        headers={HOOK_API_KEY_HEADER: "not-a-key"},
    )

    assert response.status_code == 401
    body = response.text
    assert response.json()["error"]["code"] == "HOOK_AUTHENTICATION_FAILED"
    for disallowed in ("alice", "bob", "alice-secret", "bob-secret", "legacy-secret"):
        assert disallowed not in body, disallowed

    missing = client.post(
        "/supervisor/sessions/start",
        json={"session_id": "intruder", "cwd": str(tmp_path), "branch": ""},
    )
    assert missing.status_code == 401


def test_hook_authentication_still_fails_closed_when_nothing_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WRITAI_HOOK_API_KEYS", raising=False)
    monkeypatch.delenv("WRITAI_HOOK_API_KEY", raising=False)
    client = _hook_client(tmp_path, HookCredentialVerifier.from_environment())

    response = client.post(
        "/supervisor/sessions/start",
        json={"session_id": "anyone", "cwd": str(tmp_path), "branch": ""},
        headers={HOOK_API_KEY_HEADER: "anything"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "HOOK_AUTHENTICATION_NOT_CONFIGURED"


def test_hook_credentials_parse_from_the_documented_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert parse_hook_credentials(" alice : alice-secret ,, bob:bob-secret , ") == {
        "alice-secret": "alice",
        "bob-secret": "bob",
    }
    assert parse_hook_credentials("") == {}

    with pytest.raises(ValueError, match="WRITAI_HOOK_API_KEYS"):
        parse_hook_credentials("alice:alice-secret,bob-without-secret")
    with pytest.raises(ValueError, match="WRITAI_HOOK_API_KEYS"):
        parse_hook_credentials("alice:same,bob:same")

    monkeypatch.setenv("WRITAI_HOOK_API_KEYS", "alice:alice-secret,bob:bob-secret")
    monkeypatch.setenv("WRITAI_HOOK_API_KEY", "legacy-secret")
    verifier = HookCredentialVerifier.from_environment()
    assert verifier.resolve("alice-secret") == "alice"
    assert verifier.resolve("bob-secret") == "bob"
    assert verifier.resolve("legacy-secret") == DEFAULT_HOOK_DEVELOPER_ID
    assert "alice-secret" not in repr(verifier)

    monkeypatch.setenv("WRITAI_HOOK_API_KEYS", "broken-entry")
    with pytest.raises(ValueError, match="WRITAI_HOOK_API_KEYS"):
        HookCredentialVerifier.from_environment()


def test_the_compatibility_alias_is_gone_and_the_single_key_field_stays() -> None:
    """B2-2: ``HookApiKeyVerifier`` existed for one caller, which has moved.

    ``expected_api_key`` is not part of the alias: it is the single-developer
    ``WRITAI_HOOK_API_KEY`` fallback that ``from_environment`` reads.
    """

    assert not hasattr(supervisor_api, "HookApiKeyVerifier")
    verifier = HookCredentialVerifier(expected_api_key="test-key")
    assert verifier.resolve("test-key") == DEFAULT_HOOK_DEVELOPER_ID


def test_the_pre_tool_use_request_gained_no_field() -> None:
    """Identity comes from the credential header, never from the request body."""

    assert set(ClaudePreToolUseRequest.model_fields) == {
        "session_id",
        "tool_name",
        "timestamp",
        "acknowledged_redirect_id",
    }
