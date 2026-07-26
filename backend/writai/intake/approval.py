from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from threading import RLock
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from writai.auth.hexclave import HexclavePermissionError
from writai.domain import DecisionMutation
from writai.hashing import stable_hash
from writai.intake.gate import PermissionChecker
from writai.supervisor_contract import InterruptResult


class ApprovalChannel(StrEnum):
    CLI = "cli"
    WORKSPACE_UI = "workspace-ui"
    SLACK_REACTION = "slack-reaction"
    PUSH = "push"
    EMAIL = "email"


class ApprovalDisposition(StrEnum):
    APPROVED = "APPROVED"
    ALREADY_APPROVED = "ALREADY_APPROVED"
    IGNORED_NOT_AUTHORIZED = "IGNORED_NOT_AUTHORIZED"
    PERMISSION_CHECK_UNAVAILABLE = "PERMISSION_CHECK_UNAVAILABLE"
    STALE_CONFIRMATION = "STALE_CONFIRMATION"
    MISSING_CONFIRMATION = "MISSING_CONFIRMATION"


class ApprovalError(ValueError):
    pass


class ApprovalIdentityError(RuntimeError):
    pass


class PendingApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    workspace_id: str = Field(min_length=1, max_length=128)
    decision_id: str = Field(min_length=1, max_length=160)
    supersedes_id: str = Field(min_length=1, max_length=160)
    affected_scopes: frozenset[str] = Field(min_length=1)
    permission_id: str = Field(min_length=1, max_length=128)
    source_ref: str = Field(min_length=1, max_length=1_000)
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=10_000)
    effective_at: datetime
    requirements: dict[str, dict[str, Any]]
    proposal_fingerprint: str = Field(
        min_length=71,
        max_length=71,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    proposal_instance_id: str = Field(min_length=1, max_length=255)
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_shape(self) -> PendingApproval:
        if set(self.requirements) != set(self.affected_scopes):
            raise ValueError(
                "Pending approval requirements must exactly match affected scopes."
            )
        if self.effective_at.utcoffset() is None:
            raise ValueError("Pending approval effective_at must be timezone-aware.")
        if any(not reference.strip() for reference in self.evidence_refs):
            raise ValueError("Pending approval evidence references must not be blank.")
        return self


class ApprovalEvidence(BaseModel):
    """Durable evidence bound to the exact proposal a human confirmed."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    workspace_id: str = Field(min_length=1, max_length=128)
    decision_id: str = Field(min_length=1, max_length=160)
    approver_user_id: str = Field(min_length=1, max_length=255)
    permission_id: str = Field(min_length=1, max_length=128)
    channel: ApprovalChannel
    evidence_ref: str = Field(min_length=1, max_length=1_000)
    approved_at: datetime
    confirmed_proposal_fingerprint: str = Field(
        min_length=71,
        max_length=71,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    confirmed_proposal_instance_id: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_time(self) -> ApprovalEvidence:
        if self.approved_at.utcoffset() is None:
            raise ValueError("Approval evidence time must be timezone-aware.")
        return self


class ApprovalAttemptRequest(BaseModel):
    """Public approval input; authority role is always derived server-side."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approval_token: SecretStr
    channel: ApprovalChannel
    evidence_ref: str = Field(min_length=1, max_length=1_000)
    confirmed_proposal_fingerprint: str = Field(
        min_length=71,
        max_length=71,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    confirmed_proposal_instance_id: str = Field(min_length=1, max_length=255)


@dataclass(frozen=True)
class ApprovalResult:
    disposition: ApprovalDisposition
    evidence: ApprovalEvidence | None = None
    interrupt_result: InterruptResult = field(
        default_factory=lambda: InterruptResult((), ())
    )
    workspace: Mapping[str, Any] | None = None

    @property
    def approved(self) -> bool:
        return self.disposition in {
            ApprovalDisposition.APPROVED,
            ApprovalDisposition.ALREADY_APPROVED,
        }


class WorkspaceApprovalPort(Protocol):
    def approve_decision(
        self,
        *,
        pending: PendingApproval,
        evidence: ApprovalEvidence,
    ) -> Mapping[str, Any]: ...


class ApprovalIdentityResolver(Protocol):
    """Authenticate an opaque channel credential and return its Hexclave user."""

    def resolve_user_id(self, *, approval_token: str) -> str: ...


class InMemoryApprovalIdentityRegistry:
    """Opaque-token identity registry for app composition and deterministic tests.

    Plain credentials are never retained. Production composition can replace
    this protocol with an authenticated identity provider adapter.
    """

    def __init__(self) -> None:
        self._users_by_token_hash: dict[str, str] = {}
        self._lock = RLock()

    def register(self, *, approval_token: str, user_id: str) -> None:
        token = approval_token.strip()
        normalized_user_id = user_id.strip()
        if not token or not normalized_user_id:
            raise ApprovalIdentityError(
                "Approval token and Hexclave user ID are required."
            )
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            previous = self._users_by_token_hash.get(key)
            if previous is not None and previous != normalized_user_id:
                raise ApprovalIdentityError(
                    "The approval token is already bound to another identity."
                )
            self._users_by_token_hash[key] = normalized_user_id

    def resolve_user_id(self, *, approval_token: str) -> str:
        token = approval_token.strip()
        if not token:
            raise ApprovalIdentityError("Approval authentication is required.")
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            user_id = self._users_by_token_hash.get(key)
        if user_id is None:
            raise ApprovalIdentityError("Approval authentication failed.")
        return user_id


class ApprovalCoordinator:
    """The single identity-checked, proposal-bound path for every channel.

    The workspace port owns the atomic authoritative transition: graph apply,
    persisted evidence, and supervisor interruption.  Evidence is created
    before that call and travels with it, so there is no post-apply in-memory
    ledger write that can fail independently.
    """

    def __init__(
        self,
        *,
        permission_checker: PermissionChecker,
        workspace_port: WorkspaceApprovalPort,
        clock: Callable[[], datetime],
    ) -> None:
        self._permission_checker = permission_checker
        self._workspace_port = workspace_port
        self._clock = clock
        self._lock = RLock()

    def approve(
        self,
        *,
        pending: PendingApproval,
        approver_user_id: str,
        channel: ApprovalChannel,
        evidence_ref: str,
    ) -> ApprovalResult:
        normalized_user_id = approver_user_id.strip()
        if not normalized_user_id:
            raise ApprovalError("An approver user ID is required.")
        if not evidence_ref.strip():
            raise ApprovalError("An approval evidence reference is required.")

        with self._lock:
            try:
                allowed = self._permission_checker.has_permission(
                    user_id=normalized_user_id,
                    permission_id=pending.permission_id,
                )
            except HexclavePermissionError:
                return ApprovalResult(
                    disposition=ApprovalDisposition.PERMISSION_CHECK_UNAVAILABLE
                )
            if not allowed:
                return ApprovalResult(
                    disposition=ApprovalDisposition.IGNORED_NOT_AUTHORIZED
                )

            evidence = ApprovalEvidence(
                workspace_id=pending.workspace_id,
                decision_id=pending.decision_id,
                approver_user_id=normalized_user_id,
                permission_id=pending.permission_id,
                channel=ApprovalChannel(channel),
                evidence_ref=evidence_ref.strip(),
                approved_at=self._clock(),
                confirmed_proposal_fingerprint=pending.proposal_fingerprint,
                confirmed_proposal_instance_id=pending.proposal_instance_id,
            )
            workspace = self._workspace_port.approve_decision(
                pending=pending,
                evidence=evidence,
            )
            return ApprovalResult(
                disposition=ApprovalDisposition.APPROVED,
                evidence=evidence,
                interrupt_result=actual_partition_from_workspace(workspace),
                workspace=workspace,
            )


def pending_from_workspace(workspace: Mapping[str, Any]) -> PendingApproval | None:
    mutation = workspace.get("pending_mutation")
    if not isinstance(mutation, Mapping):
        return None
    try:
        parsed = DecisionMutation.model_validate(mutation)
    except ValueError as exc:
        raise ApprovalError("The pending mutation is malformed.") from exc
    decision = parsed.decision
    requirements = decision.attributes.get("requirements")
    if not isinstance(requirements, Mapping):
        raise ApprovalError("The pending Decision has no requirements.")
    permission_id = decision.authority_role
    source_ref = decision.source_ref
    decision_id = decision.id
    supersedes_id = parsed.supersedes_id
    workspace_id = workspace.get("id")
    proposal_instance_id = workspace.get("pending_proposal_instance_id")
    values = {
        "workspace_id": workspace_id,
        "decision_id": decision_id,
        "supersedes_id": supersedes_id,
        "permission_id": permission_id,
        "source_ref": source_ref,
        "proposal_instance_id": proposal_instance_id,
    }
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise ApprovalError("The pending Decision is missing approval metadata.")
    workspace_id = cast(str, workspace_id)
    permission_id = cast(str, permission_id)
    source_ref = cast(str, source_ref)
    proposal_instance_id = cast(str, proposal_instance_id)
    normalized_requirements: dict[str, dict[str, Any]] = {}
    for scope, requirement in requirements.items():
        if not isinstance(scope, str) or not isinstance(requirement, Mapping):
            raise ApprovalError("Pending Decision requirements are malformed.")
        normalized_requirements[scope] = dict(requirement)
    if decision.effective_at is None:
        raise ApprovalError("The pending Decision has no effective time.")
    evidence_refs = [source_ref]
    extraction = decision.attributes.get("extraction")
    if isinstance(extraction, Mapping):
        spans = extraction.get("validated_evidence_spans")
        if isinstance(spans, list):
            for span in spans:
                if not isinstance(span, Mapping):
                    continue
                reference = span.get("source_ref")
                if (
                    isinstance(reference, str)
                    and reference.strip()
                    and reference not in evidence_refs
                ):
                    evidence_refs.append(reference)
    return PendingApproval(
        workspace_id=workspace_id,
        decision_id=decision_id,
        supersedes_id=supersedes_id,
        permission_id=permission_id,
        source_ref=source_ref,
        proposal_instance_id=proposal_instance_id,
        affected_scopes=frozenset(parsed.affected_scopes),
        title=decision.title,
        text=decision.text or decision.title,
        effective_at=decision.effective_at,
        requirements=normalized_requirements,
        proposal_fingerprint=stable_hash(parsed),
        evidence_refs=tuple(evidence_refs),
    )


def actual_partition_from_workspace(
    workspace: Mapping[str, Any],
) -> InterruptResult:
    supervisor = workspace.get("supervisor")
    if not isinstance(supervisor, Mapping):
        return InterruptResult((), ())
    assignments = supervisor.get("assignments")
    if not isinstance(assignments, list):
        return InterruptResult((), ())
    interrupted: list[str] = []
    preserved: list[str] = []
    for assignment in assignments:
        if not isinstance(assignment, Mapping):
            continue
        assignment_id = assignment.get("id")
        state = assignment.get("state")
        if not isinstance(assignment_id, str) or not assignment_id:
            continue
        if state == "interrupted":
            interrupted.append(assignment_id)
        else:
            preserved.append(assignment_id)
    return InterruptResult(
        tuple(sorted(interrupted)),
        tuple(sorted(preserved)),
    )
