from dragback.domain import AgentPlan, Artifact, ArtifactKind, PlanAction
from dragback.workspaces.supervisor import (
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
