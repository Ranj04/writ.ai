from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock

from pydantic import BaseModel, Field, model_validator

from dragback.authority.engine import IntentAuthority
from dragback.domain import (
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    AuthorizationRequest,
    AuthorizationResult,
    DecisionMutation,
    Edge,
    GrantVerificationRequest,
    GrantVerificationResult,
    InvalidationReport,
    MutationResult,
    Verdict,
)
from dragback.grants import GrantSigner
from dragback.graph.memory import MemoryGraphStore
from dragback.hashing import stable_hash
from dragback.intake.approval import ApprovalEvidence
from dragback.workspaces.models import (
    WorkspaceApprovalRequest,
    WorkspaceSlackBinding,
)

_CONTEXT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$"


class DynamicAuthorityContextCreateRequest(BaseModel):
    context_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=_CONTEXT_ID_PATTERN,
    )
    version: int = Field(ge=0)
    artifacts: list[Artifact] = Field(min_length=1)
    edges: list[Edge] = Field(min_length=1)
    authority_policy: dict[str, set[str]] = Field(min_length=1)
    baseline_decision_id: str = Field(min_length=1, max_length=160)
    slack_binding: WorkspaceSlackBinding | None = None

    @model_validator(mode="after")
    def validate_graph_seed(self) -> DynamicAuthorityContextCreateRequest:
        artifact_ids = [artifact.id for artifact in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("authority context artifact IDs must be unique")
        known_ids = set(artifact_ids)
        if self.baseline_decision_id not in known_ids:
            raise ValueError("baseline_decision_id is absent from artifacts")
        decisions = [
            artifact
            for artifact in self.artifacts
            if artifact.kind is ArtifactKind.DECISION
        ]
        if len(decisions) != 1 or decisions[0].id != self.baseline_decision_id:
            raise ValueError(
                "authority context seed must contain only its baseline Decision"
            )
        if decisions[0].approval_status is not ApprovalStatus.PROPOSAL:
            raise ValueError(
                "authority context baseline Decision must be a proposal"
            )
        for edge in self.edges:
            if edge.source_id not in known_ids or edge.target_id not in known_ids:
                raise ValueError("authority context edges must reference known artifacts")
        return self


class DynamicMutationApprovalRequest(BaseModel):
    mutation: DecisionMutation
    actor_role: str = Field(min_length=1, max_length=128)
    proposal_fingerprint: str = Field(
        min_length=71,
        max_length=71,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    approval_evidence: ApprovalEvidence


class DynamicAuthorityContextState(BaseModel):
    context_id: str
    graph_version: str
    baseline_decision_id: str
    baseline_approved: bool
    artifacts: list[Artifact]
    edges: list[Edge]
    authority_policy: dict[str, set[str]]
    slack_binding: WorkspaceSlackBinding | None = None
    approval_evidence: dict[str, ApprovalEvidence] = Field(default_factory=dict)
    last_report: InvalidationReport | None = None


class DynamicAuthorityContextError(Exception):
    pass


class DynamicAuthorityContextNotFound(DynamicAuthorityContextError):
    pass


class DynamicAuthorityContextConflict(DynamicAuthorityContextError):
    pass


@dataclass
class _DynamicAuthorityContext:
    context_id: str
    baseline_decision_id: str
    graph: MemoryGraphStore
    authority: IntentAuthority
    authority_policy: dict[str, set[str]]
    slack_binding: WorkspaceSlackBinding | None = None
    baseline_approved: bool = False
    baseline_approval_role: str | None = None
    applied_decision_ids: set[str] = field(default_factory=set)
    applied_mutations: dict[str, DynamicMutationApprovalRequest] = field(
        default_factory=dict
    )
    mutation_results: dict[str, MutationResult] = field(default_factory=dict)
    lock: RLock = field(default_factory=RLock, repr=False)

    def state(self) -> DynamicAuthorityContextState:
        report = self.authority.last_report
        return DynamicAuthorityContextState(
            context_id=self.context_id,
            graph_version=self.graph.version_label,
            baseline_decision_id=self.baseline_decision_id,
            baseline_approved=self.baseline_approved,
            artifacts=self.graph.list_artifacts(),
            edges=self.graph.list_edges(),
            authority_policy={
                scope: set(roles) for scope, roles in self.authority_policy.items()
            },
            slack_binding=(
                self.slack_binding.model_copy(deep=True)
                if self.slack_binding is not None
                else None
            ),
            approval_evidence={
                decision_id: request.approval_evidence.model_copy(deep=True)
                for decision_id, request in self.applied_mutations.items()
            },
            last_report=report.model_copy(deep=True) if report else None,
        )


class DynamicAuthorityContextRegistry:
    """Authority-owned runtimes built from explicit, user-supplied graph seeds."""

    def __init__(
        self,
        *,
        grant_secret: str,
        grant_ttl_seconds: int,
        authority_threshold: float,
    ) -> None:
        self._grant_secret = grant_secret
        self._grant_ttl_seconds = grant_ttl_seconds
        self._authority_threshold = authority_threshold
        self._contexts: dict[str, _DynamicAuthorityContext] = {}
        self._lock = RLock()

    def _signing_secret(self, context_id: str) -> str:
        return hmac.new(
            self._grant_secret.encode("utf-8"),
            f"live-workspace:{context_id}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def create(
        self, request: DynamicAuthorityContextCreateRequest
    ) -> DynamicAuthorityContextState:
        with self._lock:
            if request.context_id in self._contexts:
                raise DynamicAuthorityContextConflict(
                    f"Authority context already exists: {request.context_id}"
                )
            graph = MemoryGraphStore()
            graph.reset(
                version=request.version,
                artifacts=request.artifacts,
                edges=request.edges,
            )
            policy = {
                scope: set(roles) for scope, roles in request.authority_policy.items()
            }
            authority = IntentAuthority(
                graph=graph,
                signer=GrantSigner(
                    self._signing_secret(request.context_id),
                    ttl_seconds=self._grant_ttl_seconds,
                ),
                authority_threshold=self._authority_threshold,
                authority_policy=policy,
            )
            context = _DynamicAuthorityContext(
                context_id=request.context_id,
                baseline_decision_id=request.baseline_decision_id,
                graph=graph,
                authority=authority,
                authority_policy=policy,
                slack_binding=(
                    request.slack_binding.model_copy(deep=True)
                    if request.slack_binding is not None
                    else None
                ),
            )
            baseline = graph.get_artifact(request.baseline_decision_id)
            if baseline.kind is not ArtifactKind.DECISION:
                raise DynamicAuthorityContextConflict(
                    "The baseline artifact must be a Decision."
                )
            if baseline.approval_status is not ApprovalStatus.PROPOSAL:
                raise DynamicAuthorityContextConflict(
                    "The baseline Decision must enter the authority as a proposal."
                )
            self._contexts[request.context_id] = context
            return context.state()

    @contextmanager
    def _access(self, context_id: str) -> Iterator[_DynamicAuthorityContext]:
        with self._lock:
            try:
                context = self._contexts[context_id]
            except KeyError as exc:
                raise DynamicAuthorityContextNotFound(
                    f"Unknown Live Workspace authority context: {context_id}"
                ) from exc
            context.lock.acquire()
        try:
            yield context
        finally:
            context.lock.release()

    def state(self, context_id: str) -> DynamicAuthorityContextState:
        with self._access(context_id) as context:
            return context.state()

    def delete(self, context_id: str) -> None:
        with self._lock:
            if context_id not in self._contexts:
                raise DynamicAuthorityContextNotFound(
                    f"Unknown Live Workspace authority context: {context_id}"
                )
            context = self._contexts[context_id]
            with context.lock:
                del self._contexts[context_id]

    @staticmethod
    def _validate_approval(
        context: _DynamicAuthorityContext,
        *,
        decision: Artifact,
        actor_role: str,
        authority_threshold: float,
    ) -> None:
        if decision.kind is not ArtifactKind.DECISION:
            raise DynamicAuthorityContextConflict(
                "Only a Decision artifact may be approved."
            )
        if decision.approval_status is not ApprovalStatus.PROPOSAL:
            raise DynamicAuthorityContextConflict(
                "Only a proposal may enter the approval flow."
            )
        if actor_role != decision.authority_role:
            raise DynamicAuthorityContextConflict(
                "The acting role does not match the Decision authority role."
            )
        if decision.confidence < authority_threshold:
            raise DynamicAuthorityContextConflict(
                "Decision confidence is below the authority threshold."
            )
        requirements = decision.attributes.get("requirements")
        if (
            not isinstance(requirements, dict)
            or set(requirements) != decision.scopes
            or any(not isinstance(value, dict) for value in requirements.values())
        ):
            raise DynamicAuthorityContextConflict(
                "Decision requirements must exactly match its affected scopes."
            )
        disallowed = {
            scope
            for scope in decision.scopes
            if actor_role not in context.authority_policy.get(scope, set())
        }
        if disallowed:
            raise DynamicAuthorityContextConflict(
                f"Role {actor_role!r} is not authoritative for scopes: "
                + ", ".join(sorted(disallowed))
            )

    def approve_baseline(
        self,
        context_id: str,
        request: WorkspaceApprovalRequest,
    ) -> DynamicAuthorityContextState:
        with self._access(context_id) as context:
            if context.baseline_approved:
                if request.actor_role != context.baseline_approval_role:
                    raise DynamicAuthorityContextConflict(
                        "The baseline Decision was approved by a different role."
                    )
                return context.state()
            baseline = context.graph.get_artifact(context.baseline_decision_id)
            self._validate_approval(
                context,
                decision=baseline,
                actor_role=request.actor_role,
                authority_threshold=self._authority_threshold,
            )
            baseline.approval_status = ApprovalStatus.APPROVED
            context.graph.update_artifact(baseline)
            context.baseline_approved = True
            context.baseline_approval_role = request.actor_role
            return context.state()

    def approve_mutation(
        self,
        context_id: str,
        request: DynamicMutationApprovalRequest,
    ) -> MutationResult:
        with self._access(context_id) as context:
            if not context.baseline_approved:
                raise DynamicAuthorityContextConflict(
                    "Approve the baseline Decision before applying changes."
                )
            proposal = request.mutation.decision
            expected_fingerprint = stable_hash(request.mutation)
            evidence = request.approval_evidence
            if request.proposal_fingerprint != expected_fingerprint:
                raise DynamicAuthorityContextConflict(
                    "The confirmed proposal fingerprint does not match the mutation."
                )
            if (
                evidence.workspace_id != context_id.removeprefix("live-")
                or evidence.decision_id != proposal.id
                or evidence.permission_id != request.actor_role
                or evidence.confirmed_proposal_fingerprint
                != expected_fingerprint
            ):
                raise DynamicAuthorityContextConflict(
                    "Approval evidence is not bound to this exact mutation."
                )
            if proposal.id in context.applied_decision_ids:
                previous = context.applied_mutations[proposal.id]
                if previous != request:
                    raise DynamicAuthorityContextConflict(
                        f"Decision ID was already applied with different input: {proposal.id}"
                    )
                return context.mutation_results[proposal.id].model_copy(deep=True)
            try:
                superseded = context.graph.get_artifact(
                    request.mutation.supersedes_id
                )
            except KeyError as exc:
                raise DynamicAuthorityContextConflict(
                    "The proposed supersession target does not exist in the authority graph."
                ) from exc
            if superseded.kind is not ArtifactKind.DECISION:
                raise DynamicAuthorityContextConflict(
                    "The proposed supersession target is not a Decision."
                )
            self._validate_approval(
                context,
                decision=proposal,
                actor_role=request.actor_role,
                authority_threshold=self._authority_threshold,
            )
            approved = request.mutation.model_copy(deep=True)
            approved.decision.approval_status = ApprovalStatus.APPROVED
            extraction = approved.decision.attributes.get("extraction")
            if isinstance(extraction, dict):
                approved.decision.attributes["extraction"] = {
                    **extraction,
                    "human_reviewed": True,
                    "reviewed_at": evidence.approved_at.isoformat(),
                    "reviewed_by": evidence.approver_user_id,
                    "approval_channel": evidence.channel.value,
                    "approval_evidence_ref": evidence.evidence_ref,
                    "confirmed_proposal_fingerprint": expected_fingerprint,
                    "confirmed_proposal_instance_id": (
                        evidence.confirmed_proposal_instance_id
                    ),
                }
            result = context.authority.apply_decision_change(approved)
            if not result.applied:
                raise DynamicAuthorityContextConflict(result.reason)
            context.applied_decision_ids.add(proposal.id)
            context.applied_mutations[proposal.id] = request.model_copy(deep=True)
            context.mutation_results[proposal.id] = result.model_copy(deep=True)
            return result

    def authorize(
        self,
        context_id: str,
        request: AuthorizationRequest,
    ) -> AuthorizationResult:
        with self._access(context_id) as context:
            if not context.baseline_approved:
                return AuthorizationResult(
                    verdict=Verdict.HUMAN_REVIEW,
                    reason="The imported baseline Decision has not been approved.",
                    graph_version=context.graph.version_label,
                    task_id=request.task_id,
                )
            return context.authority.evaluate_plan(
                run_id=request.run_id,
                task_id=request.task_id,
                plan=request.plan,
            )

    def verify_grant(
        self,
        context_id: str,
        request: GrantVerificationRequest,
    ) -> GrantVerificationResult:
        with self._access(context_id) as context:
            return context.authority.verify_grant(
                token=request.token,
                run_id=request.run_id,
                task_id=request.task_id,
                plan=request.plan,
            )
