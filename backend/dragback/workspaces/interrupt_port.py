"""The real `SupervisorInterruptPort` binding.

Lane B calls `preview()` to render a blast radius on the confirmation screen and
`interrupt()` once a human with the right permission has approved. This class
replaces `NullSupervisorInterruptPort`; it does not replace the frozen contract.

It issues no verdict. Which assignments an approved change stops is decided by
the authority engine's graph traversal; this port reads that answer out of the
`InvalidationReport` and records the consequence so a `PreToolUse` hook can
enforce it. Before approval there is no report yet, so `preview()` applies the
same validity rule the traversal will apply — a Task stops only when *every*
scope it carries was changed — rather than a looser "any overlap" guess that
would over-report the blast radius.
"""
from __future__ import annotations

from threading import RLock

from dragback.domain import InvalidationReport
from dragback.supervisor_contract import InterruptRequest, InterruptResult
from dragback.workspaces.models import LiveWorkspaceRecord
from dragback.workspaces.repository import LiveWorkspaceRepository
from dragback.workspaces.runtimes.claude_code import ClaudeCodeSupervisorRuntime
from dragback.workspaces.supervisor import (
    AppliedInterrupt,
    SupervisorAssignment,
    SupervisorAssignmentState,
    SupervisorLifecycleState,
    SupervisorRuntimeAdapter,
    WorkspaceSupervisor,
)

# States an approved change can still interrupt. QUEUED work has no live session
# to deny, and REDIRECTED/RESUMED/COMPLETED work has already moved past this
# change. Keep this identical to the orchestrator's own rule, or the previewed
# blast radius stops matching the set that actually gets denied.
_INTERRUPTIBLE_STATES = frozenset(
    {
        SupervisorAssignmentState.RUNNING,
        SupervisorAssignmentState.CONTINUING,
    }
)


class WorkspaceSupervisorInterruptPort:
    """Scope-aware interrupts over persisted Live Workspace supervisor state."""

    def __init__(
        self,
        *,
        repository: LiveWorkspaceRepository,
        runtime: SupervisorRuntimeAdapter | None = None,
    ) -> None:
        self._repository = repository
        self._runtime: SupervisorRuntimeAdapter = (
            runtime if runtime is not None else ClaudeCodeSupervisorRuntime()
        )
        # Guards this process's read-modify-write. The JSON repository is
        # explicitly a single-writer store (see docs/ARCHITECTURE.md), so this is
        # not cross-process mutual exclusion; two agent services writing one store
        # can still lose an interrupt.
        self._lock = RLock()

    # -- target selection ---------------------------------------------------

    @staticmethod
    def _report_for(
        record: LiveWorkspaceRecord,
        decision_id: str,
    ) -> InvalidationReport | None:
        """The authority's own answer, but only if it describes THIS decision."""

        report = record.invalidation_report
        if report is None or report.changed_decision_id != decision_id:
            return None
        return report

    @staticmethod
    def _stops_work(
        assignment: SupervisorAssignment,
        *,
        affected_scopes: frozenset[str],
        report: InvalidationReport | None,
        already_invalidated: frozenset[str] = frozenset(),
    ) -> bool:
        if report is not None:
            # Graph traversal already ran; never second-guess it.
            return assignment.task_id in set(report.invalidated_task_ids)
        # Pre-approval estimate. A Task is INVALIDATED only when *every* scope it
        # carries has been invalidated — and the engine ACCUMULATES: it unions
        # each change into the artifact's invalidated_scopes and invalidates once
        # the union covers the task. So a task scoped {A,B} that already lost A to
        # an earlier decision is fully invalidated by a later change to B.
        #
        # Comparing against this change alone would call that task preserved and
        # then stop it anyway, which is precisely the case where the number on the
        # approver's confirmation screen stops matching reality.
        if not assignment.scopes:
            return False
        return assignment.scopes <= (set(affected_scopes) | set(already_invalidated))

    @staticmethod
    def _already_invalidated(
        supervisor: WorkspaceSupervisor | None,
        decision_id: str,
    ) -> frozenset[str]:
        """Scopes earlier approved decisions already took off this workspace."""

        if supervisor is None:
            return frozenset()
        scopes: set[str] = set()
        for entry in supervisor.applied_interrupts:
            if entry.decision_id != decision_id:
                scopes.update(entry.affected_scopes)
        return frozenset(scopes)

    @classmethod
    def _partition(
        cls,
        supervisor: WorkspaceSupervisor | None,
        *,
        affected_scopes: frozenset[str],
        report: InvalidationReport | None,
        already_invalidated: frozenset[str] = frozenset(),
    ) -> InterruptResult:
        if supervisor is None:
            return InterruptResult((), ())
        interrupted: list[str] = []
        preserved: list[str] = []
        for assignment in supervisor.assignments:
            stops = cls._stops_work(
                assignment,
                affected_scopes=affected_scopes,
                report=report,
                already_invalidated=already_invalidated,
            )
            hit = stops and (
                assignment.state in _INTERRUPTIBLE_STATES
                or assignment.state is SupervisorAssignmentState.INTERRUPTED
            )
            (interrupted if hit else preserved).append(assignment.id)
        return InterruptResult(
            tuple(sorted(interrupted)),
            tuple(sorted(preserved)),
        )

    @staticmethod
    def _provenance_for(
        assignment: SupervisorAssignment,
        *,
        report: InvalidationReport | None,
        fallback: tuple[str, ...],
    ) -> list[str]:
        """This assignment's own lineage, not another task's.

        Every interrupted session is explained with the path that reaches *its*
        task. Handing them all one shared path would explain one engineer's
        interrupt using someone else's work.
        """

        if report is None:
            return list(fallback)
        selected: list[str] = []
        for path in report.paths:
            if assignment.task_id in path.node_ids and len(path.node_ids) > len(
                selected
            ):
                selected = list(path.node_ids)
        return selected or list(fallback)

    # -- port -------------------------------------------------------------

    def preview(self, request: InterruptRequest) -> InterruptResult:
        """Blast radius. Reads a deep copy from the repository and saves nothing."""

        record = self._repository.get(request.workspace_id)
        recorded = self._recorded(record.supervisor, request)
        if recorded is not None:
            return recorded
        return self._partition(
            record.supervisor,
            affected_scopes=request.affected_scopes,
            report=self._report_for(record, request.decision_id),
            already_invalidated=self._already_invalidated(
                record.supervisor, request.decision_id
            ),
        )

    def interrupt(self, request: InterruptRequest) -> InterruptResult:
        """Interrupt intersecting assignments. Idempotent per decision_id."""

        with self._lock:
            record = self._repository.get(request.workspace_id)
            recorded = self._recorded(record.supervisor, request)
            if recorded is not None:
                return recorded
            report = self._report_for(record, request.decision_id)
            result = self._partition(
                record.supervisor,
                affected_scopes=request.affected_scopes,
                report=report,
                already_invalidated=self._already_invalidated(
                    record.supervisor, request.decision_id
                ),
            )
            self._apply(record, request, result, report)
            self._repository.save(record)
            return result

    @staticmethod
    def _recorded(
        supervisor: WorkspaceSupervisor | None,
        request: InterruptRequest,
    ) -> InterruptResult | None:
        """The answer this decision already got, replayed verbatim.

        Idempotency is keyed on the decision AND the scopes it claimed. A retry
        carrying different scopes is a different request; replaying the first
        answer for it would silently return a blast radius that never matched
        what was asked, so it fails loudly instead.
        """

        if supervisor is None:
            return None
        for entry in supervisor.applied_interrupts:
            if entry.decision_id != request.decision_id:
                continue
            if set(entry.affected_scopes) != set(request.affected_scopes):
                raise ValueError(
                    f"decision {request.decision_id} was already applied with "
                    f"scopes {sorted(entry.affected_scopes)}; refusing to replay "
                    f"it for {sorted(request.affected_scopes)}"
                )
            return InterruptResult(
                tuple(entry.interrupted_assignment_ids),
                tuple(entry.preserved_assignment_ids),
            )
        return None

    def _apply(
        self,
        record: LiveWorkspaceRecord,
        request: InterruptRequest,
        result: InterruptResult,
        report: InvalidationReport | None,
    ) -> None:
        supervisor = record.supervisor
        if supervisor is None:
            return
        targets = set(result.interrupted_assignment_ids)
        supervisor.assignments = [
            (
                self._runtime.transition(
                    assignment,
                    state=SupervisorAssignmentState.INTERRUPTED,
                    interrupt_reason=request.interrupt_reason,
                    redirect_instruction=request.redirect_instruction,
                    provenance_path=self._provenance_for(
                        assignment,
                        report=report,
                        fallback=request.provenance_path,
                    ),
                    interrupt_enforced=True,
                )
                if assignment.id in targets
                else assignment
            )
            for assignment in supervisor.assignments
        ]
        supervisor.applied_interrupts.append(
            AppliedInterrupt(
                decision_id=request.decision_id,
                interrupted_assignment_ids=list(result.interrupted_assignment_ids),
                preserved_assignment_ids=list(result.preserved_assignment_ids),
                affected_scopes=sorted(request.affected_scopes),
            )
        )
        if targets:
            supervisor.state = SupervisorLifecycleState.INTERRUPTING
            # The assignments this runtime just touched are enforced by a real
            # hook. Leaving the supervisor labelled "simulated" around live
            # assignments would be exactly the replayed-as-live lie inverted.
            # Label both together or neither. Setting execution_mode while the
            # adapter name stays "fixture-agent-runtime" would produce a LIVE
            # supervisor claiming to be the fixture — the real-vs-simulated lie
            # this repo exists to avoid.
            if self._runtime.adapter_name != "claude-code-hook-runtime":
                raise ValueError(
                    "cannot label supervisor live: unrepresentable adapter "
                    f"{self._runtime.adapter_name!r}"
                )
            supervisor.adapter = "claude-code-hook-runtime"
            supervisor.execution_mode = self._runtime.execution_mode
