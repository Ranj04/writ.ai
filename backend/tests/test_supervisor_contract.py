"""Seam invariants. Both build lanes depend on these; break one and the other lane breaks."""
from __future__ import annotations

from dragback.supervisor_contract import (
    InterruptRequest,
    InterruptResult,
    NullSupervisorInterruptPort,
    SupervisorInterruptPort,
)
from dragback.workspaces.supervisor import (
    FixtureSupervisorRuntime,
    SupervisorAssignment,
    SupervisorExecutionMode,
    WorkspaceSupervisor,
)


def _request() -> InterruptRequest:
    return InterruptRequest(
        workspace_id="csv-exports",
        decision_id="DEC-018",
        affected_scopes=frozenset({"export.authorization"}),
        provenance_path=("DEC-018", "DEC-004", "SPEC-009", "TICKET-100", "TASK-102"),
        interrupt_reason="Exports must be admin-only.",
        redirect_instruction="Gate the export control behind an administrator check.",
    )


def test_fixture_runtime_still_reports_simulated() -> None:
    """The demo runtime must never claim to control a real provider process."""

    runtime = FixtureSupervisorRuntime()
    assert runtime.execution_mode is SupervisorExecutionMode.SIMULATED
    assert runtime.adapter_name == "fixture-agent-runtime"


def test_execution_mode_defaults_stay_simulated_after_widening() -> None:
    assignment = SupervisorAssignment(
        id="ASSIGNMENT-TASK-102",
        task_id="TASK-102",
        task_title="Expose export to all users",
        agent_name="Export Subagent",
        run_id="RUN-1",
        plan_id="PLAN-027",
        decision_snapshot="graph-v17",
    )
    supervisor = WorkspaceSupervisor(id="SUPERVISOR-CSV")
    assert assignment.execution_mode is SupervisorExecutionMode.SIMULATED
    assert supervisor.execution_mode is SupervisorExecutionMode.SIMULATED


def test_live_execution_mode_exists_and_is_accepted() -> None:
    assert SupervisorExecutionMode.LIVE.value == "live"
    supervisor = WorkspaceSupervisor(
        id="SUPERVISOR-CSV",
        execution_mode=SupervisorExecutionMode.LIVE,
    )
    assert supervisor.execution_mode is SupervisorExecutionMode.LIVE


def test_null_port_satisfies_the_protocol_and_never_claims_a_blast_radius() -> None:
    port: SupervisorInterruptPort = NullSupervisorInterruptPort()
    empty = InterruptResult((), ())
    assert port.preview(_request()) == empty
    assert port.interrupt(_request()) == empty


def test_interrupt_request_is_frozen() -> None:
    """Lane B builds it, Lane A reads it; neither may mutate it in flight."""

    request = _request()
    try:
        request.workspace_id = "other"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("InterruptRequest must be immutable")
