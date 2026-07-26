"""Typed session registry facade for Claude Code lifecycle hooks."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from threading import RLock
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dragback.hashing import stable_hash
from dragback.workspaces.models import LiveWorkspaceRecord
from dragback.workspaces.repository import (
    LiveWorkspaceNotFound,
    LiveWorkspaceRepository,
)
from dragback.workspaces.runtimes.claude_code import (
    ClaudeCodeSupervisorRuntime,
)
from dragback.workspaces.session_binding import (
    AssignmentLocator,
    ClaudeCodeSessionBinding,
    ClaudeCodeSessionRegistry,
    SessionBindingSource,
    SupervisorAssignmentTarget,
)
from dragback.workspaces.supervisor import (
    SupervisorAssignment,
    SupervisorAssignmentState,
    SupervisorExecutionMode,
    SupervisorLifecycleState,
    SupervisorRuntimeProvider,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class HookPermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class HookDenialMode(StrEnum):
    ONCE = "once"
    UNTIL_ACKNOWLEDGED = "until-acknowledged"
    UNTIL_REGISTERED = "until-registered"


class ClaudeSessionStartRequest(_FrozenModel):
    session_id: str = Field(min_length=1, max_length=255)
    cwd: str = Field(min_length=1, max_length=4096)
    branch: str = Field(default="", max_length=1024)


class ClaudeSessionEndRequest(_FrozenModel):
    session_id: str = Field(min_length=1, max_length=255)


class ClaudePreToolUseRequest(_FrozenModel):
    """Privacy boundary: the hook may transmit exactly these four fields.

    ``acknowledged_redirect_id`` is the only addition to the original three, and
    it is not developer data: it is an identifier **this service issued**, echoed
    back to confirm that the redirect reached the agent. It carries no tool
    input, file contents, transcript, cwd or permission mode, and the hook can
    only ever send a value it was given.
    """

    session_id: str = Field(min_length=1, max_length=255)
    tool_name: str = Field(min_length=1, max_length=255)
    timestamp: datetime
    #: The ``redirect_id`` from a previous deny that the hook confirms it
    #: delivered. See ``ClaudeCodeSessionEnforcement.check``.
    acknowledged_redirect_id: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_timestamp(self) -> ClaudePreToolUseRequest:
        if self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return self


class ClaudeSessionStartResponse(_FrozenModel):
    binding: ClaudeCodeSessionBinding


class ClaudeSessionEndResponse(_FrozenModel):
    released: bool


class ClaudeHookVerdict(_FrozenModel):
    """A deterministic session-gate decision, never an authority verdict."""

    decision: HookPermissionDecision
    reason: str = Field(min_length=1, max_length=1000)
    workspace_id: str | None = Field(default=None, max_length=255)
    assignment_id: str | None = Field(default=None, max_length=255)
    task_id: str | None = Field(default=None, max_length=255)
    binding_source: SessionBindingSource | None = None
    denial_mode: HookDenialMode | None = None
    redirect_instruction: str | None = Field(default=None, max_length=8000)
    provenance_path: tuple[str, ...] = Field(default=(), max_length=100)
    evidence_ref: str | None = Field(default=None, max_length=1000)
    #: Set when this deny carries a redirect the agent must be told about. The
    #: hook echoes it back on its NEXT check, and only then does the assignment
    #: advance. Until that confirmation arrives the same redirect is re-delivered
    #: verbatim, so a lost response costs one repeat instead of a silent allow.
    redirect_id: str | None = Field(default=None, max_length=255)


class ClaudeSessionAcknowledgement(_FrozenModel):
    acknowledged: bool
    interrupt_key: str = Field(min_length=1, max_length=255)


class AssignmentEnforcementSnapshot(_FrozenModel):
    locator: AssignmentLocator
    assignment: SupervisorAssignment
    current_decision_snapshot: str = Field(min_length=1, max_length=255)
    evidence_ref: str | None = Field(default=None, max_length=1000)


#: Deny-once is per *assignment*. Once an assignment has reached one of these
#: states its deny has been spent: `check` falls past the INTERRUPTED branch,
#: finds the snapshot already advanced by `mark_redirect_delivered`, and allows.
#: Firing an approved change again allows it straight through. A stage re-armed
#: without a full re-seed therefore looks identical to a broken product, which
#: is why this set is named once here rather than restated by each consumer.
#:
#: INTERRUPTED is deliberately NOT in this set. It is the *armed* state, not a
#: spent one: `check` denies on it every time, either once with the redirect
#: instruction or until a human acknowledges. Verified against a running
#: service — an interrupted assignment denies, and only then becomes REDIRECTED.
#: Treating it as spent would report a correctly armed stage as broken.
SPENT_DENY_STATES = frozenset(
    {
        SupervisorAssignmentState.REDIRECTED,
        SupervisorAssignmentState.RESUMED,
        SupervisorAssignmentState.COMPLETED,
    }
)


class SessionAssignmentState(_FrozenModel):
    """The assignment facts behind one registered session.

    A ``ClaudeCodeSessionBinding`` carries ids, not state, so every consumer that
    needed the state — the dev CLI, the stage readiness check — used to make a
    second ``GET /live-workspaces/{id}`` round trip per workspace to find it, and
    could disagree with the verdict the hook would actually get. These fields are
    read through the same gateway ``check`` reads, so the list and the verdict
    cannot drift apart.

    Every field is a *fact*, not a verdict. ``check`` remains the only place a
    permission decision is made.
    """

    bound: bool = False
    #: True when the session is bound but the gateway can no longer find its
    #: assignment. That denies until acknowledged, so it is not a quiet gap.
    assignment_missing: bool = False
    state: SupervisorAssignmentState | None = None
    decision_snapshot: str | None = Field(default=None, max_length=255)
    current_decision_snapshot: str | None = Field(default=None, max_length=255)
    #: Whether the assignment is pinned to the workspace's current graph version.
    #: This is the gate ``check`` applies, restated as a fact.
    snapshot_current: bool = False
    #: See ``SPENT_DENY_STATES``.
    deny_spent: bool = False

    @classmethod
    def unbound(cls) -> SessionAssignmentState:
        return cls()

    @classmethod
    def missing(cls) -> SessionAssignmentState:
        return cls(bound=True, assignment_missing=True)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: AssignmentEnforcementSnapshot,
    ) -> SessionAssignmentState:
        assignment = snapshot.assignment
        return cls(
            bound=True,
            state=assignment.state,
            decision_snapshot=assignment.decision_snapshot,
            current_decision_snapshot=snapshot.current_decision_snapshot,
            snapshot_current=(
                assignment.decision_snapshot == snapshot.current_decision_snapshot
            ),
            deny_spent=assignment.state in SPENT_DENY_STATES,
        )


class RegisteredSession(_FrozenModel):
    """One registered session: where it is bound, and what state that binding is in."""

    binding: ClaudeCodeSessionBinding
    assignment_state: SessionAssignmentState

    def to_payload(self) -> dict[str, object]:
        """Flat wire shape: binding fields, plus the assignment facts.

        Flat rather than nested because the existing readers accept either a
        bare binding or one wrapped in ``{"binding": ...}`` and already read
        ``session_id`` at the top level. Adding keys cannot break them; moving
        the existing ones would.
        """

        payload = self.binding.model_dump(mode="json")
        payload.update(self.assignment_state.model_dump(mode="json"))
        return payload


class SupervisorAssignmentGateway(Protocol):
    """Persistence seam used by the hook service and the agent API router."""

    def list_live_claude_assignments(
        self,
    ) -> list[SupervisorAssignmentTarget]: ...

    def get(
        self,
        locator: AssignmentLocator,
    ) -> AssignmentEnforcementSnapshot | None: ...

    def mark_redirect_delivered(
        self,
        locator: AssignmentLocator,
        *,
        expected_run_id: str,
    ) -> AssignmentEnforcementSnapshot | None: ...


class RepositorySupervisorAssignmentGateway:
    """Atomic assignment access over the existing Live Workspace repository."""

    def __init__(
        self,
        *,
        repository: LiveWorkspaceRepository,
        runtime: ClaudeCodeSupervisorRuntime,
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._lock = RLock()

    def list_live_claude_assignments(
        self,
    ) -> list[SupervisorAssignmentTarget]:
        with self._lock:
            targets: list[SupervisorAssignmentTarget] = []
            for record in self._repository.list():
                supervisor = record.supervisor
                if (
                    supervisor is None
                    or supervisor.execution_mode
                    is not SupervisorExecutionMode.LIVE
                ):
                    continue
                for assignment in supervisor.assignments:
                    if (
                        assignment.execution_mode
                        is not SupervisorExecutionMode.LIVE
                        or assignment.runtime_provider
                        not in {
                            SupervisorRuntimeProvider.GENERIC,
                            SupervisorRuntimeProvider.CLAUDE_CODE,
                        }
                        or assignment.state
                        is SupervisorAssignmentState.COMPLETED
                    ):
                        continue
                    targets.append(
                        SupervisorAssignmentTarget(
                            workspace_id=record.definition.id,
                            assignment=assignment.model_copy(deep=True),
                        )
                    )
            return sorted(
                targets,
                key=lambda item: (item.workspace_id, item.assignment_id),
            )

    def get(
        self,
        locator: AssignmentLocator,
    ) -> AssignmentEnforcementSnapshot | None:
        with self._lock:
            try:
                record = self._repository.get(locator.workspace_id)
            except LiveWorkspaceNotFound:
                return None
            supervisor = record.supervisor
            if (
                supervisor is None
                or supervisor.execution_mode
                is not SupervisorExecutionMode.LIVE
            ):
                return None
            assignment = next(
                (
                    item
                    for item in supervisor.assignments
                    if item.id == locator.assignment_id
                    and item.task_id == locator.task_id
                    and item.execution_mode is SupervisorExecutionMode.LIVE
                ),
                None,
            )
            if assignment is None:
                return None
            return _snapshot(record, locator, assignment)

    def mark_redirect_delivered(
        self,
        locator: AssignmentLocator,
        *,
        expected_run_id: str,
    ) -> AssignmentEnforcementSnapshot | None:
        """Advance the hook-visible snapshot after one redirect delivery.

        This is intentionally weaker than plan/grant verification: the hook has
        no session plan to re-evaluate. A protected-branch/PR verification check
        remains the backstop.
        """

        with self._lock:
            try:
                record = self._repository.get(locator.workspace_id)
            except LiveWorkspaceNotFound:
                return None
            supervisor = record.supervisor
            if (
                supervisor is None
                or supervisor.execution_mode
                is not SupervisorExecutionMode.LIVE
            ):
                return None
            index = next(
                (
                    index
                    for index, item in enumerate(supervisor.assignments)
                    if item.id == locator.assignment_id
                    and item.task_id == locator.task_id
                    and item.execution_mode is SupervisorExecutionMode.LIVE
                ),
                None,
            )
            if index is None:
                return None
            assignment = supervisor.assignments[index]
            if (
                assignment.run_id == expected_run_id
                and assignment.state is SupervisorAssignmentState.INTERRUPTED
            ):
                supervisor.assignments[index] = self._runtime.transition(
                    assignment,
                    state=SupervisorAssignmentState.REDIRECTED,
                    decision_snapshot=record.graph_version,
                )
                supervisor.state = SupervisorLifecycleState.REDIRECTING
                self._repository.save(record)
            current = supervisor.assignments[index]
            return _snapshot(record, locator, current)


class ClaudeCodeSessionEnforcement:
    """Deterministic lifecycle and ``PreToolUse`` checks for one agent service."""

    def __init__(
        self,
        *,
        registry: ClaudeCodeSessionRegistry,
        assignments: SupervisorAssignmentGateway,
    ) -> None:
        self._registry = registry
        self._assignments = assignments

    def start(
        self,
        request: ClaudeSessionStartRequest,
    ) -> ClaudeSessionStartResponse:
        binding = self._registry.register(
            session_id=request.session_id,
            cwd=request.cwd,
            branch=request.branch,
            candidates=self._assignments.list_live_claude_assignments(),
        )
        return ClaudeSessionStartResponse(binding=binding)

    def end(
        self,
        request: ClaudeSessionEndRequest,
    ) -> ClaudeSessionEndResponse:
        return ClaudeSessionEndResponse(
            released=self._registry.release(request.session_id)
        )

    def sessions(self) -> tuple[ClaudeCodeSessionBinding, ...]:
        """Every registered session, unbound ones included.

        Unbound sessions are listed on purpose: such a session is allowed
        everything, and hiding it would turn a visible gap into a silent one.
        """

        return self._registry.list()

    def registered_sessions(self) -> tuple[RegisteredSession, ...]:
        """Every registered session with the assignment state behind it.

        Reads only. Nothing here transitions an assignment or delivers a
        redirect — that is ``check``'s job, and listing sessions must never
        consume the deny that the demo depends on.
        """

        return tuple(
            RegisteredSession(
                binding=binding,
                assignment_state=self._assignment_state(binding),
            )
            for binding in self._registry.list()
        )

    def _assignment_state(
        self,
        binding: ClaudeCodeSessionBinding,
    ) -> SessionAssignmentState:
        if binding.assignment is None:
            return SessionAssignmentState.unbound()
        snapshot = self._assignments.get(binding.assignment)
        if snapshot is None:
            return SessionAssignmentState.missing()
        return SessionAssignmentState.from_snapshot(snapshot)

    def check(
        self,
        request: ClaudePreToolUseRequest,
    ) -> ClaudeHookVerdict:
        binding = self._registry.get(request.session_id)
        if binding is None:
            return ClaudeHookVerdict(
                decision=HookPermissionDecision.DENY,
                reason=(
                    "Dragback has no registered lifecycle record for this "
                    "Claude Code session."
                ),
                denial_mode=HookDenialMode.UNTIL_REGISTERED,
            )
        if binding.source is SessionBindingSource.UNRESOLVED_ATTACHMENT:
            # This session asked to be supervised by a named assignment and that
            # assignment does not exist. Allowing it would make one junk
            # `.dragback/attach` an off switch for enforcement, so it denies
            # until the marker is corrected or removed. Distinct from unbound,
            # which is genuinely "no binding information".
            return ClaudeHookVerdict(
                decision=HookPermissionDecision.DENY,
                reason=(
                    "Dragback cannot resolve this session's explicit attachment: "
                    + _bounded(binding.detail, 200)
                ),
                denial_mode=HookDenialMode.UNTIL_REGISTERED,
                binding_source=binding.source,
            )
        if binding.assignment is None:
            return ClaudeHookVerdict(
                decision=HookPermissionDecision.ALLOW,
                reason=(
                    "Dragback session is registered but visibly unbound; "
                    "no assignment enforcement was inferred."
                ),
                binding_source=binding.source,
            )

        snapshot = self._assignments.get(binding.assignment)
        if snapshot is None:
            return _deny_for_missing_assignment(binding)
        assignment = snapshot.assignment
        interrupt_key = _interrupt_key(snapshot)
        acknowledged = self._registry.is_acknowledged(
            session_id=request.session_id,
            interrupt_key=interrupt_key,
        )

        if assignment.state is SupervisorAssignmentState.INTERRUPTED:
            instruction = _clean_optional(assignment.redirect_instruction)
            if instruction is not None:
                # DENY UNTIL ACKNOWLEDGED, not deny-once.
                #
                # This used to advance the assignment to REDIRECTED *before* the
                # denial had reached anyone. If the response or the hook's
                # stdout was lost, the assignment had already moved on: the next
                # check found a current snapshot and ALLOWED, so the redirect was
                # never delivered and the agent carried on doing invalidated
                # work. A fail-open in the one path the product is named for.
                #
                # Advancement now happens only on confirmed receipt. The deny
                # carries `redirect_id`; the hook records it once the verdict is
                # actually on stdout and echoes it on its next check. Until that
                # arrives, the identical redirect is re-delivered — idempotent,
                # still terminating, and it fails closed.
                if request.acknowledged_redirect_id != interrupt_key:
                    return _deny_assignment(
                        binding=binding,
                        snapshot=snapshot,
                        mode=HookDenialMode.UNTIL_ACKNOWLEDGED,
                        redirect_instruction=instruction,
                        redirect_id=interrupt_key,
                    )
                # Confirmed delivered. Advance now, then fall through and answer
                # on the assignment's NEW state rather than guessing at it.
                delivered = self._assignments.mark_redirect_delivered(
                    snapshot.locator,
                    expected_run_id=assignment.run_id,
                )
                if delivered is None:
                    return _deny_for_missing_assignment(binding)
                snapshot = delivered
                assignment = delivered.assignment
            elif acknowledged:
                return _allow_acknowledged(binding, snapshot)
            else:
                return _deny_assignment(
                    binding=binding,
                    snapshot=snapshot,
                    mode=HookDenialMode.UNTIL_ACKNOWLEDGED,
                    redirect_instruction=None,
                )

        if assignment.state is SupervisorAssignmentState.CONTINUING:
            return ClaudeHookVerdict(
                decision=HookPermissionDecision.ALLOW,
                reason=(
                    "Dragback preserved this assignment because its task scopes "
                    "do not intersect the approved change."
                ),
                workspace_id=snapshot.locator.workspace_id,
                assignment_id=assignment.id,
                task_id=assignment.task_id,
                binding_source=binding.source,
            )

        if (
            assignment.decision_snapshot
            != snapshot.current_decision_snapshot
        ):
            if acknowledged:
                return _allow_acknowledged(binding, snapshot)
            return _deny_assignment(
                binding=binding,
                snapshot=snapshot,
                mode=HookDenialMode.UNTIL_ACKNOWLEDGED,
                redirect_instruction=None,
            )

        return ClaudeHookVerdict(
            decision=HookPermissionDecision.ALLOW,
            reason="Dragback assignment is bound to the current decision snapshot.",
            workspace_id=snapshot.locator.workspace_id,
            assignment_id=assignment.id,
            task_id=assignment.task_id,
            binding_source=binding.source,
        )

    def acknowledge(
        self,
        *,
        session_id: str,
    ) -> ClaudeSessionAcknowledgement:
        binding = self._registry.get(session_id)
        if binding is None or binding.assignment is None:
            raise KeyError(session_id)
        snapshot = self._assignments.get(binding.assignment)
        if snapshot is None:
            raise KeyError(session_id)
        interrupt_key = _interrupt_key(snapshot)
        self._registry.acknowledge(
            session_id=session_id,
            interrupt_key=interrupt_key,
        )
        return ClaudeSessionAcknowledgement(
            acknowledged=True,
            interrupt_key=interrupt_key,
        )


def _snapshot(
    record: LiveWorkspaceRecord,
    locator: AssignmentLocator,
    assignment: SupervisorAssignment,
) -> AssignmentEnforcementSnapshot:
    graph_version = record.graph_version
    report = record.invalidation_report
    evidence_refs = report.evidence_refs if report else ()
    evidence_ref = next(iter(evidence_refs), None)
    return AssignmentEnforcementSnapshot(
        locator=locator,
        assignment=assignment.model_copy(deep=True),
        current_decision_snapshot=graph_version,
        evidence_ref=(
            _bounded(evidence_ref, 1000)
            if evidence_ref is not None
            else None
        ),
    )


def _interrupt_key(snapshot: AssignmentEnforcementSnapshot) -> str:
    assignment = snapshot.assignment
    return stable_hash(
        {
            "workspace_id": snapshot.locator.workspace_id,
            "assignment_id": assignment.id,
            "run_id": assignment.run_id,
            "current_decision_snapshot": snapshot.current_decision_snapshot,
            "interrupt_reason": assignment.interrupt_reason,
            "redirect_instruction": assignment.redirect_instruction,
            "provenance_path": assignment.provenance_path,
        }
    )


def _deny_for_missing_assignment(
    binding: ClaudeCodeSessionBinding,
) -> ClaudeHookVerdict:
    locator = binding.assignment
    assert locator is not None
    return ClaudeHookVerdict(
        decision=HookPermissionDecision.DENY,
        reason=(
            "Dragback session binding is stale because its assignment is no "
            "longer present."
        ),
        workspace_id=locator.workspace_id,
        assignment_id=locator.assignment_id,
        task_id=locator.task_id,
        binding_source=binding.source,
        denial_mode=HookDenialMode.UNTIL_ACKNOWLEDGED,
    )


def _deny_assignment(
    *,
    binding: ClaudeCodeSessionBinding,
    snapshot: AssignmentEnforcementSnapshot,
    mode: HookDenialMode,
    redirect_instruction: str | None,
    redirect_id: str | None = None,
) -> ClaudeHookVerdict:
    assignment = snapshot.assignment
    return ClaudeHookVerdict(
        decision=HookPermissionDecision.DENY,
        reason=(
            _bounded(
                assignment.interrupt_reason
                or (
                    "The assignment is not bound to the current approved "
                    "decision snapshot."
                ),
                1000,
            )
        ),
        workspace_id=snapshot.locator.workspace_id,
        assignment_id=assignment.id,
        task_id=assignment.task_id,
        binding_source=binding.source,
        denial_mode=mode,
        redirect_instruction=(
            _bounded(redirect_instruction, 8000)
            if redirect_instruction is not None
            else None
        ),
        redirect_id=redirect_id,
        provenance_path=tuple(
            _bounded(item, 255)
            for item in assignment.provenance_path[:100]
        ),
        evidence_ref=(
            _bounded(snapshot.evidence_ref, 1000)
            if snapshot.evidence_ref is not None
            else (
                "dragback://workspaces/"
                f"{snapshot.locator.workspace_id}/assignments/{assignment.id}"
            )
        ),
    )


def _allow_acknowledged(
    binding: ClaudeCodeSessionBinding,
    snapshot: AssignmentEnforcementSnapshot,
) -> ClaudeHookVerdict:
    assignment = snapshot.assignment
    return ClaudeHookVerdict(
        decision=HookPermissionDecision.ALLOW,
        reason=(
            "A human acknowledged this persistent session interrupt; the PR "
            "verification gate remains the compliance backstop."
        ),
        workspace_id=snapshot.locator.workspace_id,
        assignment_id=assignment.id,
        task_id=assignment.task_id,
        binding_source=binding.source,
    )


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.replace("\x00", "").split())
    return normalized or None


def _bounded(value: str, limit: int) -> str:
    normalized = " ".join(value.replace("\x00", "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."
