"""Claude Code runtime state recorded for the synchronous hook boundary."""

from __future__ import annotations

from typing import Literal

from writai.domain import AgentPlan, Artifact, PlanAction
from writai.workspaces.supervisor import (
    FixtureSupervisorRuntime,
    SupervisorAssignment,
    SupervisorAssignmentState,
    SupervisorExecutionMode,
    SupervisorRuntimeProvider,
    WorkspaceSupervisor,
)


class ClaudeCodeSupervisorRuntime:
    """Live state adapter consumed by Claude Code lifecycle hooks.

    The adapter records assignment state only. It does not contact an authority,
    inspect model output, or return an authority verdict. The synchronous
    ``PreToolUse`` hook is the process-control boundary.
    """

    adapter_name: Literal["claude-code-hook-runtime"] = (
        "claude-code-hook-runtime"
    )
    execution_mode: Literal[SupervisorExecutionMode.LIVE] = (
        SupervisorExecutionMode.LIVE
    )

    def __init__(self) -> None:
        self._state_adapter = FixtureSupervisorRuntime()

    def create_supervisor(
        self,
        *,
        workspace_id: str,
        supervisor_run_id: str,
        tasks: list[Artifact],
        plan: AgentPlan,
        decision_snapshot: str,
    ) -> WorkspaceSupervisor:
        """Create the same deterministic assignments, explicitly marked live."""

        supervisor = self._state_adapter.create_supervisor(
            workspace_id=workspace_id,
            supervisor_run_id=supervisor_run_id,
            tasks=tasks,
            plan=plan,
            decision_snapshot=decision_snapshot,
        )
        assignments = [
            assignment.model_copy(
                update={
                    "execution_mode": _execution_mode_for(assignment),
                },
                deep=True,
            )
            for assignment in supervisor.assignments
        ]
        return supervisor.model_copy(
            update={
                "adapter": self.adapter_name,
                "execution_mode": SupervisorExecutionMode.LIVE,
                "assignments": assignments,
            },
            deep=True,
        )

    def transition(
        self,
        assignment: SupervisorAssignment,
        *,
        state: SupervisorAssignmentState,
        plan: AgentPlan | None = None,
        plan_actions: list[PlanAction] | None = None,
        decision_snapshot: str | None = None,
        create_replacement_run: bool = False,
        interrupt_reason: str | None = None,
        redirect_instruction: str | None = None,
        provenance_path: list[str] | None = None,
        interrupt_enforced: bool | None = None,
    ) -> SupervisorAssignment:
        """Record a live transition without deciding whether it is authorized."""

        transitioned = self._state_adapter.transition(
            assignment,
            state=state,
            plan=plan,
            plan_actions=plan_actions,
            decision_snapshot=decision_snapshot,
            create_replacement_run=create_replacement_run,
            interrupt_reason=interrupt_reason,
            redirect_instruction=redirect_instruction,
            provenance_path=provenance_path,
            interrupt_enforced=interrupt_enforced,
        )
        return transitioned.model_copy(
            update={
                "execution_mode": _execution_mode_for(transitioned),
            },
            deep=True,
        )


def _execution_mode_for(
    assignment: SupervisorAssignment,
) -> SupervisorExecutionMode:
    if assignment.runtime_provider is SupervisorRuntimeProvider.CODEX:
        # v1 has no synchronous Codex enforcement hook. Preserve an honest
        # simulated label instead of claiming the Claude adapter controls it.
        return SupervisorExecutionMode.SIMULATED
    return SupervisorExecutionMode.LIVE
