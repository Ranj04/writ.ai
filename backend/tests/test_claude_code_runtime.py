"""A1 — the live runtime adapter and the real SupervisorInterruptPort binding."""
from __future__ import annotations

import pytest
from dragback.domain import (
    AgentPlan,
    Artifact,
    ArtifactKind,
    InvalidationPath,
    InvalidationReport,
    PlanAction,
)
from dragback.supervisor_contract import (
    InterruptRequest,
    InterruptResult,
    SupervisorInterruptPort,
)
from dragback.workspaces.interrupt_port import WorkspaceSupervisorInterruptPort
from dragback.workspaces.models import LiveWorkspaceRecord
from dragback.workspaces.repository import LiveWorkspaceNotFound
from dragback.workspaces.runtimes.claude_code import ClaudeCodeSupervisorRuntime
from dragback.workspaces.supervisor import (
    FixtureSupervisorRuntime,
    SupervisorAssignmentState,
    SupervisorExecutionMode,
    SupervisorLifecycleState,
    SupervisorRuntimeAdapter,
)

WORKSPACE_ID = "csv-exports"


def _tasks() -> list[Artifact]:
    return [
        Artifact(
            id="TASK-101",
            kind=ArtifactKind.TASK,
            title="Generate CSV files",
            scopes={"export.generation"},
        ),
        Artifact(
            id="TASK-102",
            kind=ArtifactKind.TASK,
            title="Expose export to all users",
            scopes={"export.authorization"},
            attributes={"runtime_provider": "claude-code"},
        ),
    ]


def _plan() -> AgentPlan:
    return AgentPlan(
        id="PLAN-027",
        ticket_id="TICKET-100",
        objective="Add CSV export for all users.",
        actions=[
            PlanAction(
                id="ACTION-1",
                description="Generate a valid CSV file.",
                scopes={"export.generation"},
                attributes={"task_id": "TASK-101"},
            ),
            PlanAction(
                id="ACTION-2",
                description="Expose the export control to all users.",
                scopes={"export.authorization"},
                attributes={"task_id": "TASK-102"},
            ),
        ],
    )


class _MemoryRepository:
    """Repository double with the same deep-copy semantics as the JSON store."""

    def __init__(self) -> None:
        self._records: dict[str, LiveWorkspaceRecord] = {}
        self.saves = 0

    def create(self, record: LiveWorkspaceRecord) -> None:
        self._records[record.definition.id] = record.model_copy(deep=True)

    def save(self, record: LiveWorkspaceRecord) -> None:
        if record.definition.id not in self._records:
            raise LiveWorkspaceNotFound(record.definition.id)
        self.saves += 1
        self._records[record.definition.id] = record.model_copy(deep=True)

    def get(self, workspace_id: str) -> LiveWorkspaceRecord:
        record = self._records.get(workspace_id)
        if record is None:
            raise LiveWorkspaceNotFound(workspace_id)
        return record.model_copy(deep=True)

    def list(self) -> list[LiveWorkspaceRecord]:
        return [record.model_copy(deep=True) for record in self._records.values()]


def _record(repository: _MemoryRepository) -> LiveWorkspaceRecord:
    """Build a record carrying only what the interrupt port reads."""

    runtime = ClaudeCodeSupervisorRuntime()
    supervisor = runtime.create_supervisor(
        workspace_id=WORKSPACE_ID,
        supervisor_run_id=f"LIVE-{WORKSPACE_ID.upper()}-RUN",
        tasks=_tasks(),
        plan=_plan(),
        decision_snapshot="graph-v17",
    )
    supervisor.assignments = [
        runtime.transition(
            assignment,
            state=SupervisorAssignmentState.RUNNING,
            decision_snapshot="graph-v17",
        )
        for assignment in supervisor.assignments
    ]
    supervisor.state = SupervisorLifecycleState.RUNNING
    record = LiveWorkspaceRecord.model_construct(
        definition=_Definition(WORKSPACE_ID),
        context_id=f"ctx-{WORKSPACE_ID}",
        graph_version="graph-v17",
        current_plan=_plan(),
        supervisor=supervisor,
    )
    repository.create(record)
    return record


class _Definition:
    """Minimal stand-in for the import request; only `.id` is read here."""

    def __init__(self, workspace_id: str) -> None:
        self.id = workspace_id


def _request(*, scopes: set[str], decision_id: str = "DEC-018") -> InterruptRequest:
    return InterruptRequest(
        workspace_id=WORKSPACE_ID,
        decision_id=decision_id,
        affected_scopes=frozenset(scopes),
        provenance_path=("DEC-018", "DEC-004", "SPEC-009", "TICKET-100", "TASK-102"),
        interrupt_reason="Approved decision DEC-018 changed export.authorization.",
        redirect_instruction="Gate the export control behind an administrator check.",
    )


def test_runtime_satisfies_the_protocol_and_reports_live() -> None:
    runtime: SupervisorRuntimeAdapter = ClaudeCodeSupervisorRuntime()
    assert runtime.execution_mode is SupervisorExecutionMode.LIVE
    assert runtime.adapter_name == "claude-code-hook-runtime"
    # The fixture must be unaffected by the second adapter existing.
    assert FixtureSupervisorRuntime().execution_mode is SupervisorExecutionMode.SIMULATED


def test_live_supervisor_and_assignments_are_labelled_live() -> None:
    supervisor = ClaudeCodeSupervisorRuntime().create_supervisor(
        workspace_id=WORKSPACE_ID,
        supervisor_run_id="LIVE-CSV-RUN",
        tasks=_tasks(),
        plan=_plan(),
        decision_snapshot="graph-v17",
    )
    assert supervisor.adapter == "claude-code-hook-runtime"
    assert supervisor.execution_mode is SupervisorExecutionMode.LIVE
    assert [assignment.id for assignment in supervisor.assignments] == [
        "ASSIGNMENT-TASK-101",
        "ASSIGNMENT-TASK-102",
    ]
    assert all(
        assignment.execution_mode is SupervisorExecutionMode.LIVE
        for assignment in supervisor.assignments
    )


def test_live_and_fixture_record_identical_state_apart_from_mode() -> None:
    """The live adapter differs by what reads it, not by what it records."""

    common = {
        "workspace_id": WORKSPACE_ID,
        "supervisor_run_id": "LIVE-CSV-RUN",
        "tasks": _tasks(),
        "plan": _plan(),
        "decision_snapshot": "graph-v17",
    }
    fixture = FixtureSupervisorRuntime().create_supervisor(**common)  # type: ignore[arg-type]
    live = ClaudeCodeSupervisorRuntime().create_supervisor(**common)  # type: ignore[arg-type]
    ignored = {"execution_mode", "adapter"}
    assert fixture.model_dump(exclude=ignored | {"assignments"}) == live.model_dump(
        exclude=ignored | {"assignments"}
    )
    assert [
        assignment.model_dump(exclude=ignored) for assignment in fixture.assignments
    ] == [assignment.model_dump(exclude=ignored) for assignment in live.assignments]


def test_adopting_a_simulated_assignment_relabels_it_live() -> None:
    """Never label a replayed payload as live — and never label a live one simulated."""

    simulated = FixtureSupervisorRuntime().create_supervisor(
        workspace_id=WORKSPACE_ID,
        supervisor_run_id="LIVE-CSV-RUN",
        tasks=_tasks(),
        plan=_plan(),
        decision_snapshot="graph-v17",
    )
    adopted = ClaudeCodeSupervisorRuntime().transition(
        simulated.assignments[0],
        state=SupervisorAssignmentState.RUNNING,
    )
    assert simulated.assignments[0].execution_mode is SupervisorExecutionMode.SIMULATED
    assert adopted.execution_mode is SupervisorExecutionMode.LIVE


def test_preview_reports_the_scope_intersection_and_mutates_nothing() -> None:
    repository = _MemoryRepository()
    _record(repository)
    port: SupervisorInterruptPort = WorkspaceSupervisorInterruptPort(
        repository=repository
    )

    result = port.preview(_request(scopes={"export.authorization"}))

    assert result == InterruptResult(
        ("ASSIGNMENT-TASK-102",),
        ("ASSIGNMENT-TASK-101",),
    )
    assert repository.saves == 0
    stored = repository.get(WORKSPACE_ID)
    assert stored.supervisor is not None
    assert all(
        assignment.state is SupervisorAssignmentState.RUNNING
        for assignment in stored.supervisor.assignments
    )


def test_interrupt_transitions_only_the_intersecting_assignment() -> None:
    repository = _MemoryRepository()
    _record(repository)
    port = WorkspaceSupervisorInterruptPort(repository=repository)

    result = port.interrupt(_request(scopes={"export.authorization"}))

    assert result.interrupted_assignment_ids == ("ASSIGNMENT-TASK-102",)
    assert result.preserved_assignment_ids == ("ASSIGNMENT-TASK-101",)
    stored = repository.get(WORKSPACE_ID)
    assert stored.supervisor is not None
    by_id = {a.id: a for a in stored.supervisor.assignments}
    interrupted = by_id["ASSIGNMENT-TASK-102"]
    preserved = by_id["ASSIGNMENT-TASK-101"]
    assert interrupted.state is SupervisorAssignmentState.INTERRUPTED
    assert interrupted.interrupt_enforced is True
    assert interrupted.provenance_path[0] == "DEC-018"
    assert interrupted.redirect_instruction is not None
    # The out-of-scope sibling survives untouched — a person, this time.
    assert preserved.state is SupervisorAssignmentState.RUNNING
    assert preserved.interrupt_enforced is False
    assert preserved.interrupt_reason is None


def test_preview_equals_the_set_interrupt_actually_changes() -> None:
    repository = _MemoryRepository()
    _record(repository)
    port = WorkspaceSupervisorInterruptPort(repository=repository)
    request = _request(scopes={"export.authorization"})

    previewed = port.preview(request)
    applied = port.interrupt(request)

    assert previewed == applied


def test_interrupt_is_idempotent_per_decision_id() -> None:
    repository = _MemoryRepository()
    _record(repository)
    port = WorkspaceSupervisorInterruptPort(repository=repository)
    request = _request(scopes={"export.authorization"})

    first = port.interrupt(request)
    saves_after_first = repository.saves
    second = port.interrupt(request)

    assert first == second
    assert repository.saves == saves_after_first
    stored = repository.get(WORKSPACE_ID)
    assert stored.supervisor is not None
    states = [assignment.state for assignment in stored.supervisor.assignments]
    assert states.count(SupervisorAssignmentState.INTERRUPTED) == 1


def test_a_replayed_decision_after_restart_returns_the_same_partition() -> None:
    """Idempotency survives a restart: the answer is persisted beside the state."""

    repository = _MemoryRepository()
    _record(repository)
    request = _request(scopes={"export.authorization"})
    first = WorkspaceSupervisorInterruptPort(repository=repository).interrupt(request)

    restarted = WorkspaceSupervisorInterruptPort(repository=repository)
    saves_before = repository.saves
    assert restarted.interrupt(request) == first
    assert repository.saves == saves_before


def test_a_replay_after_redirect_does_not_re_interrupt() -> None:
    """The dangerous replay: the session already complied and moved on."""

    repository = _MemoryRepository()
    _record(repository)
    request = _request(scopes={"export.authorization"})
    first = WorkspaceSupervisorInterruptPort(repository=repository).interrupt(request)

    # The developer's agent took the redirect and resumed under the corrected plan.
    record = repository.get(WORKSPACE_ID)
    assert record.supervisor is not None
    for assignment in record.supervisor.assignments:
        if assignment.id in first.interrupted_assignment_ids:
            assignment.state = SupervisorAssignmentState.RESUMED
    repository.save(record)

    replayed = WorkspaceSupervisorInterruptPort(repository=repository).interrupt(request)

    assert replayed == first
    stored = repository.get(WORKSPACE_ID)
    assert stored.supervisor is not None
    assert all(
        assignment.state is not SupervisorAssignmentState.INTERRUPTED
        for assignment in stored.supervisor.assignments
    )


def test_a_non_intersecting_change_interrupts_nobody() -> None:
    repository = _MemoryRepository()
    _record(repository)
    port = WorkspaceSupervisorInterruptPort(repository=repository)

    result = port.interrupt(
        _request(scopes={"billing.refunds"}, decision_id="DEC-999")
    )

    assert result.interrupted_assignment_ids == ()
    assert result.preserved_assignment_ids == (
        "ASSIGNMENT-TASK-101",
        "ASSIGNMENT-TASK-102",
    )
    stored = repository.get(WORKSPACE_ID)
    assert stored.supervisor is not None
    assert all(
        assignment.state is SupervisorAssignmentState.RUNNING
        for assignment in stored.supervisor.assignments
    )
    assert stored.supervisor.execution_mode is SupervisorExecutionMode.LIVE


def test_a_partially_affected_task_is_not_interrupted() -> None:
    """Partial scope overlap is NEEDS_REVIEW, not INVALIDATED. It keeps working."""

    repository = _MemoryRepository()
    record = _record(repository)
    assert record.supervisor is not None
    record.supervisor.assignments[1].scopes = {
        "export.authorization",
        "export.audit",
    }
    repository.save(record)
    port = WorkspaceSupervisorInterruptPort(repository=repository)

    result = port.preview(_request(scopes={"export.authorization"}))

    assert result.interrupted_assignment_ids == ()
    assert result.preserved_assignment_ids == (
        "ASSIGNMENT-TASK-101",
        "ASSIGNMENT-TASK-102",
    )


def test_an_applied_report_overrides_the_pre_approval_estimate() -> None:
    """Once the graph has spoken, the port never second-guesses the traversal."""

    repository = _MemoryRepository()
    record = _record(repository)
    record.invalidation_report = InvalidationReport(
        graph_version="graph-v18",
        changed_decision_id="DEC-018",
        superseded_decision_id="DEC-004",
        affected_scopes={"export.authorization"},
        preserved_task_ids=["TASK-102"],
        invalidated_task_ids=["TASK-101"],
    )
    repository.save(record)
    port = WorkspaceSupervisorInterruptPort(repository=repository)

    result = port.interrupt(_request(scopes={"export.authorization"}))

    # Scope intersection alone would have chosen TASK-102; the report says TASK-101.
    assert result.interrupted_assignment_ids == ("ASSIGNMENT-TASK-101",)


def test_each_interrupted_assignment_gets_its_own_provenance_path() -> None:
    repository = _MemoryRepository()
    record = _record(repository)
    record.invalidation_report = InvalidationReport(
        graph_version="graph-v18",
        changed_decision_id="DEC-018",
        superseded_decision_id="DEC-004",
        affected_scopes={"export.authorization", "export.generation"},
        invalidated_task_ids=["TASK-101", "TASK-102"],
        paths=[
            InvalidationPath(
                artifact_id="TASK-101",
                node_ids=["DEC-018", "SPEC-009", "TICKET-100", "TASK-101"],
            ),
            InvalidationPath(
                artifact_id="TASK-102",
                node_ids=["DEC-018", "DEC-004", "SPEC-009", "TICKET-100", "TASK-102"],
            ),
        ],
    )
    repository.save(record)
    port = WorkspaceSupervisorInterruptPort(repository=repository)

    port.interrupt(
        _request(scopes={"export.authorization", "export.generation"})
    )

    stored = repository.get(WORKSPACE_ID)
    assert stored.supervisor is not None
    paths = {a.task_id: a.provenance_path for a in stored.supervisor.assignments}
    assert paths["TASK-101"][-1] == "TASK-101"
    assert paths["TASK-102"][-1] == "TASK-102"
    assert paths["TASK-101"] != paths["TASK-102"]


def test_an_unknown_workspace_fails_closed() -> None:
    port = WorkspaceSupervisorInterruptPort(repository=_MemoryRepository())
    with pytest.raises(LiveWorkspaceNotFound):
        port.preview(_request(scopes={"export.authorization"}))


def test_preview_accounts_for_scopes_an_earlier_decision_already_took() -> None:
    """The engine accumulates invalidated scopes; the preview must too.

    A task scoped {authorization, audit} that already lost `authorization` to an
    earlier approved decision is fully invalidated by a later change to `audit`.
    Comparing against the new change alone would call it preserved and then stop
    it anyway — the previewed number on the approver's screen would be wrong.
    """

    repository = _MemoryRepository()
    record = _record(repository)
    assert record.supervisor is not None
    record.supervisor.assignments[1].scopes = {"export.authorization", "export.audit"}
    repository.save(record)
    port = WorkspaceSupervisorInterruptPort(repository=repository)

    # DEC-018 takes export.authorization. The task survives: audit is untouched.
    first = port.interrupt(_request(scopes={"export.authorization"}))
    assert first.interrupted_assignment_ids == ()

    # DEC-019 now takes export.audit. Together the two cover the task entirely.
    second = port.preview(
        _request(scopes={"export.audit"}, decision_id="DEC-019")
    )
    assert second.interrupted_assignment_ids == ("ASSIGNMENT-TASK-102",)


def test_replaying_a_decision_with_different_scopes_is_refused() -> None:
    """A retry that claims different scopes is a different request."""

    repository = _MemoryRepository()
    _record(repository)
    port = WorkspaceSupervisorInterruptPort(repository=repository)
    port.interrupt(_request(scopes={"export.authorization"}))

    # Same decision id, different scopes: replaying the first answer would report
    # a blast radius that never matched what was asked.
    with pytest.raises(ValueError, match="refusing to replay"):
        port.interrupt(_request(scopes={"export.generation"}))
