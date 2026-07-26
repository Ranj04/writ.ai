"""Deterministic Callwright escalation for unacknowledged interrupts.

The scanner in this module never calls Callwright directly.  It submits a
deterministically rendered plan and its snapshot-bound signed grant through the
existing workspace executor boundary.  The executor remains responsible for
fresh grant verification, target allowlisting, the live-call feature gate, and
the Callwright attempt store's at-most-once submission behavior.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from threading import RLock
from typing import Any, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    model_validator,
)
from writai.domain import (
    AgentPlan,
    PlanAction,
    VerificationCode,
    utc_now,
)
from writai.hashing import stable_hash
from writai.integrations.callwright import (
    CALLWRIGHT_PROVIDER,
    CallReceipt,
)
from writai.workspaces.models import WorkspaceExecutionResult
from writai.workspaces.supervisor import SupervisorAssignmentState

_MAX_EXPLANATION_FRAGMENT = 900
_ACTIVE_INTERRUPT_STATES = {SupervisorAssignmentState.INTERRUPTED}


class InterruptEscalationError(RuntimeError):
    """The scanner cannot safely determine or submit one escalation."""


class InterruptEscalationConflict(InterruptEscalationError):
    """One interrupt key resolved to conflicting immutable candidates."""


class InterruptEscalationDisposition(StrEnum):
    """A deterministic scanner outcome; none of these is an authority verdict."""

    INACTIVE = "inactive"
    OUT_OF_SCOPE = "out-of-scope"
    ACKNOWLEDGED = "acknowledged"
    WAITING = "waiting"
    DISABLED = "disabled"
    BLOCKED = "blocked"
    SIMULATED = "simulated"
    SUBMITTED = "submitted"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class InterruptEscalationIntent(_FrozenModel):
    """Immutable evidence and routing context for one interrupted session."""

    workspace_id: str = Field(min_length=1, max_length=255)
    authority_context_id: str = Field(min_length=1, max_length=255)
    session_id: str = Field(min_length=1, max_length=255)
    interrupt_key: str = Field(min_length=1, max_length=255)
    assignment_id: str = Field(min_length=1, max_length=255)
    assignment_state: SupervisorAssignmentState
    run_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    decision_snapshot: str = Field(min_length=1, max_length=255)
    origin_decision_id: str = Field(min_length=1, max_length=255)
    origin_event_id: str = Field(min_length=1, max_length=255)
    interrupted_at: datetime
    assignment_scopes: frozenset[str] = Field(min_length=1, max_length=100)
    affected_scopes: frozenset[str] = Field(min_length=1, max_length=100)
    interrupt_reason: str = Field(min_length=1, max_length=2_000)
    provenance_path: tuple[str, ...] = Field(min_length=1, max_length=100)
    invalidated_artifact_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=100,
    )
    preserved_artifact_ids: tuple[str, ...] = Field(
        default=(),
        max_length=100,
    )
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    requirements_by_scope: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        max_length=100,
    )
    phone_number_ref: str = Field(min_length=1, max_length=128)
    language: str = Field(
        default="en",
        min_length=2,
        max_length=35,
        pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*$",
    )

    @model_validator(mode="after")
    def validate_explanation(self) -> InterruptEscalationIntent:
        if self.interrupted_at.utcoffset() is None:
            raise ValueError("interrupted_at must be timezone-aware")
        if any(not scope.strip() for scope in self.assignment_scopes):
            raise ValueError("assignment scopes must not be blank")
        if any(not scope.strip() for scope in self.affected_scopes):
            raise ValueError("affected scopes must not be blank")
        if any(not scope.strip() for scope in self.requirements_by_scope):
            raise ValueError("requirement scopes must not be blank")
        _require_unique_nonblank(
            self.provenance_path,
            label="provenance path",
        )
        _require_unique_nonblank(
            self.invalidated_artifact_ids,
            label="invalidated artifact IDs",
        )
        _require_unique_nonblank(
            self.preserved_artifact_ids,
            label="preserved artifact IDs",
        )
        _require_unique_nonblank(
            self.evidence_refs,
            label="evidence references",
        )
        return self

    @property
    def impacted_scopes(self) -> frozenset[str]:
        return self.assignment_scopes & self.affected_scopes

    @property
    def escalation_key(self) -> str:
        """Identity of one originating interrupt, stable across later snapshots."""

        return stable_hash(
            {
                "workspace_id": self.workspace_id,
                "authority_context_id": self.authority_context_id,
                "assignment_id": self.assignment_id,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "origin_decision_id": self.origin_decision_id,
                "origin_event_id": self.origin_event_id,
                "interrupted_at": self.interrupted_at,
            }
        )


class AuthorizedInterruptEscalation(_FrozenModel):
    """One stored escalation intent plus the opaque grant issued for its plan."""

    intent: InterruptEscalationIntent
    grant_token: SecretStr

    @model_validator(mode="after")
    def validate_grant(self) -> AuthorizedInterruptEscalation:
        if not self.grant_token.get_secret_value().strip():
            raise ValueError("grant_token must not be blank")
        return self


class InterruptEscalationResult(_FrozenModel):
    """Explainable scanner result safe to expose without the signed grant."""

    escalation_key: str = Field(min_length=1, max_length=255)
    disposition: InterruptEscalationDisposition
    workspace_id: str
    session_id: str
    assignment_id: str
    run_id: str
    task_id: str
    decision_snapshot: str
    origin_decision_id: str
    origin_event_id: str
    checked_at: datetime
    due_at: datetime
    impacted_scopes: tuple[str, ...] = ()
    reason: str = Field(min_length=1, max_length=2_000)
    provenance_path: tuple[str, ...]
    invalidated_artifact_ids: tuple[str, ...]
    preserved_artifact_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    verification_code: VerificationCode | None = None
    execution_mode: str | None = None
    call_receipt: CallReceipt | None = None


class InterruptEscalationSource(Protocol):
    """Current scheduler input; implementations must return fresh assignment state."""

    def list_candidates(
        self,
        *,
        now: datetime,
    ) -> tuple[AuthorizedInterruptEscalation, ...]: ...


class InterruptAcknowledgementReader(Protocol):
    """Structural match for ``ClaudeCodeSessionRegistry``."""

    def is_acknowledged(
        self,
        *,
        session_id: str,
        interrupt_key: str,
    ) -> bool: ...


class GrantGatedExecutorPort(Protocol):
    """Structural match for ``LiveWorkspaceTransport.execute``."""

    def execute(
        self,
        *,
        context_id: str,
        token: str,
        run_id: str,
        task_id: str,
        plan: AgentPlan,
    ) -> WorkspaceExecutionResult: ...


class InterruptEscalationScanner:
    """Scan fresh interrupt state and submit each due call through the executor."""

    def __init__(
        self,
        *,
        source: InterruptEscalationSource,
        acknowledgements: InterruptAcknowledgementReader,
        executor: GrantGatedExecutorPort,
        threshold: timedelta,
        callwright_live_calls_enabled: bool,
    ) -> None:
        if threshold <= timedelta(0):
            raise ValueError("interrupt escalation threshold must be positive")
        self._source = source
        self._acknowledgements = acknowledgements
        self._executor = executor
        self._threshold = threshold
        self._callwright_live_calls_enabled = callwright_live_calls_enabled
        self._scan_lock = RLock()

    def scan(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[InterruptEscalationResult, ...]:
        checked_at = now or utc_now()
        _require_aware(checked_at, label="now")
        with self._scan_lock:
            candidates = _unique_candidates(
                self._source.list_candidates(now=checked_at)
            )
            return tuple(
                self.escalate_due(candidate, now=checked_at)
                for candidate in candidates
            )

    def escalate_due(
        self,
        candidate: AuthorizedInterruptEscalation,
        *,
        now: datetime | None = None,
    ) -> InterruptEscalationResult:
        return escalate_due(
            candidate=candidate,
            acknowledgements=self._acknowledgements,
            executor=self._executor,
            threshold=self._threshold,
            callwright_live_calls_enabled=self._callwright_live_calls_enabled,
            now=now or utc_now(),
        )


def build_interrupt_escalation_plan(
    intent: InterruptEscalationIntent,
) -> AgentPlan:
    """Render the exact hashed Callwright plan an authority may grant.

    No model chooses the call's structure or explanation.  The plan is a pure
    function of the stored interrupt, its scope intersection, and evidence.
    """

    impacted_scopes = tuple(sorted(intent.impacted_scopes))
    if not impacted_scopes:
        raise InterruptEscalationError(
            "An out-of-scope interrupt cannot produce an escalation plan."
        )
    if set(intent.requirements_by_scope) != set(impacted_scopes):
        raise InterruptEscalationError(
            "The escalation plan requires one current requirement set for "
            "every impacted scope."
        )
    suffix = intent.escalation_key[:16].upper()
    scope_text = ", ".join(impacted_scopes)
    objective = _bounded(
        (
            "Notify the authorized lead that writ.ai assignment "
            f"{intent.assignment_id} remains unacknowledged after an interrupt "
            f"affecting {scope_text} at {intent.decision_snapshot}."
        ),
        2_000,
    )
    instructions = [
        _bounded(
            f"Interrupt reason: {intent.interrupt_reason}",
            _MAX_EXPLANATION_FRAGMENT,
        ),
        _bounded(
            "Provenance path: " + " -> ".join(intent.provenance_path),
            _MAX_EXPLANATION_FRAGMENT,
        ),
        _bounded(
            "Invalidated artifacts: "
            + ", ".join(intent.invalidated_artifact_ids),
            _MAX_EXPLANATION_FRAGMENT,
        ),
        _bounded(
            "Preserved artifacts: "
            + (
                ", ".join(intent.preserved_artifact_ids)
                if intent.preserved_artifact_ids
                else "(none recorded)"
            ),
            _MAX_EXPLANATION_FRAGMENT,
        ),
        _bounded(
            "Evidence references: " + ", ".join(intent.evidence_refs),
            _MAX_EXPLANATION_FRAGMENT,
        ),
        (
            "Ask the lead to review and acknowledge the interrupt through "
            "writ.ai; do not approve work or make commitments on the call."
        ),
    ]
    requirement_actions = [
        PlanAction(
            id=f"ACTION-INTERRUPT-REQUIREMENT-{suffix}-{index}",
            description=(
                "Bind the escalation grant to the current approved "
                f"requirements for {scope}."
            ),
            scopes={scope},
            attributes=dict(intent.requirements_by_scope[scope]),
        )
        for index, scope in enumerate(impacted_scopes, start=1)
    ]
    return AgentPlan(
        id=f"PLAN-INTERRUPT-ESCALATION-{suffix}",
        ticket_id=intent.task_id,
        objective=objective,
        actions=[
            *requirement_actions,
            PlanAction(
                id=f"ACTION-INTERRUPT-ESCALATION-{suffix}",
                description=(
                    "Place the pre-authorized escalation call for this "
                    "unacknowledged interrupt."
                ),
                # Current scope requirements are proven by the deterministic
                # actions above. Keeping the vendor action unscoped prevents
                # vendor-only fields from masquerading as requirement values.
                scopes=set(),
                attributes={
                    "provider": CALLWRIGHT_PROVIDER,
                    "phone_number_ref": intent.phone_number_ref,
                    "objective": objective,
                    "instructions": instructions,
                    "allowed_commitments": [],
                    "max_deposit_usd": 0,
                    "language": intent.language,
                },
            )
        ],
    )


def escalate_due(
    *,
    candidate: AuthorizedInterruptEscalation,
    acknowledgements: InterruptAcknowledgementReader,
    executor: GrantGatedExecutorPort,
    threshold: timedelta,
    callwright_live_calls_enabled: bool,
    now: datetime,
) -> InterruptEscalationResult:
    """Evaluate and, if due, submit exactly one grant-gated escalation attempt."""

    if threshold <= timedelta(0):
        raise ValueError("interrupt escalation threshold must be positive")
    _require_aware(now, label="now")
    intent = candidate.intent
    due_at = intent.interrupted_at + threshold
    impacted_scopes = tuple(sorted(intent.impacted_scopes))

    if intent.assignment_state not in _ACTIVE_INTERRUPT_STATES:
        return _result(
            intent,
            disposition=InterruptEscalationDisposition.INACTIVE,
            checked_at=now,
            due_at=due_at,
            impacted_scopes=impacted_scopes,
            reason=(
                f"Assignment state {intent.assignment_state.value!r} is not an "
                "unacknowledged interrupted state."
            ),
        )
    if not impacted_scopes:
        return _result(
            intent,
            disposition=InterruptEscalationDisposition.OUT_OF_SCOPE,
            checked_at=now,
            due_at=due_at,
            impacted_scopes=(),
            reason=(
                "The approved change does not intersect this assignment's "
                "scopes; no escalation is permitted."
            ),
        )
    if acknowledgements.is_acknowledged(
        session_id=intent.session_id,
        interrupt_key=intent.interrupt_key,
    ):
        return _result(
            intent,
            disposition=InterruptEscalationDisposition.ACKNOWLEDGED,
            checked_at=now,
            due_at=due_at,
            impacted_scopes=impacted_scopes,
            reason=(
                "The session registry records an acknowledgment for this exact "
                "interrupt; no call was submitted."
            ),
        )
    if now < due_at:
        return _result(
            intent,
            disposition=InterruptEscalationDisposition.WAITING,
            checked_at=now,
            due_at=due_at,
            impacted_scopes=impacted_scopes,
            reason=(
                "The interrupt is unacknowledged, but its configured escalation "
                "threshold has not elapsed."
            ),
        )
    if not callwright_live_calls_enabled:
        return _result(
            intent,
            disposition=InterruptEscalationDisposition.DISABLED,
            checked_at=now,
            due_at=due_at,
            impacted_scopes=impacted_scopes,
            reason=(
                "The interrupt is due, but callwright_live_calls_enabled is false; "
                "no executor request was made."
            ),
        )

    plan = build_interrupt_escalation_plan(intent)

    # Close the ordinary acknowledge-during-scan race immediately before the
    # irreversible executor boundary.  The executor remains the final grant gate.
    if acknowledgements.is_acknowledged(
        session_id=intent.session_id,
        interrupt_key=intent.interrupt_key,
    ):
        return _result(
            intent,
            disposition=InterruptEscalationDisposition.ACKNOWLEDGED,
            checked_at=now,
            due_at=due_at,
            impacted_scopes=impacted_scopes,
            reason=(
                "The exact interrupt was acknowledged before executor "
                "submission; no call was submitted."
            ),
        )

    execution = executor.execute(
        context_id=intent.authority_context_id,
        token=candidate.grant_token.get_secret_value(),
        run_id=intent.run_id,
        task_id=intent.task_id,
        plan=plan,
    )
    if not execution.applied:
        return _result(
            intent,
            disposition=InterruptEscalationDisposition.BLOCKED,
            checked_at=now,
            due_at=due_at,
            impacted_scopes=impacted_scopes,
            reason=f"Grant-gated executor refused the escalation: {execution.reason}",
            verification_code=execution.verification_code,
            execution_mode=execution.execution_mode,
        )
    receipt = execution.call_receipt
    if receipt is None or execution.execution_mode not in {"live", "simulated"}:
        return _result(
            intent,
            disposition=InterruptEscalationDisposition.BLOCKED,
            checked_at=now,
            due_at=due_at,
            impacted_scopes=impacted_scopes,
            reason=(
                "The executor reported an applied operation without a confirmed "
                "Callwright receipt and explicit execution mode."
            ),
            verification_code=execution.verification_code,
            execution_mode=execution.execution_mode,
        )
    if (
        execution.execution_mode == "live"
        and receipt.provider != CALLWRIGHT_PROVIDER
    ):
        return _result(
            intent,
            disposition=InterruptEscalationDisposition.BLOCKED,
            checked_at=now,
            due_at=due_at,
            impacted_scopes=impacted_scopes,
            reason=(
                "The executor labeled a non-Callwright receipt as live; "
                "writ.ai refused to report a live escalation."
            ),
            verification_code=execution.verification_code,
            execution_mode=execution.execution_mode,
            call_receipt=receipt,
        )

    simulated = execution.execution_mode == "simulated"
    return _result(
        intent,
        disposition=(
            InterruptEscalationDisposition.SIMULATED
            if simulated
            else InterruptEscalationDisposition.SUBMITTED
        ),
        checked_at=now,
        due_at=due_at,
        impacted_scopes=impacted_scopes,
        reason=(
            "Snapshot-bound grant verified; simulated Callwright escalation "
            "accepted without a live phone call."
            if simulated
            else (
                "Snapshot-bound grant verified; live Callwright escalation "
                "submitted for the unacknowledged interrupt."
            )
        ),
        verification_code=execution.verification_code,
        execution_mode=execution.execution_mode,
        call_receipt=receipt,
    )


def _result(
    intent: InterruptEscalationIntent,
    *,
    disposition: InterruptEscalationDisposition,
    checked_at: datetime,
    due_at: datetime,
    impacted_scopes: tuple[str, ...],
    reason: str,
    verification_code: VerificationCode | None = None,
    execution_mode: str | None = None,
    call_receipt: CallReceipt | None = None,
) -> InterruptEscalationResult:
    return InterruptEscalationResult(
        escalation_key=intent.escalation_key,
        disposition=disposition,
        workspace_id=intent.workspace_id,
        session_id=intent.session_id,
        assignment_id=intent.assignment_id,
        run_id=intent.run_id,
        task_id=intent.task_id,
        decision_snapshot=intent.decision_snapshot,
        origin_decision_id=intent.origin_decision_id,
        origin_event_id=intent.origin_event_id,
        checked_at=checked_at,
        due_at=due_at,
        impacted_scopes=impacted_scopes,
        reason=reason,
        provenance_path=intent.provenance_path,
        invalidated_artifact_ids=intent.invalidated_artifact_ids,
        preserved_artifact_ids=intent.preserved_artifact_ids,
        evidence_refs=intent.evidence_refs,
        verification_code=verification_code,
        execution_mode=execution_mode,
        call_receipt=call_receipt,
    )


def _unique_candidates(
    candidates: tuple[AuthorizedInterruptEscalation, ...],
) -> tuple[AuthorizedInterruptEscalation, ...]:
    by_key: dict[str, AuthorizedInterruptEscalation] = {}
    for candidate in candidates:
        key = candidate.intent.escalation_key
        existing = by_key.get(key)
        if existing is not None and existing != candidate:
            raise InterruptEscalationConflict(
                "The same interrupt key produced conflicting escalation candidates."
            )
        by_key[key] = candidate
    return tuple(by_key[key] for key in sorted(by_key))


def _require_unique_nonblank(values: tuple[str, ...], *, label: str) -> None:
    if any(not value.strip() for value in values):
        raise ValueError(f"{label} must not contain blank values")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicate values")


def _require_aware(value: datetime, *, label: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _bounded(value: str, limit: int) -> str:
    normalized = " ".join(value.replace("\x00", "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."
