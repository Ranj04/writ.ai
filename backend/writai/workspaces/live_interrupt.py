"""Scope-sensitive interrupt port for live Claude Code assignments."""

from __future__ import annotations

from threading import RLock

from writai.hashing import stable_hash
from writai.supervisor_contract import InterruptRequest, InterruptResult
from writai.workspaces.models import LiveWorkspaceRecord, WorkspaceEvent
from writai.workspaces.repository import LiveWorkspaceRepository
from writai.workspaces.runtimes.claude_code import (
    ClaudeCodeSupervisorRuntime,
)
from writai.workspaces.supervisor import (
    SupervisorAssignment,
    SupervisorAssignmentState,
    SupervisorExecutionMode,
    SupervisorLifecycleState,
)


class LiveClaudeCodeInterruptPort:
    """Record approved interrupts without issuing an authority verdict.

    ``interrupt`` persists one event per ``(workspace_id, decision_id)``, so a
    webhook retry or agent-service restart returns the original partition
    without interrupting a redirected session a second time.
    """

    def __init__(
        self,
        *,
        repository: LiveWorkspaceRepository,
        runtime: ClaudeCodeSupervisorRuntime,
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._lock = RLock()

    def preview(self, request: InterruptRequest) -> InterruptResult:
        """Return the live assignment partition without mutating it."""

        with self._lock:
            assignments = self._live_assignments(request.workspace_id)
            return _partition(assignments, request)

    def interrupt(self, request: InterruptRequest) -> InterruptResult:
        """Interrupt exactly the live assignments whose scopes intersect."""

        if not request.affected_scopes:
            raise ValueError("an interrupt must carry at least one affected scope")
        with self._lock:
            record = self._repository.get(request.workspace_id)
            completed = _completed_interrupt(record, request)
            if completed is not None:
                return completed
            supervisor = record.supervisor
            if (
                supervisor is None
                or supervisor.execution_mode
                is not SupervisorExecutionMode.LIVE
            ):
                raise RuntimeError(
                    "the workspace is not bound to the live Claude Code runtime"
            )
            result = _partition(supervisor.assignments, request)
            interrupted = set(result.interrupted_assignment_ids)
            preserved = set(result.preserved_assignment_ids)
            changed: list[SupervisorAssignment] = []
            for assignment in supervisor.assignments:
                if assignment.id in interrupted:
                    changed.append(
                        self._runtime.transition(
                            assignment,
                            state=SupervisorAssignmentState.INTERRUPTED,
                            interrupt_reason=request.interrupt_reason,
                            redirect_instruction=request.redirect_instruction,
                            provenance_path=list(request.provenance_path),
                            interrupt_enforced=True,
                        )
                    )
                    continue
                if (
                    assignment.id in preserved
                    and assignment.state is SupervisorAssignmentState.RUNNING
                ):
                    changed.append(
                        self._runtime.transition(
                            assignment,
                            state=SupervisorAssignmentState.CONTINUING,
                        )
                    )
                    continue
                changed.append(assignment)
            supervisor.assignments = changed
            if interrupted:
                supervisor.state = SupervisorLifecycleState.INTERRUPTING
            record.history.append(
                WorkspaceEvent(
                    sequence=len(record.history) + 1,
                    event_type="supervisor.interrupt.enforced",
                    detail=(
                        f"Decision {request.decision_id} interrupted "
                        f"{len(interrupted)} live Claude Code assignments and "
                        f"preserved {len(preserved)}."
                    ),
                    data={
                        "decision_id": request.decision_id,
                        "request_fingerprint": _request_fingerprint(request),
                        "interrupted_assignment_ids": list(
                            result.interrupted_assignment_ids
                        ),
                        "preserved_assignment_ids": list(
                            result.preserved_assignment_ids
                        ),
                    },
                )
            )
            self._repository.save(record)
            return result

    def _live_assignments(
        self,
        workspace_id: str,
    ) -> list[SupervisorAssignment]:
        record = self._repository.get(workspace_id)
        supervisor = record.supervisor
        if (
            supervisor is None
            or supervisor.execution_mode
            is not SupervisorExecutionMode.LIVE
        ):
            raise RuntimeError(
                "the workspace is not bound to the live Claude Code runtime"
            )
        return supervisor.assignments


def _partition(
    assignments: list[SupervisorAssignment],
    request: InterruptRequest,
) -> InterruptResult:
    active = [
        assignment
        for assignment in assignments
        if assignment.execution_mode is SupervisorExecutionMode.LIVE
        and assignment.state is not SupervisorAssignmentState.COMPLETED
    ]
    interrupted = sorted(
        assignment.id
        for assignment in active
        if bool(assignment.scopes & request.affected_scopes)
    )
    preserved = sorted(
        assignment.id
        for assignment in active
        if not assignment.scopes & request.affected_scopes
    )
    return InterruptResult(
        interrupted_assignment_ids=tuple(interrupted),
        preserved_assignment_ids=tuple(preserved),
    )


def _completed_interrupt(
    record: LiveWorkspaceRecord,
    request: InterruptRequest,
) -> InterruptResult | None:
    for event in reversed(record.history):
        if (
            event.event_type != "supervisor.interrupt.enforced"
            or event.data.get("decision_id") != request.decision_id
        ):
            continue
        if event.data.get("request_fingerprint") != _request_fingerprint(request):
            raise ValueError(
                "the decision ID was already used for a different interrupt"
            )
        interrupted = event.data.get("interrupted_assignment_ids")
        preserved = event.data.get("preserved_assignment_ids")
        if not isinstance(interrupted, list) or not isinstance(preserved, list):
            raise RuntimeError("the persisted interrupt record is malformed")
        if not all(isinstance(item, str) for item in [*interrupted, *preserved]):
            raise RuntimeError("the persisted interrupt record is malformed")
        return InterruptResult(
            interrupted_assignment_ids=tuple(interrupted),
            preserved_assignment_ids=tuple(preserved),
        )
    return None


def _request_fingerprint(request: InterruptRequest) -> str:
    return stable_hash(
        {
            "workspace_id": request.workspace_id,
            "decision_id": request.decision_id,
            "affected_scopes": sorted(request.affected_scopes),
            "provenance_path": list(request.provenance_path),
            "interrupt_reason": request.interrupt_reason,
            "redirect_instruction": request.redirect_instruction,
        }
    )
