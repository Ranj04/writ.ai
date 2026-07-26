from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class ArtifactKind(StrEnum):
    DECISION = "Decision"
    SPECIFICATION = "Specification"
    TICKET = "Ticket"
    TASK = "Task"
    AGENT_PLAN = "AgentPlan"
    PULL_REQUEST = "PullRequest"
    CODE_CHANGE = "CodeChange"
    EVIDENCE = "Evidence"
    AGENT_RUN = "AgentRun"


class EdgeKind(StrEnum):
    SUPERSEDES = "SUPERSEDES"
    AMENDS = "AMENDS"
    CONTRADICTS = "CONTRADICTS"
    BASIS_FOR = "BASIS_FOR"
    CREATES = "CREATES"
    DECOMPOSES_TO = "DECOMPOSES_TO"
    CURRENTLY_DRIVES = "CURRENTLY_DRIVES"
    IMPLEMENTS = "IMPLEMENTS"
    SUPPORTED_BY = "SUPPORTED_BY"


class ApprovalStatus(StrEnum):
    PROPOSAL = "proposal"
    APPROVED = "approved"
    REJECTED = "rejected"


class ValidityStatus(StrEnum):
    VALID = "VALID"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    INVALIDATED = "INVALIDATED"


class Verdict(StrEnum):
    ALLOW = "ALLOW"
    REPLAN = "REPLAN"
    BLOCK = "BLOCK"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class VerificationCode(StrEnum):
    VALID = "VALID"
    INVALID_TOKEN = "INVALID_TOKEN"
    NON_ALLOW_VERDICT = "NON_ALLOW_VERDICT"
    EXPIRED = "EXPIRED"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    PLAN_HASH_MISMATCH = "PLAN_HASH_MISMATCH"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    CURRENT_PLAN_REJECTED = "CURRENT_PLAN_REJECTED"


class LoopState(StrEnum):
    PLAN = "PLAN"
    VERIFY = "VERIFY"
    ACT = "ACT"
    REPLAN = "REPLAN"
    BLOCKED = "BLOCKED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    COMPLETE = "COMPLETE"


class Artifact(BaseModel):
    id: str
    kind: ArtifactKind
    title: str
    text: str = ""
    scopes: set[str] = Field(default_factory=set)
    validity: ValidityStatus = ValidityStatus.VALID
    invalidated_scopes: set[str] = Field(default_factory=set)
    approval_status: ApprovalStatus | None = None
    authority_role: str | None = None
    confidence: float = 1.0
    effective_at: datetime | None = None
    source_ref: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class Edge(BaseModel):
    source_id: str
    target_id: str
    kind: EdgeKind
    scopes: set[str] = Field(default_factory=set)
    evidence_ref: str | None = None


class PlanAction(BaseModel):
    id: str
    description: str
    scopes: set[str] = Field(default_factory=set)
    attributes: dict[str, Any] = Field(default_factory=dict)


class AgentPlan(BaseModel):
    id: str
    ticket_id: str
    objective: str
    actions: list[PlanAction]

    @property
    def scopes(self) -> set[str]:
        return set().union(*(action.scopes for action in self.actions)) if self.actions else set()


class AgentRun(BaseModel):
    run_id: str
    ticket_id: str
    plan: AgentPlan
    state: LoopState = LoopState.PLAN
    tests_passed: bool = False
    graph_snapshot: str | None = None
    grant_token: str | None = None
    history: list[str] = Field(default_factory=list)


class DecisionMutation(BaseModel):
    decision: Artifact
    supersedes_id: str
    affected_scopes: set[str]


class InvalidationPath(BaseModel):
    artifact_id: str
    node_ids: list[str]


class InvalidationReport(BaseModel):
    graph_version: str
    changed_decision_id: str
    superseded_decision_id: str
    affected_scopes: set[str]
    affected_artifact_ids: list[str] = Field(default_factory=list)
    upstream_chain_artifact_ids: list[str] = Field(default_factory=list)
    preserved_task_ids: list[str] = Field(default_factory=list)
    invalidated_task_ids: list[str] = Field(default_factory=list)
    needs_review_artifact_ids: list[str] = Field(default_factory=list)
    stopped_work_artifact_ids: list[str] = Field(default_factory=list)
    directly_mentioned_artifact_ids: list[str] = Field(default_factory=list)
    preserved_artifact_ids: list[str] = Field(default_factory=list)
    paths: list[InvalidationPath] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class MutationResult(BaseModel):
    applied: bool
    reason: str
    graph_version: str
    verdict: Verdict | None = None
    report: InvalidationReport | None = None


class GrantPayload(BaseModel):
    authorization_id: str
    run_id: str
    task_id: str
    decision_snapshot: str
    plan_hash: str
    verdict: Verdict
    issued_at: datetime
    expires_at: datetime


class SignedGrant(BaseModel):
    payload: GrantPayload
    token: str


class PlanMismatch(BaseModel):
    action_id: str
    scope: str
    expected: dict[str, Any]
    actual: dict[str, Any]


class AuthorizationResult(BaseModel):
    verdict: Verdict
    reason: str
    graph_version: str
    task_id: str
    affected_scopes: set[str] = Field(default_factory=set)
    mismatches: list[PlanMismatch] = Field(default_factory=list)
    current_requirements: dict[str, dict[str, Any]] = Field(default_factory=dict)
    invalidation_path: list[str] = Field(default_factory=list)
    invalidated_artifact_ids: list[str] = Field(default_factory=list)
    preserved_artifact_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    grant: SignedGrant | None = None


class AuthorizationRequest(BaseModel):
    run_id: str
    task_id: str
    plan: AgentPlan


class GrantVerificationRequest(BaseModel):
    token: str
    run_id: str
    task_id: str
    plan: AgentPlan


class GrantVerificationResult(BaseModel):
    valid: bool
    code: VerificationCode
    reason: str
    payload: GrantPayload | None = None
