from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from writai.domain import (
    AgentPlan,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    PlanAction,
)
from writai.services.supervisor_api import (
    HOOK_API_KEY_HEADER,
    HookApiKeyVerifier,
    build_supervisor_session_router,
)
from writai.services.support import install_api_support
from writai.supervisor_contract import InterruptRequest
from writai.workspaces.live_interrupt import LiveClaudeCodeInterruptPort
from writai.workspaces.models import (
    LiveWorkspaceImportRequest,
    LiveWorkspaceRecord,
)
from writai.workspaces.runtimes.claude_code import (
    ClaudeCodeSupervisorRuntime,
)
from writai.workspaces.session_binding import (
    ClaudeCodeSessionRegistry,
    SessionBindingSource,
    SupervisorAssignmentTarget,
)
from writai.workspaces.session_enforcement import (
    SPENT_DENY_STATES,
    ClaudeCodeSessionEnforcement,
    ClaudePreToolUseRequest,
    ClaudeSessionStartRequest,
    HookDenialMode,
    HookPermissionDecision,
    RepositorySupervisorAssignmentGateway,
)
from writai.workspaces.supervisor import (
    FixtureSupervisorRuntime,
    SupervisorAssignmentState,
    SupervisorExecutionMode,
    SupervisorRuntimeProvider,
)


class MemoryWorkspaceRepository:
    def __init__(self, record: LiveWorkspaceRecord) -> None:
        self._record = record.model_copy(deep=True)

    def create(self, record: LiveWorkspaceRecord) -> None:
        self._record = record.model_copy(deep=True)

    def save(self, record: LiveWorkspaceRecord) -> None:
        self._record = record.model_copy(deep=True)

    def get(self, workspace_id: str) -> LiveWorkspaceRecord:
        assert self._record.definition.id == workspace_id
        return self._record.model_copy(deep=True)

    def list(self) -> list[LiveWorkspaceRecord]:
        return [self._record.model_copy(deep=True)]


def _definition() -> LiveWorkspaceImportRequest:
    scopes = {"export.authorization", "export.generation"}
    baseline = datetime(2026, 7, 24, tzinfo=UTC)
    return LiveWorkspaceImportRequest(
        id="csv-exports",
        name="CSV exports",
        authority_policy={
            "export.authorization": {"approve_compliance"},
            "export.generation": {"approve_compliance"},
        },
        baseline_decision=Artifact(
            id="DEC-004",
            kind=ArtifactKind.DECISION,
            title="CSV export baseline",
            scopes=scopes,
            approval_status=ApprovalStatus.PROPOSAL,
            authority_role="approve_compliance",
            effective_at=baseline,
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
            id="TICKET-100",
            kind=ArtifactKind.TICKET,
            title="Build exports",
            scopes=scopes,
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
                    attributes={
                        "task_id": "TASK-102",
                        "audience": "all-users",
                    },
                ),
                PlanAction(
                    id="ACTION-CSV",
                    description="Generate CSV output.",
                    scopes={"export.generation"},
                    attributes={
                        "task_id": "TASK-101",
                        "format": "csv",
                    },
                ),
            ],
        ),
    )


def _live_record() -> tuple[
    MemoryWorkspaceRepository,
    ClaudeCodeSupervisorRuntime,
]:
    definition = _definition()
    runtime = ClaudeCodeSupervisorRuntime()
    supervisor = runtime.create_supervisor(
        workspace_id=definition.id,
        supervisor_run_id="LIVE-CSV-EXPORTS-RUN",
        tasks=definition.tasks,
        plan=definition.plan,
        decision_snapshot="graph-v17",
    )
    supervisor.assignments = [
        runtime.transition(
            assignment,
            state=SupervisorAssignmentState.RUNNING,
        )
        for assignment in supervisor.assignments
    ]
    record = LiveWorkspaceRecord(
        definition=definition,
        context_id="live-csv-exports",
        graph_version="graph-v17",
        current_plan=definition.plan,
        supervisor=supervisor,
    )
    return MemoryWorkspaceRepository(record), runtime


def _check(
    session_id: str,
    acknowledged_redirect_id: str | None = None,
) -> ClaudePreToolUseRequest:
    return ClaudePreToolUseRequest(
        session_id=session_id,
        tool_name="Write",
        timestamp=datetime(2026, 7, 25, tzinfo=UTC),
        acknowledged_redirect_id=acknowledged_redirect_id,
    )


def test_live_runtime_is_typed_live_and_fixture_remains_simulated() -> None:
    definition = _definition()
    live = ClaudeCodeSupervisorRuntime()
    supervisor = live.create_supervisor(
        workspace_id=definition.id,
        supervisor_run_id="LIVE-CSV-EXPORTS-RUN",
        tasks=definition.tasks,
        plan=definition.plan,
        decision_snapshot="graph-v17",
    )

    assert live.execution_mode is SupervisorExecutionMode.LIVE
    assert supervisor.adapter == "claude-code-hook-runtime"
    assert supervisor.execution_mode is SupervisorExecutionMode.LIVE
    assert all(
        assignment.execution_mode is SupervisorExecutionMode.LIVE
        for assignment in supervisor.assignments
    )
    assert FixtureSupervisorRuntime().execution_mode is SupervisorExecutionMode.SIMULATED


def test_codex_assignment_is_not_mislabeled_as_live() -> None:
    definition = _definition()
    codex_task = definition.tasks[0].model_copy(deep=True)
    codex_task.attributes["runtime_provider"] = "codex"
    plan = definition.plan.model_copy(deep=True)
    plan.actions = [plan.actions[0]]

    supervisor = ClaudeCodeSupervisorRuntime().create_supervisor(
        workspace_id=definition.id,
        supervisor_run_id="LIVE-CSV-EXPORTS-RUN",
        tasks=[codex_task],
        plan=plan,
        decision_snapshot="graph-v17",
    )

    assignment = supervisor.assignments[0]
    assert assignment.runtime_provider is SupervisorRuntimeProvider.CODEX
    assert assignment.execution_mode is SupervisorExecutionMode.SIMULATED


def test_scope_sensitive_interrupt_denies_once_and_preserves_sibling(
    tmp_path: Path,
) -> None:
    repository, runtime = _live_record()
    updated = repository.get("csv-exports")
    updated.graph_version = "graph-v18"
    repository.save(updated)
    port = LiveClaudeCodeInterruptPort(
        repository=repository,
        runtime=runtime,
    )
    request = InterruptRequest(
        workspace_id="csv-exports",
        decision_id="DEC-018",
        affected_scopes=frozenset({"export.authorization"}),
        provenance_path=(
            "DEC-018",
            "DEC-004",
            "SPEC-009",
            "TICKET-100",
            "TASK-102",
        ),
        interrupt_reason="Exports now require an administrator.",
        redirect_instruction="Add the administrator authorization check.",
    )

    preview = port.preview(request)
    assert preview.interrupted_assignment_ids == ("ASSIGNMENT-TASK-102",)
    assert preview.preserved_assignment_ids == ("ASSIGNMENT-TASK-101",)
    assert port.interrupt(request) == preview
    assert port.interrupt(request) == preview

    gateway = RepositorySupervisorAssignmentGateway(
        repository=repository,
        runtime=runtime,
    )
    registry = ClaudeCodeSessionRegistry()
    enforcement = ClaudeCodeSessionEnforcement(
        registry=registry,
        assignments=gateway,
    )
    enforcement.start(
        ClaudeSessionStartRequest(
            session_id="session-affected",
            cwd=str(tmp_path),
            branch="feat/TASK-102-admin-exports",
        )
    )
    first = enforcement.check(_check("session-affected"))
    # The deny is NOT spent by being issued. It is spent by being *received*:
    # the hook echoes `redirect_id` on its next check and only then does the
    # assignment advance. A second check that does not acknowledge re-delivers
    # the identical redirect rather than allowing.
    unacknowledged = enforcement.check(_check("session-affected"))
    second = enforcement.check(
        _check("session-affected", acknowledged_redirect_id=first.redirect_id)
    )

    assert first.decision is HookPermissionDecision.DENY
    assert first.denial_mode is HookDenialMode.UNTIL_ACKNOWLEDGED
    assert first.redirect_id
    assert first.redirect_instruction == (
        "Add the administrator authorization check."
    )
    assert unacknowledged.decision is HookPermissionDecision.DENY
    assert unacknowledged.redirect_instruction == first.redirect_instruction
    assert unacknowledged.redirect_id == first.redirect_id
    assert second.decision is HookPermissionDecision.ALLOW
    assert LiveClaudeCodeInterruptPort(
        repository=repository,
        runtime=runtime,
    ).interrupt(request) == preview
    assert (
        enforcement.check(_check("session-affected")).decision
        is HookPermissionDecision.ALLOW
    )

    preserved_binding = enforcement.start(
        ClaudeSessionStartRequest(
            session_id="session-preserved",
            cwd=str(tmp_path / "preserved"),
            branch="feat/TASK-101-csv-generation",
        )
    )
    preserved = enforcement.check(_check("session-preserved"))
    assert (
        preserved_binding.binding.source
        is SessionBindingSource.BRANCH
    )
    assert preserved.decision is HookPermissionDecision.ALLOW
    assert "do not intersect" in preserved.reason


def test_outright_invalidated_assignment_denies_until_acknowledged(
    tmp_path: Path,
) -> None:
    repository, runtime = _live_record()
    record = repository.get("csv-exports")
    record.graph_version = "graph-v18"
    assert record.supervisor is not None
    assignment = next(
        item
        for item in record.supervisor.assignments
        if item.task_id == "TASK-102"
    )
    interrupted = runtime.transition(
        assignment,
        state=SupervisorAssignmentState.INTERRUPTED,
        interrupt_reason="The assignment was invalidated without a redirect.",
        interrupt_enforced=True,
    )
    record.supervisor.assignments = [
        interrupted if item.id == interrupted.id else item
        for item in record.supervisor.assignments
    ]
    repository.save(record)
    enforcement = ClaudeCodeSessionEnforcement(
        registry=ClaudeCodeSessionRegistry(),
        assignments=RepositorySupervisorAssignmentGateway(
            repository=repository,
            runtime=runtime,
        ),
    )
    enforcement.start(
        ClaudeSessionStartRequest(
            session_id="session-invalid",
            cwd=str(tmp_path),
            branch="feat/TASK-102-invalid",
        )
    )

    assert (
        enforcement.check(_check("session-invalid")).denial_mode
        is HookDenialMode.UNTIL_ACKNOWLEDGED
    )
    assert (
        enforcement.check(_check("session-invalid")).decision
        is HookPermissionDecision.DENY
    )
    enforcement.acknowledge(session_id="session-invalid")
    assert (
        enforcement.check(_check("session-invalid")).decision
        is HookPermissionDecision.ALLOW
    )


def test_registered_unbound_session_allows_without_inference(
    tmp_path: Path,
) -> None:
    repository, runtime = _live_record()
    enforcement = ClaudeCodeSessionEnforcement(
        registry=ClaudeCodeSessionRegistry(),
        assignments=RepositorySupervisorAssignmentGateway(
            repository=repository,
            runtime=runtime,
        ),
    )
    response = enforcement.start(
        ClaudeSessionStartRequest(
            session_id="session-unbound",
            cwd=str(tmp_path),
            branch="feature/no-task-id",
        )
    )

    assert response.binding.source is SessionBindingSource.UNBOUND
    assert enforcement.check(
        _check("session-unbound")
    ).decision is HookPermissionDecision.ALLOW
    unknown = enforcement.check(_check("never-registered"))
    assert unknown.decision is HookPermissionDecision.DENY
    assert unknown.denial_mode is HookDenialMode.UNTIL_REGISTERED


def test_binding_precedence_is_explicit_then_branch_then_task_file(
    tmp_path: Path,
) -> None:
    repository, _runtime = _live_record()
    record = repository.get("csv-exports")
    assert record.supervisor is not None
    candidates = [
        SupervisorAssignmentTarget(
            workspace_id="csv-exports",
            assignment=item,
        )
        for item in record.supervisor.assignments
    ]
    registry = ClaudeCodeSessionRegistry()
    registry.attach(
        cwd=tmp_path,
        workspace_id="csv-exports",
        assignment_id="ASSIGNMENT-TASK-102",
    )
    (tmp_path / ".writai").mkdir()
    (tmp_path / ".writai" / "task").write_text(
        "TASK-101\n",
        encoding="utf-8",
    )

    explicit = registry.register(
        session_id="explicit",
        cwd=tmp_path,
        branch="feat/TASK-101-generation",
        candidates=candidates,
    )
    assert explicit.source is SessionBindingSource.EXPLICIT
    assert explicit.assignment is not None
    assert explicit.assignment.task_id == "TASK-102"

    registry.detach(cwd=tmp_path)
    branch = registry.register(
        session_id="branch",
        cwd=tmp_path,
        branch="feat/TASK-102-authorization",
        candidates=candidates,
    )
    assert branch.source is SessionBindingSource.BRANCH
    assert branch.assignment is not None
    assert branch.assignment.task_id == "TASK-102"

    task_file = registry.register(
        session_id="task-file",
        cwd=tmp_path,
        branch="feature/no-task",
        candidates=candidates,
    )
    assert task_file.source is SessionBindingSource.TASK_FILE
    assert task_file.assignment is not None
    assert task_file.assignment.task_id == "TASK-101"


def test_attach_file_binds_explicitly_before_branch_and_task_file(
    tmp_path: Path,
) -> None:
    repository, _runtime = _live_record()
    record = repository.get("csv-exports")
    assert record.supervisor is not None
    candidates = [
        SupervisorAssignmentTarget(
            workspace_id="csv-exports",
            assignment=item,
        )
        for item in record.supervisor.assignments
    ]
    marker_directory = tmp_path / ".writai"
    marker_directory.mkdir()
    (marker_directory / "attach").write_text(
        "ASSIGNMENT-TASK-102\n",
        encoding="utf-8",
    )
    (marker_directory / "task").write_text("TASK-101\n", encoding="utf-8")

    binding = ClaudeCodeSessionRegistry().register(
        session_id="file-explicit",
        cwd=tmp_path,
        branch="feat/TASK-101-generation",
        candidates=candidates,
    )

    assert binding.source is SessionBindingSource.EXPLICIT
    assert binding.assignment is not None
    assert binding.assignment.assignment_id == "ASSIGNMENT-TASK-102"


def test_unknown_attach_file_denies_rather_than_becoming_unbound(
    tmp_path: Path,
) -> None:
    """An attachment that resolves to nothing must not read as "no binding".

    Unbound is permissive -- the session is allowed everything -- so answering
    "you named an assignment that does not exist" with unbound would make one
    junk marker file an off switch for enforcement. Raised by the cross-model
    review against the first version of this wiring.
    """

    repository, _runtime = _live_record()
    record = repository.get("csv-exports")
    assert record.supervisor is not None
    candidates = [
        SupervisorAssignmentTarget(
            workspace_id="csv-exports",
            assignment=item,
        )
        for item in record.supervisor.assignments
    ]
    marker_directory = tmp_path / ".writai"
    marker_directory.mkdir()
    (marker_directory / "attach").write_text("ASSIGNMENT-UNKNOWN\n")
    (marker_directory / "task").write_text("TASK-101\n")

    binding = ClaudeCodeSessionRegistry().register(
        session_id="unknown-attach",
        cwd=tmp_path,
        branch="feat/TASK-101-generation",
        candidates=candidates,
    )

    assert binding.source is SessionBindingSource.UNRESOLVED_ATTACHMENT
    assert binding.assignment is None
    assert "does not name an available live assignment" in binding.detail


def test_attach_file_matching_multiple_workspaces_denies(
    tmp_path: Path,
) -> None:
    repository, _runtime = _live_record()
    record = repository.get("csv-exports")
    assert record.supervisor is not None
    assignment = record.supervisor.assignments[0]
    candidates = [
        SupervisorAssignmentTarget(
            workspace_id="csv-exports",
            assignment=assignment,
        ),
        SupervisorAssignmentTarget(
            workspace_id="other-workspace",
            assignment=assignment,
        ),
    ]
    marker_directory = tmp_path / ".writai"
    marker_directory.mkdir()
    (marker_directory / "attach").write_text(f"{assignment.id}\n")

    binding = ClaudeCodeSessionRegistry().register(
        session_id="ambiguous-attach",
        cwd=tmp_path,
        branch="",
        candidates=candidates,
    )

    assert binding.source is SessionBindingSource.UNRESOLVED_ATTACHMENT
    assert binding.assignment is None
    assert "multiple workspaces" in binding.detail


@pytest.mark.parametrize(
    ("marker_kind", "expected_detail"),
    [
        ("symlink", "must not be a symbolic link"),
        ("oversized", "exceeds the safe size limit"),
        ("multi-line", "must contain exactly one assignment ID"),
    ],
)
def test_an_unreadable_attach_file_is_ignored_rather_than_stripping_enforcement(
    tmp_path: Path,
    marker_kind: str,
    expected_detail: str,
) -> None:
    """A corrupt marker must not be a way to switch enforcement off.

    Stopping at UNBOUND here would mean anyone who can write the working
    directory can free a session from supervision by leaving a garbage attach
    file, because an unbound session is allowed everything. So an *unreadable*
    marker falls through to the remaining rules and the session still binds —
    and the binding says the marker was skipped, so the reason is visible in
    `writai dev why` rather than swallowed.

    `scripts/ci/writai_ci_check.py` falls through on exactly these cases too.
    The service and the PR check must not disagree about what binds.
    """

    repository, _runtime = _live_record()
    record = repository.get("csv-exports")
    assert record.supervisor is not None
    candidates = [
        SupervisorAssignmentTarget(
            workspace_id="csv-exports",
            assignment=item,
        )
        for item in record.supervisor.assignments
    ]
    marker_directory = tmp_path / ".writai"
    marker_directory.mkdir()
    attach_path = marker_directory / "attach"
    if marker_kind == "symlink":
        target = tmp_path / "attach-target"
        target.write_text("ASSIGNMENT-TASK-102\n")
        attach_path.symlink_to(target)
    elif marker_kind == "oversized":
        attach_path.write_text("A" * 513)
    else:
        attach_path.write_text("ASSIGNMENT-TASK-102\nASSIGNMENT-TASK-101\n")
    (marker_directory / "task").write_text("TASK-101\n")

    binding = ClaudeCodeSessionRegistry().register(
        session_id=f"unsafe-{marker_kind}",
        cwd=tmp_path,
        branch="feat/TASK-101-generation",
        candidates=candidates,
    )

    assert binding.source is SessionBindingSource.BRANCH
    assert binding.assignment is not None
    assert binding.assignment.task_id == "TASK-101"
    assert expected_detail in binding.detail
    assert ".writai/attach was ignored" in binding.detail


@pytest.mark.parametrize(
    "marker_kind",
    ["symlink", "oversized", "multi-line"],
)
def test_an_unreadable_attach_file_with_nothing_else_to_bind_is_unbound(
    tmp_path: Path,
    marker_kind: str,
) -> None:
    """Falling through is not the same as inventing a binding."""

    repository, _runtime = _live_record()
    record = repository.get("csv-exports")
    assert record.supervisor is not None
    candidates = [
        SupervisorAssignmentTarget(workspace_id="csv-exports", assignment=item)
        for item in record.supervisor.assignments
    ]
    marker_directory = tmp_path / ".writai"
    marker_directory.mkdir()
    attach_path = marker_directory / "attach"
    if marker_kind == "symlink":
        target = tmp_path / "attach-target"
        target.write_text("ASSIGNMENT-TASK-102\n")
        attach_path.symlink_to(target)
    elif marker_kind == "oversized":
        attach_path.write_text("A" * 513)
    else:
        attach_path.write_text("ASSIGNMENT-TASK-102\nASSIGNMENT-TASK-101\n")

    binding = ClaudeCodeSessionRegistry().register(
        session_id=f"lonely-{marker_kind}",
        cwd=tmp_path,
        branch="chore/no-task",
        candidates=candidates,
    )

    assert binding.source is SessionBindingSource.UNBOUND
    assert binding.assignment is None
    assert ".writai/attach was ignored" in binding.detail


def test_pre_tool_request_rejects_private_hook_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ClaudePreToolUseRequest.model_validate(
            {
                "session_id": "session-1",
                "tool_name": "Write",
                "timestamp": "2026-07-25T00:00:00Z",
                "tool_input": {"file_path": "/secret"},
            }
        )


def test_session_start_router_reads_attach_file_and_binds_explicitly(
    tmp_path: Path,
) -> None:
    repository, runtime = _live_record()
    enforcement = ClaudeCodeSessionEnforcement(
        registry=ClaudeCodeSessionRegistry(),
        assignments=RepositorySupervisorAssignmentGateway(
            repository=repository,
            runtime=runtime,
        ),
    )
    app = FastAPI()
    install_api_support(app)
    app.include_router(
        build_supervisor_session_router(
            enforcement,
            api_key_verifier=HookApiKeyVerifier(expected_api_key="key"),
        )
    )
    marker_directory = tmp_path / ".writai"
    marker_directory.mkdir()
    (marker_directory / "attach").write_text(
        "ASSIGNMENT-TASK-102\n",
        encoding="utf-8",
    )

    response = TestClient(app).post(
        "/supervisor/sessions/start",
        json={
            "session_id": "file-bound",
            "cwd": str(tmp_path),
            "branch": "feature/no-task",
        },
        headers={HOOK_API_KEY_HEADER: "key"},
    )

    assert response.status_code == 200
    binding = response.json()["binding"]
    assert binding["source"] == SessionBindingSource.EXPLICIT
    assert binding["assignment"]["assignment_id"] == "ASSIGNMENT-TASK-102"


def test_agent_service_router_exposes_fail_closed_check_without_private_fields(
    tmp_path: Path,
) -> None:
    repository, runtime = _live_record()
    registry = ClaudeCodeSessionRegistry()
    enforcement = ClaudeCodeSessionEnforcement(
        registry=registry,
        assignments=RepositorySupervisorAssignmentGateway(
            repository=repository,
            runtime=runtime,
        ),
    )
    app = FastAPI()
    install_api_support(app)
    app.include_router(
        build_supervisor_session_router(
            enforcement,
            api_key_verifier=HookApiKeyVerifier(
                expected_api_key="developer-hook-api-key"
            ),
        )
    )
    client = TestClient(app)
    authenticated = {
        HOOK_API_KEY_HEADER: "developer-hook-api-key",
    }

    unknown = client.post(
        "/supervisor/sessions/unknown/check",
        json={
            "session_id": "unknown",
            "tool_name": "Read",
            "timestamp": "2026-07-25T00:00:00Z",
        },
        headers=authenticated,
    )
    assert unknown.status_code == 200
    assert unknown.json()["decision"] == "deny"

    private = client.post(
        "/supervisor/sessions/private/check",
        json={
            "session_id": "private",
            "tool_name": "Write",
            "timestamp": "2026-07-25T00:00:00Z",
            "tool_input": {"content": "must not cross the boundary"},
        },
        headers=authenticated,
    )
    assert private.status_code == 422
    assert private.json()["error"]["code"] == "INVALID_REQUEST"

    started = client.post(
        "/supervisor/sessions/start",
        json={
            "session_id": "unbound",
            "cwd": str(tmp_path),
            "branch": "feature/no-task",
        },
        headers=authenticated,
    )
    assert started.status_code == 200
    checked = client.post(
        "/supervisor/sessions/unbound/check",
        json={
            "session_id": "unbound",
            "tool_name": "Read",
            "timestamp": "2026-07-25T00:00:00Z",
        },
        headers=authenticated,
    )
    assert checked.status_code == 200
    assert checked.json()["decision"] == "allow"

    bound = client.post(
        "/supervisor/sessions/start",
        json={
            "session_id": "bound",
            "cwd": str(tmp_path / "bound"),
            "branch": "feature/TASK-102-authorize",
        },
        headers=authenticated,
    )
    assert bound.status_code == 200
    assert bound.json()["binding"]["assignment"]["task_id"] == "TASK-102"

    rebind_without_auth = client.post(
        "/supervisor/sessions/start",
        json={
            "session_id": "bound",
            "cwd": str(tmp_path / "bound"),
            "branch": "feature/no-task",
        },
    )
    assert rebind_without_auth.status_code == 401
    retained = registry.get("bound")
    assert retained is not None and retained.assignment is not None
    assert retained.assignment.task_id == "TASK-102"
    check_without_auth = client.post(
        "/supervisor/sessions/bound/check",
        json={
            "session_id": "bound",
            "tool_name": "Read",
            "timestamp": "2026-07-25T00:00:00Z",
        },
    )
    assert check_without_auth.status_code == 401
    end_with_wrong_auth = client.post(
        "/supervisor/sessions/bound/end",
        json={"session_id": "bound"},
        headers={HOOK_API_KEY_HEADER: "wrong-key"},
    )
    assert end_with_wrong_auth.status_code == 401
    assert registry.get("bound") is not None

    record = repository.get("csv-exports")
    record.graph_version = "graph-v18"
    assert record.supervisor is not None
    assignment = next(
        item
        for item in record.supervisor.assignments
        if item.task_id == "TASK-102"
    )
    interrupted = runtime.transition(
        assignment,
        state=SupervisorAssignmentState.INTERRUPTED,
        interrupt_reason="A human must acknowledge this invalidation.",
        interrupt_enforced=True,
    )
    record.supervisor.assignments = [
        interrupted if item.id == interrupted.id else item
        for item in record.supervisor.assignments
    ]
    repository.save(record)

    denied = client.post(
        "/supervisor/sessions/bound/check",
        json={
            "session_id": "bound",
            "tool_name": "Write",
            "timestamp": "2026-07-25T00:00:00Z",
        },
        headers=authenticated,
    )
    assert denied.status_code == 200
    assert denied.json()["decision"] == "deny"

    bypass = client.post(
        "/supervisor/sessions/bound/acknowledge",
        headers={HOOK_API_KEY_HEADER: "wrong-key"},
    )
    assert bypass.status_code == 401
    still_denied = client.post(
        "/supervisor/sessions/bound/check",
        json={
            "session_id": "bound",
            "tool_name": "Write",
            "timestamp": "2026-07-25T00:00:01Z",
        },
        headers=authenticated,
    )
    assert still_denied.json()["decision"] == "deny"

    acknowledged = client.post(
        "/supervisor/sessions/bound/acknowledge",
        headers=authenticated,
    )
    assert acknowledged.status_code == 200
    allowed = client.post(
        "/supervisor/sessions/bound/check",
        json={
            "session_id": "bound",
            "tool_name": "Write",
            "timestamp": "2026-07-25T00:00:02Z",
        },
        headers=authenticated,
    )
    assert allowed.json()["decision"] == "allow"


def test_session_list_carries_the_state_each_session_is_judged_on(
    tmp_path: Path,
) -> None:
    """`GET /supervisor/sessions` answers the three questions that decide a demo.

    Is the session bound? Is its assignment pinned to the current graph version?
    Has its deny already been spent? A binding alone answers none of them, and a
    caller that has to reconstruct them from a second endpoint can reach a
    different answer than the hook would.
    """

    repository, runtime = _live_record()
    registry = ClaudeCodeSessionRegistry()
    enforcement = ClaudeCodeSessionEnforcement(
        registry=registry,
        assignments=RepositorySupervisorAssignmentGateway(
            repository=repository,
            runtime=runtime,
        ),
    )
    app = FastAPI()
    install_api_support(app)
    app.include_router(
        build_supervisor_session_router(
            enforcement,
            api_key_verifier=HookApiKeyVerifier(expected_api_key="key"),
        )
    )
    client = TestClient(app)
    authenticated = {HOOK_API_KEY_HEADER: "key"}

    for session_id, branch, cwd in (
        ("bound", "feature/TASK-102-authorize", tmp_path / "bound"),
        ("loose", "feature/no-task", tmp_path / "loose"),
    ):
        assert (
            client.post(
                "/supervisor/sessions/start",
                json={"session_id": session_id, "cwd": str(cwd), "branch": branch},
                headers=authenticated,
            ).status_code
            == 200
        )

    listed = client.get("/supervisor/sessions", headers=authenticated)
    assert listed.status_code == 200
    by_session = {item["session_id"]: item for item in listed.json()["sessions"]}

    # An unbound session is allowed everything and looks identical to a working
    # one, so it is listed rather than hidden, and says so in one field.
    loose = by_session["loose"]
    assert loose["bound"] is False
    assert loose["source"] == SessionBindingSource.UNBOUND
    assert loose["state"] is None

    armed = by_session["bound"]
    assert armed["bound"] is True
    assert armed["assignment"]["task_id"] == "TASK-102"
    assert armed["state"] == SupervisorAssignmentState.RUNNING
    assert armed["snapshot_current"] is True
    assert armed["decision_snapshot"] == armed["current_decision_snapshot"]
    assert armed["deny_spent"] is False

    # Fire: the workspace advances and the assignment is interrupted.
    record = repository.get("csv-exports")
    record.graph_version = "graph-v18"
    assert record.supervisor is not None
    assignment = next(
        item for item in record.supervisor.assignments if item.task_id == "TASK-102"
    )
    record.supervisor.assignments = [
        runtime.transition(
            assignment,
            state=SupervisorAssignmentState.INTERRUPTED,
            interrupt_reason="A human must acknowledge this invalidation.",
            redirect_instruction="Re-plan against admin-only exports.",
            interrupt_enforced=True,
        )
        if item.id == assignment.id
        else item
        for item in record.supervisor.assignments
    ]
    repository.save(record)

    def listed_bound() -> dict[str, object]:
        return {
            item["session_id"]: item
            for item in client.get(
                "/supervisor/sessions", headers=authenticated
            ).json()["sessions"]
        }["bound"]

    fired = listed_bound()
    assert fired["state"] == SupervisorAssignmentState.INTERRUPTED
    assert fired["snapshot_current"] is False
    assert fired["current_decision_snapshot"] == "graph-v18"
    # INTERRUPTED is ARMED, not spent. `check` denies on it every time. Reporting
    # it as spent would call a correctly armed stage broken.
    assert fired["deny_spent"] is False

    # Listing is a read. It must not consume the deny the demo depends on.
    verdict = client.post(
        "/supervisor/sessions/bound/check",
        json={
            "session_id": "bound",
            "tool_name": "Write",
            "timestamp": "2026-07-25T00:00:00Z",
        },
        headers=authenticated,
    )
    assert verdict.json()["decision"] == "deny"
    redirect_id = verdict.json()["redirect_id"]
    assert redirect_id

    # Issuing the deny did NOT spend it. The assignment is still interrupted and
    # still reads as unspent, because nothing has confirmed the redirect
    # reached anyone yet.
    still_armed = listed_bound()
    assert still_armed["state"] == SupervisorAssignmentState.INTERRUPTED
    assert still_armed["deny_spent"] is False

    # The hook confirms delivery on its next check. Only now does the assignment
    # advance, and only now is the deny spent.
    allowed = client.post(
        "/supervisor/sessions/bound/check",
        json={
            "session_id": "bound",
            "tool_name": "Write",
            "timestamp": "2026-07-25T00:00:01Z",
            "acknowledged_redirect_id": redirect_id,
        },
        headers=authenticated,
    )
    assert allowed.json()["decision"] == "allow"

    spent = listed_bound()
    assert spent["state"] == SupervisorAssignmentState.REDIRECTED
    assert spent["snapshot_current"] is True
    assert spent["deny_spent"] is True


def test_deny_spent_means_exactly_the_states_that_no_longer_deny() -> None:
    """The field has to agree with `check`, or the readiness board lies.

    Verified against a running service as well as here: an INTERRUPTED
    assignment denies and only then becomes REDIRECTED; a REDIRECTED one is
    allowed straight through.
    """

    assert SupervisorAssignmentState.INTERRUPTED not in SPENT_DENY_STATES
    assert SPENT_DENY_STATES == {
        SupervisorAssignmentState.REDIRECTED,
        SupervisorAssignmentState.RESUMED,
        SupervisorAssignmentState.COMPLETED,
    }
    # CONTINUING is the preserved sibling. It is always allowed, by design, and
    # is not a spent deny — it never had one.
    assert SupervisorAssignmentState.CONTINUING not in SPENT_DENY_STATES


def test_session_list_reports_a_binding_whose_assignment_vanished(
    tmp_path: Path,
) -> None:
    """A bound session whose assignment is gone denies. It must not read as healthy."""

    repository, runtime = _live_record()
    enforcement = ClaudeCodeSessionEnforcement(
        registry=ClaudeCodeSessionRegistry(),
        assignments=RepositorySupervisorAssignmentGateway(
            repository=repository,
            runtime=runtime,
        ),
    )
    enforcement.start(
        ClaudeSessionStartRequest(
            session_id="orphan",
            cwd=str(tmp_path),
            branch="feature/TASK-102-authorize",
        )
    )

    record = repository.get("csv-exports")
    assert record.supervisor is not None
    record.supervisor.assignments = [
        item for item in record.supervisor.assignments if item.task_id != "TASK-102"
    ]
    repository.save(record)

    (session,) = enforcement.registered_sessions()
    assert session.assignment_state.bound is True
    assert session.assignment_state.assignment_missing is True
    assert session.assignment_state.snapshot_current is False
    assert session.assignment_state.state is None

    assert (
        enforcement.check(
            ClaudePreToolUseRequest(
                session_id="orphan",
                tool_name="Read",
                timestamp=datetime(2026, 7, 25, tzinfo=UTC),
            )
        ).decision
        is HookPermissionDecision.DENY
    )


def test_agent_service_router_fails_closed_when_hook_auth_is_unconfigured(
    tmp_path: Path,
) -> None:
    repository, runtime = _live_record()
    enforcement = ClaudeCodeSessionEnforcement(
        registry=ClaudeCodeSessionRegistry(),
        assignments=RepositorySupervisorAssignmentGateway(
            repository=repository,
            runtime=runtime,
        ),
    )
    app = FastAPI()
    install_api_support(app)
    app.include_router(
        build_supervisor_session_router(
            enforcement,
            api_key_verifier=HookApiKeyVerifier(expected_api_key=""),
        )
    )

    response = TestClient(app).post(
        "/supervisor/sessions/start",
        json={
            "session_id": "session-unconfigured",
            "cwd": str(tmp_path),
            "branch": "feature/TASK-102-authorize",
        },
        headers={HOOK_API_KEY_HEADER: "any-key"},
    )

    assert response.status_code == 503
    assert (
        response.json()["error"]["code"]
        == "HOOK_AUTHENTICATION_NOT_CONFIGURED"
    )


def test_an_unresolvable_attachment_denies_instead_of_switching_enforcement_off(
    tmp_path: Path,
) -> None:
    """The other half of the corrupt-marker hole, raised by the cross-model review.

    Falling through on an UNREADABLE marker was the first fix. This is the
    readable-but-unresolvable case: the instruction was understood and names an
    assignment that does not exist. Answering it with an unbound session would
    allow that session everything, so it denies until the marker is corrected.
    """

    repository, runtime = _live_record()
    enforcement = ClaudeCodeSessionEnforcement(
        registry=ClaudeCodeSessionRegistry(),
        assignments=RepositorySupervisorAssignmentGateway(
            repository=repository,
            runtime=runtime,
        ),
    )
    marker = tmp_path / ".writai"
    marker.mkdir()
    (marker / "attach").write_text("ASSIGNMENT-DOES-NOT-EXIST\n", encoding="utf-8")

    response = enforcement.start(
        ClaudeSessionStartRequest(
            session_id="ghost",
            cwd=str(tmp_path),
            branch="chore/no-task",
        )
    )
    assert response.binding.source is SessionBindingSource.UNRESOLVED_ATTACHMENT

    verdict = enforcement.check(_check("ghost"))
    assert verdict.decision is HookPermissionDecision.DENY
    assert verdict.denial_mode is HookDenialMode.UNTIL_REGISTERED
    assert "explicit attachment" in verdict.reason

    # And it is visible as its own state, not hidden among healthy sessions.
    (session,) = enforcement.registered_sessions()
    assert session.assignment_state.bound is False
    assert session.binding.source is SessionBindingSource.UNRESOLVED_ATTACHMENT


def test_a_lost_deny_response_re_delivers_instead_of_allowing(
    tmp_path: Path,
) -> None:
    """INT-3, reproduced: the fail-open that defeated the core mechanic.

    `mark_redirect_delivered` used to run while BUILDING the deny, so the
    assignment advanced to REDIRECTED before the denial had reached anyone. If
    the HTTP response or the hook's stdout was then lost, the next check found a
    current snapshot and ALLOWED — the redirect was never delivered, and the
    agent carried on doing work an approved decision had already invalidated.
    Deny-once became allow-always for the price of one dropped packet.

    Advancement now requires confirmed receipt. This test drops the response and
    asserts the next call denies again with the IDENTICAL redirect.
    """

    repository, runtime = _live_record()
    enforcement = ClaudeCodeSessionEnforcement(
        registry=ClaudeCodeSessionRegistry(),
        assignments=RepositorySupervisorAssignmentGateway(
            repository=repository,
            runtime=runtime,
        ),
    )
    enforcement.start(
        ClaudeSessionStartRequest(
            session_id="lossy",
            cwd=str(tmp_path),
            branch="feat/TASK-102-admin-exports",
        )
    )

    record = repository.get("csv-exports")
    record.graph_version = "graph-v18"
    assert record.supervisor is not None
    assignment = next(
        item for item in record.supervisor.assignments if item.task_id == "TASK-102"
    )
    record.supervisor.assignments = [
        runtime.transition(
            assignment,
            state=SupervisorAssignmentState.INTERRUPTED,
            interrupt_reason="Exports must be admin-only.",
            redirect_instruction="Add the administrator authorization check.",
            interrupt_enforced=True,
        )
        if item.id == assignment.id
        else item
        for item in record.supervisor.assignments
    ]
    repository.save(record)

    # Three consecutive checks, every response lost in transit — the hook never
    # learns the redirect id, so it never acknowledges.
    verdicts = [enforcement.check(_check("lossy")) for _ in range(3)]

    for verdict in verdicts:
        assert verdict.decision is HookPermissionDecision.DENY
        assert verdict.denial_mode is HookDenialMode.UNTIL_ACKNOWLEDGED
        assert verdict.redirect_instruction == (
            "Add the administrator authorization check."
        )
    # Idempotent: the same redirect, identified the same way, every time.
    assert len({verdict.redirect_id for verdict in verdicts}) == 1

    # And the assignment never moved, so nothing was silently consumed.
    (session,) = enforcement.registered_sessions()
    assert session.assignment_state.state is SupervisorAssignmentState.INTERRUPTED
    assert session.assignment_state.deny_spent is False

    # It still TERMINATES. One delivered response ends it.
    allowed = enforcement.check(
        _check("lossy", acknowledged_redirect_id=verdicts[0].redirect_id)
    )
    assert allowed.decision is HookPermissionDecision.ALLOW
    (session,) = enforcement.registered_sessions()
    assert session.assignment_state.state is SupervisorAssignmentState.REDIRECTED
    assert session.assignment_state.deny_spent is True


def test_a_stale_or_forged_acknowledgement_does_not_advance_the_assignment(
    tmp_path: Path,
) -> None:
    """Only the redirect id this service issued for THIS interrupt counts."""

    repository, runtime = _live_record()
    enforcement = ClaudeCodeSessionEnforcement(
        registry=ClaudeCodeSessionRegistry(),
        assignments=RepositorySupervisorAssignmentGateway(
            repository=repository,
            runtime=runtime,
        ),
    )
    enforcement.start(
        ClaudeSessionStartRequest(
            session_id="forger",
            cwd=str(tmp_path),
            branch="feat/TASK-102-admin-exports",
        )
    )
    record = repository.get("csv-exports")
    record.graph_version = "graph-v18"
    assert record.supervisor is not None
    assignment = next(
        item for item in record.supervisor.assignments if item.task_id == "TASK-102"
    )
    record.supervisor.assignments = [
        runtime.transition(
            assignment,
            state=SupervisorAssignmentState.INTERRUPTED,
            interrupt_reason="Exports must be admin-only.",
            redirect_instruction="Add the administrator authorization check.",
            interrupt_enforced=True,
        )
        if item.id == assignment.id
        else item
        for item in record.supervisor.assignments
    ]
    repository.save(record)

    for forged in ("", "not-the-right-id", "sha256:" + "0" * 64):
        verdict = enforcement.check(
            _check("forger", acknowledged_redirect_id=forged or None)
        )
        assert verdict.decision is HookPermissionDecision.DENY, forged

    (session,) = enforcement.registered_sessions()
    assert session.assignment_state.state is SupervisorAssignmentState.INTERRUPTED
