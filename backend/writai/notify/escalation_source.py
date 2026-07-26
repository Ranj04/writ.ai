"""Repository-backed candidate source for interrupt escalation."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Protocol

from writai.domain import (
    AgentPlan,
    AuthorizationRequest,
    AuthorizationResult,
    SignedGrant,
    Verdict,
)
from writai.hashing import stable_hash
from writai.notify.escalate import (
    AuthorizedInterruptEscalation,
    InterruptEscalationConflict,
    InterruptEscalationError,
    InterruptEscalationIntent,
    build_interrupt_escalation_plan,
)
from writai.workspaces.models import LiveWorkspaceRecord, WorkspaceEvent
from writai.workspaces.repository import (
    LiveWorkspaceNotFound,
    LiveWorkspaceRepository,
)
from writai.workspaces.session_binding import ClaudeCodeSessionRegistry
from writai.workspaces.supervisor import (
    SupervisorAssignment,
    SupervisorAssignmentState,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr

_ACTIVE_STATES = {SupervisorAssignmentState.INTERRUPTED}


@dataclass(frozen=True)
class _OriginatingInterrupt:
    decision_id: str
    event_id: str
    interrupted_at: datetime
    affected_scopes: frozenset[str]
    invalidated_task_ids: tuple[str, ...]
    preserved_task_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class StoredInterruptEscalationGrant(_FrozenModel):
    """Durable binding that prevents grant rotation from causing a second dial."""

    escalation_key: str = Field(min_length=1, max_length=255)
    plan_hash: str = Field(min_length=1, max_length=255)
    decision_snapshot: str = Field(min_length=1, max_length=255)
    run_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    grant: SignedGrant


class InterruptEscalationGrantStore(Protocol):
    def get_or_create(
        self,
        escalation_key: str,
        factory: Callable[[], StoredInterruptEscalationGrant],
    ) -> StoredInterruptEscalationGrant: ...


class InMemoryInterruptEscalationGrantStore:
    def __init__(self) -> None:
        self._records: dict[str, StoredInterruptEscalationGrant] = {}
        self._lock = RLock()

    def get_or_create(
        self,
        escalation_key: str,
        factory: Callable[[], StoredInterruptEscalationGrant],
    ) -> StoredInterruptEscalationGrant:
        with self._lock:
            record = self._records.get(escalation_key)
            if record is None:
                record = factory()
            if record.escalation_key != escalation_key:
                raise InterruptEscalationConflict(
                    "The interrupt escalation grant factory returned another key."
                )
            self._records[record.escalation_key] = record.model_copy(deep=True)
            return record.model_copy(deep=True)


class SqliteInterruptEscalationGrantStore:
    """Atomic cross-worker grant winner for each originating interrupt."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._lock = RLock()
        self._initialize()

    def get_or_create(
        self,
        escalation_key: str,
        factory: Callable[[], StoredInterruptEscalationGrant],
    ) -> StoredInterruptEscalationGrant:
        with self._lock:
            connection = self._connect()
            try:
                # The authority call deliberately happens while this write lock
                # is held. Another worker cannot mint a second authorization for
                # the same key; it waits, then consumes the committed winner.
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT record_json
                    FROM interrupt_escalation_grants
                    WHERE escalation_key = ?
                    """,
                    (escalation_key,),
                ).fetchone()
                if row is not None:
                    record = StoredInterruptEscalationGrant.model_validate_json(
                        row[0]
                    )
                    connection.commit()
                    return record
                record = factory()
                if record.escalation_key != escalation_key:
                    raise InterruptEscalationConflict(
                        "The interrupt escalation grant factory returned "
                        "another key."
                    )
                connection.execute(
                    """
                    INSERT INTO interrupt_escalation_grants (
                        escalation_key,
                        record_json
                    ) VALUES (?, ?)
                    """,
                    (escalation_key, record.model_dump_json()),
                )
                connection.commit()
                return record.model_copy(deep=True)
            except sqlite3.Error as exc:
                connection.rollback()
                raise InterruptEscalationError(
                    "The interrupt escalation grant store is unavailable."
                ) from exc
            except (ValueError, TypeError) as exc:
                connection.rollback()
                raise InterruptEscalationError(
                    "The interrupt escalation grant store contains invalid data."
                ) from exc
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
                self._secure_permissions()

    def _initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS interrupt_escalation_grants (
                    escalation_key TEXT PRIMARY KEY,
                    record_json TEXT NOT NULL
                )
                """
            )
            connection.commit()
        except sqlite3.Error as exc:
            raise InterruptEscalationError(
                "The interrupt escalation grant store could not be initialized."
            ) from exc
        finally:
            connection.close()
            self._secure_permissions()

    def _connect(self) -> sqlite3.Connection:
        try:
            return sqlite3.connect(self._path, timeout=30.0)
        except sqlite3.Error as exc:
            raise InterruptEscalationError(
                "The interrupt escalation grant store is unavailable."
            ) from exc

    def _secure_permissions(self) -> None:
        try:
            self._path.chmod(0o600)
        except OSError as exc:
            if self._path.exists():
                raise InterruptEscalationError(
                    "The interrupt escalation grant store permissions could "
                    "not be secured."
                ) from exc


class InterruptEscalationAuthorityPort(Protocol):
    """Subset of ``LiveWorkspaceTransport`` needed to issue the exact grant."""

    def authorize(
        self,
        context_id: str,
        request: AuthorizationRequest,
    ) -> AuthorizationResult: ...


class RepositoryInterruptEscalationSource:
    """Derive due candidates from persisted assignments and live bindings.

    The source authorizes only candidates whose threshold has elapsed. A grant
    is persisted once and reused forever for the same interrupt key, so a
    service restart cannot rotate the Callwright idempotency key.
    """

    def __init__(
        self,
        *,
        repository: LiveWorkspaceRepository,
        sessions: ClaudeCodeSessionRegistry,
        authority: InterruptEscalationAuthorityPort,
        grants: InterruptEscalationGrantStore,
        threshold: timedelta,
        phone_number_ref: str,
        language: str = "en",
    ) -> None:
        if threshold <= timedelta(0):
            raise ValueError("interrupt escalation threshold must be positive")
        if not phone_number_ref.strip():
            raise ValueError("interrupt escalation phone reference is required")
        self._repository = repository
        self._sessions = sessions
        self._authority = authority
        self._grants = grants
        self._threshold = threshold
        self._phone_number_ref = phone_number_ref.strip()
        self._language = language.strip()
        self._lock = RLock()

    def list_candidates(
        self,
        *,
        now: datetime,
    ) -> tuple[AuthorizedInterruptEscalation, ...]:
        if now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        candidates: list[AuthorizedInterruptEscalation] = []
        for binding in self._sessions.list():
            locator = binding.assignment
            if locator is None:
                continue
            try:
                record = self._repository.get(locator.workspace_id)
            except LiveWorkspaceNotFound:
                continue
            assignment = _current_assignment(record, locator.assignment_id)
            if (
                assignment is None
                or assignment.task_id != locator.task_id
                or assignment.state not in _ACTIVE_STATES
            ):
                continue
            provenance_path = _required_values(
                assignment.provenance_path,
                label="provenance path",
                assignment_id=assignment.id,
            )
            origin = _originating_interrupt(
                record,
                assignment,
                decision_id=provenance_path[0],
            )
            impacted_scopes = assignment.scopes & origin.affected_scopes
            if not impacted_scopes:
                continue
            interrupt_key = _interrupt_key(record, assignment)
            if self._sessions.is_acknowledged(
                session_id=binding.session_id,
                interrupt_key=interrupt_key,
            ):
                continue
            if now < origin.interrupted_at + self._threshold:
                continue
            requirements = _current_requirements(
                record,
                impacted_scopes=impacted_scopes,
            )
            intent = InterruptEscalationIntent(
                workspace_id=record.definition.id,
                authority_context_id=record.context_id,
                session_id=binding.session_id,
                interrupt_key=interrupt_key,
                assignment_id=assignment.id,
                assignment_state=assignment.state,
                run_id=assignment.run_id,
                task_id=assignment.task_id,
                decision_snapshot=record.graph_version,
                origin_decision_id=origin.decision_id,
                origin_event_id=origin.event_id,
                interrupted_at=origin.interrupted_at,
                assignment_scopes=frozenset(assignment.scopes),
                affected_scopes=origin.affected_scopes,
                interrupt_reason=_required_text(
                    assignment.interrupt_reason,
                    label="interrupt reason",
                    assignment_id=assignment.id,
                ),
                provenance_path=provenance_path,
                invalidated_artifact_ids=origin.invalidated_task_ids,
                preserved_artifact_ids=origin.preserved_task_ids,
                evidence_refs=origin.evidence_refs,
                requirements_by_scope=requirements,
                phone_number_ref=self._phone_number_ref,
                language=self._language,
            )
            candidates.append(
                AuthorizedInterruptEscalation(
                    intent=intent,
                    grant_token=SecretStr(
                        self._grant_for(intent).grant.token
                    ),
                )
            )
        return tuple(
            sorted(
                candidates,
                key=lambda item: item.intent.escalation_key,
            )
        )

    def _grant_for(
        self,
        intent: InterruptEscalationIntent,
    ) -> StoredInterruptEscalationGrant:
        with self._lock:
            return self._grant_for_locked(intent)

    def _grant_for_locked(
        self,
        intent: InterruptEscalationIntent,
    ) -> StoredInterruptEscalationGrant:
        plan = build_interrupt_escalation_plan(intent)
        plan_hash = stable_hash(plan)
        existing = self._grants.get_or_create(
            intent.escalation_key,
            lambda: self._authorize_grant(
                intent,
                plan_hash=plan_hash,
                plan=plan,
            ),
        )
        expected = (
            existing.plan_hash == plan_hash
            and existing.decision_snapshot == intent.decision_snapshot
            and existing.run_id == intent.run_id
            and existing.task_id == intent.task_id
            and existing.grant.payload.plan_hash == plan_hash
            and existing.grant.payload.decision_snapshot
            == intent.decision_snapshot
            and existing.grant.payload.run_id == intent.run_id
            and existing.grant.payload.task_id == intent.task_id
            and existing.grant.payload.verdict is Verdict.ALLOW
        )
        if not expected:
            raise InterruptEscalationConflict(
                "The stored interrupt escalation grant does not match "
                "the current immutable candidate."
            )
        return existing

    def _authorize_grant(
        self,
        intent: InterruptEscalationIntent,
        *,
        plan_hash: str,
        plan: AgentPlan,
    ) -> StoredInterruptEscalationGrant:
        authorization = self._authority.authorize(
            intent.authority_context_id,
            AuthorizationRequest(
                run_id=intent.run_id,
                task_id=intent.task_id,
                plan=plan,
            ),
        )
        grant = authorization.grant
        if (
            authorization.verdict is not Verdict.ALLOW
            or grant is None
            or authorization.graph_version != intent.decision_snapshot
            or authorization.task_id != intent.task_id
            or grant.payload.run_id != intent.run_id
            or grant.payload.task_id != intent.task_id
            or grant.payload.decision_snapshot != intent.decision_snapshot
            or grant.payload.plan_hash != plan_hash
            or grant.payload.verdict is not Verdict.ALLOW
        ):
            raise InterruptEscalationError(
                "Intent authority did not issue a matching ALLOW grant for "
                f"interrupt escalation {intent.assignment_id}."
            )
        stored = StoredInterruptEscalationGrant(
            escalation_key=intent.escalation_key,
            plan_hash=plan_hash,
            decision_snapshot=intent.decision_snapshot,
            run_id=intent.run_id,
            task_id=intent.task_id,
            grant=grant,
        )
        return stored


def _current_assignment(
    record: LiveWorkspaceRecord,
    assignment_id: str,
) -> SupervisorAssignment | None:
    supervisor = record.supervisor
    if supervisor is None:
        return None
    return next(
        (
            assignment
            for assignment in supervisor.assignments
            if assignment.id == assignment_id
        ),
        None,
    )


def _interrupt_key(
    record: LiveWorkspaceRecord,
    assignment: SupervisorAssignment,
) -> str:
    """Mirror the deterministic hook key without reading private registry state."""

    return stable_hash(
        {
            "workspace_id": record.definition.id,
            "assignment_id": assignment.id,
            "run_id": assignment.run_id,
            "current_decision_snapshot": record.graph_version,
            "interrupt_reason": assignment.interrupt_reason,
            "redirect_instruction": assignment.redirect_instruction,
            "provenance_path": assignment.provenance_path,
        }
    )


def _originating_interrupt(
    record: LiveWorkspaceRecord,
    assignment: SupervisorAssignment,
    *,
    decision_id: str,
) -> _OriginatingInterrupt:
    approved_mutations = [
        item
        for item in record.approved_mutations
        if item.mutation.decision.id == decision_id
    ]
    if len(approved_mutations) != 1:
        raise InterruptEscalationError(
            "The interrupted assignment does not resolve to exactly one "
            f"approved mutation for decision {decision_id}."
        )
    approved_mutation = approved_mutations[0]
    affected_scopes = frozenset(
        approved_mutation.mutation.affected_scopes
    )
    if not affected_scopes:
        raise InterruptEscalationError(
            f"Originating decision {decision_id} has no affected scopes."
        )

    approval_events: list[
        tuple[WorkspaceEvent, tuple[str, ...], tuple[str, ...]]
    ] = []
    for event in record.history:
        if (
            event.event_type != "decision.approved"
            or event.data.get("decision_id") != decision_id
        ):
            continue
        invalidated = event.data.get("invalidated_task_ids")
        preserved = event.data.get("preserved_task_ids")
        if not isinstance(invalidated, list) or not all(
            isinstance(item, str) for item in invalidated
        ) or not isinstance(preserved, list) or not all(
            isinstance(item, str) for item in preserved
        ):
            raise InterruptEscalationError(
                "The durable decision approval event is missing its task "
                f"partition in workspace {record.definition.id}."
            )
        invalidated_ids = _required_values(
            invalidated,
            label="invalidated task IDs",
            assignment_id=assignment.id,
        )
        preserved_ids = _required_values(
            preserved,
            label="preserved task IDs",
            assignment_id=assignment.id,
            allow_empty=True,
        )
        if assignment.task_id not in invalidated_ids:
            continue
        approval_events.append((event, invalidated_ids, preserved_ids))
    if len(approval_events) != 1:
        raise InterruptEscalationError(
            "The interrupted assignment does not resolve to exactly one "
            "decision.approved task partition for decision "
            f"{decision_id} in workspace {record.definition.id}."
        )
    approval_event, invalidated_task_ids, preserved_task_ids = (
        approval_events[0]
    )

    enforced_events: list[WorkspaceEvent] = []
    for event in record.history:
        if (
            event.event_type != "supervisor.interrupt.enforced"
            or event.data.get("decision_id") != decision_id
        ):
            continue
        interrupted = event.data.get("interrupted_assignment_ids")
        if not isinstance(interrupted, list) or not all(
            isinstance(item, str) for item in interrupted
        ):
            raise InterruptEscalationError(
                "The durable supervisor interrupt event is malformed in "
                f"workspace {record.definition.id}."
            )
        if assignment.id not in interrupted:
            continue
        enforced_events.append(event)
    if len(enforced_events) > 1:
        raise InterruptEscalationError(
            "The interrupted assignment resolves to conflicting durable "
            f"supervisor events for decision {decision_id}."
        )
    origin_event = (
        enforced_events[0] if enforced_events else approval_event
    )
    if origin_event.created_at.utcoffset() is None:
        raise InterruptEscalationError(
            "The active interrupt timestamp is not timezone-aware."
        )

    evidence_candidates = [
        approved_mutation.mutation.decision.source_ref,
        (
            approved_mutation.approval_evidence.evidence_ref
            if approved_mutation.approval_evidence is not None
            else None
        ),
        approval_event.data.get("approval_evidence_ref"),
    ]
    evidence_refs = tuple(
        dict.fromkeys(
            value.strip()
            for value in evidence_candidates
            if isinstance(value, str) and value.strip()
        )
    )
    if not evidence_refs:
        raise InterruptEscalationError(
            f"Originating decision {decision_id} has no durable evidence."
        )
    event_id = stable_hash(
        {
            "workspace_id": record.definition.id,
            "event_type": origin_event.event_type,
            "event_sequence": origin_event.sequence,
            "event_created_at": origin_event.created_at,
            "decision_id": decision_id,
            "assignment_id": assignment.id,
            "task_id": assignment.task_id,
        }
    )
    return _OriginatingInterrupt(
        decision_id=decision_id,
        event_id=event_id,
        interrupted_at=origin_event.created_at,
        affected_scopes=affected_scopes,
        invalidated_task_ids=invalidated_task_ids,
        preserved_task_ids=preserved_task_ids,
        evidence_refs=evidence_refs,
    )


def _current_requirements(
    record: LiveWorkspaceRecord,
    *,
    impacted_scopes: set[str],
) -> dict[str, dict[str, object]]:
    authorizations = (
        record.replacement_authorization,
        record.conflict_authorization,
        record.initial_authorization,
    )
    current = next(
        (
            authorization
            for authorization in authorizations
            if authorization is not None
            and authorization.graph_version == record.graph_version
        ),
        None,
    )
    if current is None:
        raise InterruptEscalationError(
            "The active interrupt has no authority result for its current "
            f"snapshot in workspace {record.definition.id}."
        )
    missing = impacted_scopes - set(current.current_requirements)
    if missing:
        raise InterruptEscalationError(
            "The current authority result omits impacted scopes: "
            + ", ".join(sorted(missing))
        )
    return {
        scope: deepcopy(current.current_requirements[scope])
        for scope in sorted(impacted_scopes)
    }


def _required_text(
    value: str | None,
    *,
    label: str,
    assignment_id: str,
) -> str:
    if value is None or not value.strip():
        raise InterruptEscalationError(
            f"Assignment {assignment_id} has no explainable {label}."
        )
    return value.strip()


def _required_values(
    values: list[str],
    *,
    label: str,
    assignment_id: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    normalized = tuple(value.strip() for value in values)
    if not allow_empty and not normalized:
        raise InterruptEscalationError(
            f"Assignment {assignment_id} has no explainable {label}."
        )
    if any(not value for value in normalized):
        raise InterruptEscalationError(
            f"Assignment {assignment_id} has blank {label}."
        )
    if len(normalized) != len(set(normalized)):
        raise InterruptEscalationError(
            f"Assignment {assignment_id} has duplicate {label}."
        )
    return normalized
