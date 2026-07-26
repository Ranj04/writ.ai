from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from writai.domain import (
    AgentPlan,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    AuthorizationResult,
    DecisionMutation,
    Edge,
    EdgeKind,
    GrantPayload,
    InvalidationReport,
    MutationResult,
    PlanMismatch,
    ValidityStatus,
    Verdict,
    VerificationCode,
    utc_now,
)
from writai.hashing import stable_hash
from writai.intake.approval import ApprovalEvidence, PendingApproval
from writai.integrations.callwright import CallReceipt
from writai.provenance import AUTHORITY_DOWNSTREAM_EDGE_KINDS
from writai.workspaces.supervisor import (
    WorkspaceSupervisor,
    resolve_plan_actions_for_tasks,
)

_WORKSPACE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{2,63}$"


class SlackUserIdentityBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    slack_user_id: str = Field(min_length=1, max_length=255)
    hexclave_user_id: str = Field(min_length=1, max_length=255)
    evidence_ref: str = Field(min_length=1, max_length=1_000)


class WorkspaceSlackBinding(BaseModel):
    """Provisioned tenant and identity boundary for one Slack intake source."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    workspace_id: str = Field(
        min_length=3,
        max_length=64,
        pattern=_WORKSPACE_ID_PATTERN,
    )
    slack_team_id: str = Field(min_length=1, max_length=255)
    composio_connection_user_id: str = Field(min_length=1, max_length=255)
    hexclave_team_id: str = Field(min_length=1, max_length=255)
    user_identities: tuple[SlackUserIdentityBinding, ...] = ()

    @model_validator(mode="after")
    def validate_binding(self) -> WorkspaceSlackBinding:
        if self.composio_connection_user_id.casefold() == "default":
            raise ValueError(
                "The Composio connection must use an explicit non-default user ID."
            )
        slack_ids = [item.slack_user_id for item in self.user_identities]
        if len(slack_ids) != len(set(slack_ids)):
            raise ValueError("Each Slack user may have only one identity binding.")
        return self

    def hexclave_user_for(self, slack_user_id: str) -> str | None:
        return next(
            (
                item.hexclave_user_id
                for item in self.user_identities
                if item.slack_user_id == slack_user_id
            ),
            None,
        )


class LiveWorkspaceStatus(StrEnum):
    IMPORTED = "imported"
    BASELINE_APPROVED = "baseline-approved"
    AUTHORIZED = "authorized"
    CHANGE_PROPOSED = "change-proposed"
    CHANGE_APPLIED = "change-applied"
    INITIAL_GRANT_REJECTED = "initial-grant-rejected"
    PLAN_UPDATED = "plan-updated"
    REAUTHORIZED = "reauthorized"
    COMPLETE = "complete"


class LiveWorkspaceImportRequest(BaseModel):
    id: str = Field(min_length=3, max_length=64, pattern=_WORKSPACE_ID_PATTERN)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    authority_policy: dict[str, set[str]] = Field(min_length=1)
    baseline_decision: Artifact
    specification: Artifact
    ticket: Artifact
    tasks: list[Artifact] = Field(min_length=1)
    plan: AgentPlan
    edges: list[Edge] | None = None
    graph_version: int = Field(default=17, ge=0)
    slack_binding: WorkspaceSlackBinding | None = None

    @model_validator(mode="after")
    def validate_workspace_shape(self) -> LiveWorkspaceImportRequest:
        if (
            self.slack_binding is not None
            and self.slack_binding.workspace_id != self.id
        ):
            raise ValueError(
                "slack_binding.workspace_id must match the Workspace ID"
            )
        expected_kinds = (
            (self.baseline_decision, ArtifactKind.DECISION, "baseline_decision"),
            (self.specification, ArtifactKind.SPECIFICATION, "specification"),
            (self.ticket, ArtifactKind.TICKET, "ticket"),
        )
        for artifact, expected, label in expected_kinds:
            if artifact.kind is not expected:
                raise ValueError(f"{label} must be a {expected.value} artifact")
        if self.baseline_decision.approval_status is not ApprovalStatus.PROPOSAL:
            raise ValueError("baseline_decision must be imported as a proposal")
        if any(task.kind is not ArtifactKind.TASK for task in self.tasks):
            raise ValueError("tasks may contain only Task artifacts")
        if self.plan.ticket_id != self.ticket.id:
            raise ValueError("plan.ticket_id must match the imported ticket")

        artifacts = [
            self.baseline_decision,
            self.specification,
            self.ticket,
            *self.tasks,
        ]
        ids = [artifact.id for artifact in artifacts]
        ids.append(self.plan.id)
        if len(ids) != len(set(ids)):
            raise ValueError("workspace artifact and plan IDs must be unique")

        baseline_scopes = self.baseline_decision.scopes
        requirements = self.baseline_decision.attributes.get("requirements")
        if (
            not isinstance(requirements, dict)
            or set(requirements) != baseline_scopes
            or any(not isinstance(requirement, dict) for requirement in requirements.values())
        ):
            raise ValueError(
                "baseline decision requirements must be objects and exactly match its scopes"
            )
        if not baseline_scopes:
            raise ValueError("baseline decision must govern at least one scope")
        authority_role = self.baseline_decision.authority_role
        if authority_role is None:
            raise ValueError("baseline decision must declare authority_role")
        for scope in baseline_scopes:
            roles = self.authority_policy.get(scope, set())
            if not roles:
                raise ValueError(f"authority_policy has no roles for scope {scope!r}")
            if authority_role not in roles:
                raise ValueError(
                    f"baseline authority_role is not authorized for scope {scope!r}"
                )

            missing_from: list[str] = []
            if scope not in self.specification.scopes:
                missing_from.append("Specification")
            if scope not in self.ticket.scopes:
                missing_from.append("Ticket")
            if not any(scope in task.scopes for task in self.tasks):
                missing_from.append("Task")
            if scope not in self.plan.scopes or not any(
                scope in action.scopes for action in self.plan.actions
            ):
                missing_from.append("AgentPlan action")
            if missing_from:
                raise ValueError(
                    f"baseline requirement scope {scope!r} is missing from: "
                    + ", ".join(missing_from)
                )
            requirement = requirements[scope]
            for action in self.plan.actions:
                if scope not in action.scopes:
                    continue
                actual = {
                    key: action.attributes.get(key)
                    for key in requirement
                }
                if actual != requirement:
                    raise ValueError(
                        f"initial plan action {action.id!r} does not satisfy "
                        f"baseline requirement scope {scope!r}"
                    )

        tasks_by_id = {task.id: task for task in self.tasks}
        for action in self.plan.actions:
            if "task_id" not in action.attributes:
                continue
            task_id = action.attributes.get("task_id")
            referenced_task = (
                tasks_by_id.get(task_id) if isinstance(task_id, str) else None
            )
            if (
                referenced_task is None
                or referenced_task.validity is not ValidityStatus.VALID
            ):
                raise ValueError(
                    f"initial plan action {action.id!r} references a missing "
                    "or non-valid Task"
                )
        resolved_actions = resolve_plan_actions_for_tasks(
            self.tasks,
            self.plan,
        )
        unplanned_task_ids = [
            task.id
            for task in self.tasks
            if task.validity is ValidityStatus.VALID
            and not resolved_actions.get(task.id)
        ]
        if unplanned_task_ids:
            raise ValueError(
                "every Task must be represented by an AgentPlan action; "
                "missing: "
                + ", ".join(sorted(unplanned_task_ids))
            )

        known_ids = set(ids)
        if self.edges is not None:
            for edge in self.edges:
                if edge.source_id not in known_ids or edge.target_id not in known_ids:
                    raise ValueError(
                        f"edge endpoints must reference imported artifacts: "
                        f"{edge.source_id} -> {edge.target_id}"
                    )
        authority_edges = self.graph_edges()
        outgoing: dict[str, list[str]] = {}
        for edge in authority_edges:
            if edge.kind in AUTHORITY_DOWNSTREAM_EDGE_KINDS:
                outgoing.setdefault(edge.source_id, []).append(edge.target_id)

        def reachable(source_id: str, target_id: str) -> bool:
            pending = [source_id]
            visited: set[str] = set()
            while pending:
                current = pending.pop()
                if current == target_id:
                    return True
                if current in visited:
                    continue
                visited.add(current)
                pending.extend(outgoing.get(current, []))
            return False

        required_segments = [
            (
                self.baseline_decision.id,
                self.specification.id,
                "baseline Decision to Specification",
            ),
            (self.specification.id, self.ticket.id, "Specification to Ticket"),
            *(
                (self.ticket.id, task.id, f"Ticket to Task {task.id}")
                for task in self.tasks
            ),
            *(
                (task.id, self.plan.id, f"Task {task.id} to AgentPlan")
                for task in self.tasks
            ),
        ]
        disconnected = [
            label
            for source_id, target_id, label in required_segments
            if not reachable(source_id, target_id)
        ]
        if disconnected:
            raise ValueError(
                "workspace graph is missing authority provenance: "
                + ", ".join(disconnected)
            )

        def scoped_edge(
            source_id: str,
            target_id: str,
            kind: EdgeKind,
            scope: str,
        ) -> bool:
            return any(
                edge.source_id == source_id
                and edge.target_id == target_id
                and edge.kind is kind
                and scope in edge.scopes
                for edge in authority_edges
            )

        disconnected_scopes: list[str] = []
        for scope in sorted(baseline_scopes):
            scoped_tasks = [task for task in self.tasks if scope in task.scopes]
            has_continuous_path = (
                scoped_edge(
                    self.baseline_decision.id,
                    self.specification.id,
                    EdgeKind.BASIS_FOR,
                    scope,
                )
                and scoped_edge(
                    self.specification.id,
                    self.ticket.id,
                    EdgeKind.CREATES,
                    scope,
                )
                and any(
                    scoped_edge(
                        self.ticket.id,
                        task.id,
                        EdgeKind.DECOMPOSES_TO,
                        scope,
                    )
                    and scoped_edge(
                        task.id,
                        self.plan.id,
                        EdgeKind.CURRENTLY_DRIVES,
                        scope,
                    )
                    for task in scoped_tasks
                )
            )
            if not has_continuous_path:
                disconnected_scopes.append(scope)
        if disconnected_scopes:
            raise ValueError(
                "workspace graph lacks a continuous scoped authority path for: "
                + ", ".join(disconnected_scopes)
            )
        return self

    def graph_artifacts(self) -> list[Artifact]:
        plan_artifact = Artifact(
            id=self.plan.id,
            kind=ArtifactKind.AGENT_PLAN,
            title=self.plan.objective,
            text=self.plan.objective,
            scopes=self.plan.scopes,
            source_ref=f"workspace://{self.id}/plans/{self.plan.id}",
            attributes={"representation": "agent-plan"},
        )
        return [
            self.baseline_decision.model_copy(deep=True),
            self.specification.model_copy(deep=True),
            self.ticket.model_copy(deep=True),
            *(task.model_copy(deep=True) for task in self.tasks),
            plan_artifact,
        ]

    def graph_edges(self) -> list[Edge]:
        if self.edges is not None:
            edges = [edge.model_copy(deep=True) for edge in self.edges]
            connected_tasks = {
                edge.source_id
                for edge in edges
                if edge.target_id == self.plan.id
                and edge.kind is EdgeKind.CURRENTLY_DRIVES
            }
            for task in self.tasks:
                if task.id not in connected_tasks:
                    edges.append(
                        Edge(
                            source_id=task.id,
                            target_id=self.plan.id,
                            kind=EdgeKind.CURRENTLY_DRIVES,
                            scopes=task.scopes & self.plan.scopes,
                            evidence_ref=f"workspace://{self.id}/plans/{self.plan.id}",
                        )
                    )
            return edges
        scope_union = set().union(
            self.baseline_decision.scopes,
            self.specification.scopes,
            self.ticket.scopes,
        )
        edges = [
            Edge(
                source_id=self.baseline_decision.id,
                target_id=self.specification.id,
                kind=EdgeKind.BASIS_FOR,
                scopes=self.baseline_decision.scopes,
                evidence_ref=self.baseline_decision.source_ref,
            ),
            Edge(
                source_id=self.specification.id,
                target_id=self.ticket.id,
                kind=EdgeKind.CREATES,
                scopes=scope_union,
                evidence_ref=self.specification.source_ref,
            ),
        ]
        for task in self.tasks:
            edges.extend(
                [
                    Edge(
                        source_id=self.ticket.id,
                        target_id=task.id,
                        kind=EdgeKind.DECOMPOSES_TO,
                        scopes=task.scopes,
                        evidence_ref=task.source_ref,
                    ),
                    Edge(
                        source_id=task.id,
                        target_id=self.plan.id,
                        kind=EdgeKind.CURRENTLY_DRIVES,
                        scopes=task.scopes & self.plan.scopes,
                        evidence_ref=f"workspace://{self.id}/plans/{self.plan.id}",
                    ),
                ]
            )
        return edges


class WorkspaceApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_role: str = Field(min_length=1, max_length=128)
    proposal_fingerprint: str | None = Field(
        default=None,
        min_length=71,
        max_length=71,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    approval_evidence: ApprovalEvidence | None = None
    proposal_instance_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
    )

    @model_validator(mode="after")
    def validate_decision_evidence_pair(self) -> WorkspaceApprovalRequest:
        if (self.proposal_fingerprint is None) != (
            self.approval_evidence is None
        ):
            raise ValueError(
                "proposal_fingerprint and approval_evidence must be supplied together"
            )
        if (self.approval_evidence is None) != (
            self.proposal_instance_id is None
        ):
            raise ValueError(
                "proposal_instance_id and approval_evidence must be supplied together"
            )
        return self


class WorkspaceProposalRequest(BaseModel):
    decision: Artifact
    supersedes_id: str = Field(min_length=1, max_length=160)
    affected_scopes: set[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_proposal(self) -> WorkspaceProposalRequest:
        if self.decision.kind is not ArtifactKind.DECISION:
            raise ValueError("decision must be a Decision artifact")
        if self.decision.approval_status is not ApprovalStatus.PROPOSAL:
            raise ValueError("decision changes must be submitted as proposals")
        return self

    def mutation(self) -> DecisionMutation:
        return DecisionMutation(
            decision=self.decision.model_copy(deep=True),
            supersedes_id=self.supersedes_id,
            affected_scopes=set(self.affected_scopes),
        )


class WorkspacePlanUpdateRequest(BaseModel):
    plan: AgentPlan


class WorkspaceEmptyRequest(BaseModel):
    pass


class WorkspaceApprovalPreview(BaseModel):
    pending: PendingApproval
    interrupted_assignment_ids: tuple[str, ...] = ()
    preserved_assignment_ids: tuple[str, ...] = ()
    interrupted_count: int = Field(ge=0)
    preserved_count: int = Field(ge=0)
    total_assignment_count: int = Field(ge=0)
    assignment_provenance_paths: dict[str, tuple[str, ...]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def validate_partition(self) -> WorkspaceApprovalPreview:
        interrupted = set(self.interrupted_assignment_ids)
        preserved = set(self.preserved_assignment_ids)
        if interrupted & preserved:
            raise ValueError("Approval preview partitions must not overlap.")
        if set(self.assignment_provenance_paths) != interrupted:
            raise ValueError(
                "Every interrupted assignment needs one provenance path."
            )
        if (
            self.interrupted_count != len(self.interrupted_assignment_ids)
            or self.preserved_count != len(self.preserved_assignment_ids)
            or self.total_assignment_count
            != self.interrupted_count + self.preserved_count
        ):
            raise ValueError("Approval preview counts do not match its partition.")
        return self


class WorkspaceEvent(BaseModel):
    sequence: int = Field(ge=1)
    event_type: str
    detail: str
    created_at: datetime = Field(default_factory=utc_now)
    actor_role: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class WorkspaceExecutionResult(BaseModel):
    applied: bool
    reason: str
    verification_code: VerificationCode
    pull_request_url: str | None = None
    execution_mode: Literal["simulated", "live"] | None = None
    call_receipt: CallReceipt | None = None


class WorkspaceAuthorizationView(BaseModel):
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
    grant: GrantPayload | None = None

    @classmethod
    def from_result(
        cls, result: AuthorizationResult | None
    ) -> WorkspaceAuthorizationView | None:
        if result is None:
            return None
        return cls(
            verdict=result.verdict,
            reason=result.reason,
            graph_version=result.graph_version,
            task_id=result.task_id,
            affected_scopes=result.affected_scopes,
            mismatches=result.mismatches,
            current_requirements=result.current_requirements,
            invalidation_path=result.invalidation_path,
            invalidated_artifact_ids=result.invalidated_artifact_ids,
            preserved_artifact_ids=result.preserved_artifact_ids,
            evidence_refs=result.evidence_refs,
            grant=result.grant.payload if result.grant else None,
        )


class ApprovedWorkspaceMutation(BaseModel):
    mutation: DecisionMutation
    actor_role: str
    approval_evidence: ApprovalEvidence | None = None


class WorkspaceDecisionApprovalIntent(BaseModel):
    """Durable recovery record for an approval crossing the authority boundary."""

    model_config = ConfigDict(extra="forbid")

    mutation: DecisionMutation
    actor_role: str = Field(min_length=1, max_length=128)
    proposal_fingerprint: str = Field(
        min_length=71,
        max_length=71,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    proposal_instance_id: str = Field(min_length=1, max_length=255)
    approval_evidence: ApprovalEvidence
    base_graph_version: str = Field(min_length=1, max_length=255)
    prepared_at: datetime = Field(default_factory=utc_now)
    mutation_result: MutationResult | None = None
    authorization_result: AuthorizationResult | None = None

    @model_validator(mode="after")
    def validate_intent(self) -> WorkspaceDecisionApprovalIntent:
        evidence = self.approval_evidence
        decision_id = self.mutation.decision.id
        if self.proposal_fingerprint != stable_hash(self.mutation):
            raise ValueError(
                "Approval intent fingerprint must match its exact mutation."
            )
        if (
            evidence.decision_id != decision_id
            or evidence.permission_id != self.actor_role
            or evidence.confirmed_proposal_fingerprint
            != self.proposal_fingerprint
            or evidence.confirmed_proposal_instance_id
            != self.proposal_instance_id
        ):
            raise ValueError(
                "Approval intent evidence must match its exact proposal."
            )
        if self.prepared_at.utcoffset() is None:
            raise ValueError("Approval intent prepared_at must be timezone-aware.")
        if self.mutation_result is not None:
            result = self.mutation_result
            if (
                not result.applied
                or result.report is None
                or result.report.graph_version != result.graph_version
                or result.report.changed_decision_id != decision_id
            ):
                raise ValueError(
                    "Approval intent mutation result must prove the exact change."
                )
        if self.authorization_result is not None:
            if self.mutation_result is None:
                raise ValueError(
                    "Approval intent cannot authorize before mutation evidence."
                )
            if (
                self.authorization_result.graph_version
                != self.mutation_result.graph_version
            ):
                raise ValueError(
                    "Approval intent authorization must use the mutated graph."
                )
        return self


class LiveWorkspaceRecord(BaseModel):
    definition: LiveWorkspaceImportRequest
    context_id: str
    status: LiveWorkspaceStatus = LiveWorkspaceStatus.IMPORTED
    graph_version: str
    baseline_approved: bool = False
    baseline_approval_role: str | None = None
    baseline_proposal_instance_id: str | None = None
    baseline_approval_evidence: ApprovalEvidence | None = None
    current_plan: AgentPlan
    pending_mutation: DecisionMutation | None = None
    proposal_sequence: int = Field(default=0, ge=0)
    pending_proposal_instance_id: str | None = None
    decision_approval_intent: WorkspaceDecisionApprovalIntent | None = None
    approved_mutations: list[ApprovedWorkspaceMutation] = Field(default_factory=list)
    initial_authorization: AuthorizationResult | None = None
    conflict_authorization: AuthorizationResult | None = None
    replacement_authorization: AuthorizationResult | None = None
    invalidation_report: InvalidationReport | None = None
    initial_verification: WorkspaceExecutionResult | None = None
    replacement_verification: WorkspaceExecutionResult | None = None
    supervisor: WorkspaceSupervisor | None = None
    history: list[WorkspaceEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class LiveWorkspaceView(BaseModel):
    id: str
    name: str
    description: str
    status: LiveWorkspaceStatus
    graph_version: str
    baseline_approved: bool
    baseline_proposal_fingerprint: str
    baseline_proposal_instance_id: str | None = None
    baseline_approval_evidence: ApprovalEvidence | None = None
    baseline_decision: Artifact
    specification: Artifact
    ticket: Artifact
    tasks: list[Artifact]
    initial_plan: AgentPlan
    current_plan: AgentPlan
    authority_policy: dict[str, set[str]]
    slack_binding: WorkspaceSlackBinding | None = None
    pending_mutation: DecisionMutation | None = None
    pending_proposal_fingerprint: str | None = None
    pending_proposal_instance_id: str | None = None
    approved_mutations: list[ApprovedWorkspaceMutation] = Field(default_factory=list)
    latest_approved_mutation: DecisionMutation | None = None
    initial_authorization: WorkspaceAuthorizationView | None = None
    conflict_authorization: WorkspaceAuthorizationView | None = None
    replacement_authorization: WorkspaceAuthorizationView | None = None
    invalidation_report: InvalidationReport | None = None
    initial_verification: WorkspaceExecutionResult | None = None
    replacement_verification: WorkspaceExecutionResult | None = None
    supervisor: WorkspaceSupervisor | None = None
    history: list[WorkspaceEvent]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: LiveWorkspaceRecord) -> LiveWorkspaceView:
        definition = record.definition
        slack_binding = (
            definition.slack_binding.model_copy(
                update={"user_identities": ()},
                deep=True,
            )
            if definition.slack_binding is not None
            else None
        )
        baseline = definition.baseline_decision.model_copy(deep=True)
        if record.baseline_approved:
            baseline.approval_status = ApprovalStatus.APPROVED
        approved_mutations = [
            item.model_copy(deep=True) for item in record.approved_mutations
        ]
        for item in approved_mutations:
            item.mutation.decision.approval_status = ApprovalStatus.APPROVED
            evidence = item.approval_evidence
            extraction = item.mutation.decision.attributes.get("extraction")
            if evidence is not None and isinstance(extraction, dict):
                item.mutation.decision.attributes["extraction"] = {
                    **extraction,
                    "human_reviewed": True,
                    "reviewed_at": evidence.approved_at.isoformat(),
                    "reviewed_by": evidence.approver_user_id,
                    "approval_channel": evidence.channel.value,
                    "approval_evidence_ref": evidence.evidence_ref,
                    "confirmed_proposal_fingerprint": (
                        evidence.confirmed_proposal_fingerprint
                    ),
                    "confirmed_proposal_instance_id": (
                        evidence.confirmed_proposal_instance_id
                    ),
                }
        latest_approved_mutation = (
            approved_mutations[-1].mutation.model_copy(deep=True)
            if approved_mutations
            else None
        )
        return cls(
            id=definition.id,
            name=definition.name,
            description=definition.description,
            status=record.status,
            graph_version=record.graph_version,
            baseline_approved=record.baseline_approved,
            baseline_proposal_fingerprint=stable_hash(
                definition.baseline_decision
            ),
            baseline_proposal_instance_id=record.baseline_proposal_instance_id,
            baseline_approval_evidence=record.baseline_approval_evidence,
            baseline_decision=baseline,
            specification=definition.specification,
            ticket=definition.ticket,
            tasks=definition.tasks,
            initial_plan=definition.plan,
            current_plan=record.current_plan,
            authority_policy=definition.authority_policy,
            slack_binding=slack_binding,
            pending_mutation=record.pending_mutation,
            pending_proposal_fingerprint=(
                stable_hash(record.pending_mutation)
                if record.pending_mutation is not None
                else None
            ),
            pending_proposal_instance_id=record.pending_proposal_instance_id,
            approved_mutations=approved_mutations,
            latest_approved_mutation=latest_approved_mutation,
            initial_authorization=WorkspaceAuthorizationView.from_result(
                record.initial_authorization
            ),
            conflict_authorization=WorkspaceAuthorizationView.from_result(
                record.conflict_authorization
            ),
            replacement_authorization=WorkspaceAuthorizationView.from_result(
                record.replacement_authorization
            ),
            invalidation_report=record.invalidation_report,
            initial_verification=record.initial_verification,
            replacement_verification=record.replacement_verification,
            supervisor=record.supervisor,
            history=record.history,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class LiveWorkspaceList(BaseModel):
    workspaces: list[LiveWorkspaceView]
