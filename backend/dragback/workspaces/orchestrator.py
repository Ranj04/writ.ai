from __future__ import annotations

from collections import deque
from threading import RLock
from typing import NoReturn
from uuid import uuid4

from dragback.domain import (
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    AuthorizationRequest,
    Edge,
    EdgeKind,
    MutationResult,
    Verdict,
    VerificationCode,
    utc_now,
)
from dragback.hashing import stable_hash
from dragback.intake.approval import ApprovalEvidence, pending_from_workspace
from dragback.provenance import (
    AUTHORITY_DOWNSTREAM_EDGE_KINDS,
    authority_edge_sort_key,
)
from dragback.services.support import ApiError
from dragback.workspaces.authority_contexts import (
    DynamicAuthorityContextCreateRequest,
    DynamicAuthorityContextState,
    DynamicMutationApprovalRequest,
)
from dragback.workspaces.models import (
    ApprovedWorkspaceMutation,
    LiveWorkspaceImportRequest,
    LiveWorkspaceList,
    LiveWorkspaceRecord,
    LiveWorkspaceStatus,
    LiveWorkspaceView,
    WorkspaceApprovalPreview,
    WorkspaceApprovalRequest,
    WorkspaceDecisionApprovalIntent,
    WorkspaceEvent,
    WorkspacePlanUpdateRequest,
    WorkspaceProposalRequest,
)
from dragback.workspaces.repository import LiveWorkspaceRepository
from dragback.workspaces.supervisor import (
    FixtureSupervisorRuntime,
    SupervisorAssignment,
    SupervisorAssignmentState,
    SupervisorExecutionMode,
    SupervisorLifecycleState,
    SupervisorRuntimeAdapter,
    resolve_plan_actions_for_assignments,
)
from dragback.workspaces.transport import (
    HttpLiveWorkspaceTransport,
    LiveWorkspaceTransport,
)


class LiveWorkspaceStateConflict(ValueError):
    pass


class LiveWorkspaceOrchestrator:
    """Persistent agent-owned workflow over HTTP authority and executor boundaries."""

    def __init__(
        self,
        *,
        repository: LiveWorkspaceRepository,
        transport: LiveWorkspaceTransport | None = None,
        supervisor_runtime: SupervisorRuntimeAdapter | None = None,
    ) -> None:
        self._repository = repository
        self._transport = transport or HttpLiveWorkspaceTransport()
        self._supervisor_runtime = supervisor_runtime or FixtureSupervisorRuntime()
        self._lock = RLock()

    @staticmethod
    def _run_id(record: LiveWorkspaceRecord) -> str:
        return f"LIVE-{record.definition.id.upper()}-RUN"

    @staticmethod
    def _event(
        record: LiveWorkspaceRecord,
        *,
        event_type: str,
        detail: str,
        actor_role: str | None = None,
        data: dict[str, object] | None = None,
    ) -> None:
        record.history.append(
            WorkspaceEvent(
                sequence=len(record.history) + 1,
                event_type=event_type,
                detail=detail,
                actor_role=actor_role,
                data=data or {},
            )
        )
        record.updated_at = utc_now()

    @staticmethod
    def _conflict(message: str) -> NoReturn:
        raise LiveWorkspaceStateConflict(message)

    @staticmethod
    def _same_approval_attempt(
        expected: ApprovalEvidence,
        submitted: ApprovalEvidence,
    ) -> bool:
        """Match a retry while retaining the first durable approval timestamp."""

        return (
            submitted.workspace_id == expected.workspace_id
            and submitted.decision_id == expected.decision_id
            and submitted.approver_user_id == expected.approver_user_id
            and submitted.permission_id == expected.permission_id
            and submitted.channel is expected.channel
            and submitted.evidence_ref == expected.evidence_ref
            and submitted.confirmed_proposal_fingerprint
            == expected.confirmed_proposal_fingerprint
            and submitted.confirmed_proposal_instance_id
            == expected.confirmed_proposal_instance_id
        )

    @staticmethod
    def _completed_approval_matches(
        record: LiveWorkspaceRecord,
        *,
        decision_id: str,
        request: WorkspaceApprovalRequest,
    ) -> bool:
        return any(
            approved.mutation.decision.id == decision_id
            and stable_hash(approved.mutation) == request.proposal_fingerprint
            and approved.actor_role == request.actor_role
            and approved.approval_evidence is not None
            and approved.approval_evidence.confirmed_proposal_instance_id
            == request.proposal_instance_id
            for approved in record.approved_mutations
        )

    def _context_request(
        self, record: LiveWorkspaceRecord
    ) -> DynamicAuthorityContextCreateRequest:
        definition = record.definition
        return DynamicAuthorityContextCreateRequest(
            context_id=record.context_id,
            version=definition.graph_version,
            artifacts=definition.graph_artifacts(),
            edges=definition.graph_edges(),
            authority_policy=definition.authority_policy,
            baseline_decision_id=definition.baseline_decision.id,
            slack_binding=definition.slack_binding,
        )

    @staticmethod
    def _governance_signature(artifact: Artifact) -> tuple[object, ...]:
        return (
            artifact.id,
            artifact.kind,
            artifact.title,
            artifact.text,
            frozenset(artifact.scopes),
            artifact.authority_role,
            artifact.confidence,
            artifact.effective_at,
            artifact.source_ref,
            artifact.attributes,
        )

    def _context_matches_record(
        self,
        record: LiveWorkspaceRecord,
        state: DynamicAuthorityContextState,
    ) -> bool:
        if (
            state.graph_version != record.graph_version
            or state.baseline_decision_id
            != record.definition.baseline_decision.id
            or state.baseline_approved is not record.baseline_approved
            or state.authority_policy != record.definition.authority_policy
            or state.slack_binding != record.definition.slack_binding
        ):
            return False
        expected_evidence = {
            approved.mutation.decision.id: approved.approval_evidence
            for approved in record.approved_mutations
            if approved.approval_evidence is not None
        }
        if state.approval_evidence != expected_evidence:
            return False

        expected_artifacts = {
            artifact.id: artifact
            for artifact in record.definition.graph_artifacts()
        }
        expected_decisions = {
            record.definition.baseline_decision.id:
                record.definition.baseline_decision.model_copy(deep=True)
        }
        if record.baseline_approved:
            expected_decisions[
                record.definition.baseline_decision.id
            ].approval_status = ApprovalStatus.APPROVED
        for approved in record.approved_mutations:
            decision = approved.mutation.decision.model_copy(deep=True)
            decision.approval_status = ApprovalStatus.APPROVED
            evidence = approved.approval_evidence
            extraction = decision.attributes.get("extraction")
            if evidence is not None and isinstance(extraction, dict):
                decision.attributes["extraction"] = {
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
            expected_artifacts[decision.id] = decision
            expected_decisions[decision.id] = decision

        actual_artifacts = {artifact.id: artifact for artifact in state.artifacts}
        if set(actual_artifacts) != set(expected_artifacts):
            return False
        for artifact_id, expected in expected_artifacts.items():
            if self._governance_signature(
                actual_artifacts[artifact_id]
            ) != self._governance_signature(expected):
                return False
        actual_decisions = {
            artifact.id: artifact
            for artifact in state.artifacts
            if artifact.kind is ArtifactKind.DECISION
        }
        if set(actual_decisions) != set(expected_decisions):
            return False
        if any(
            actual_decisions[decision_id].approval_status
            is not expected.approval_status
            for decision_id, expected in expected_decisions.items()
        ):
            return False

        expected_supersessions = {
            (approved.mutation.decision.id, approved.mutation.supersedes_id)
            for approved in record.approved_mutations
        }
        actual_supersessions = {
            (edge.source_id, edge.target_id)
            for edge in state.edges
            if edge.kind is EdgeKind.SUPERSEDES
        }
        return actual_supersessions == expected_supersessions

    def _ensure_context(self, record: LiveWorkspaceRecord) -> None:
        current = self._transport.context_state(record.context_id)
        if current is not None and self._context_matches_record(record, current):
            return
        if current is not None:
            self._transport.delete_context(record.context_id)
        state = self._transport.create_context(self._context_request(record))
        if record.baseline_approved:
            if record.baseline_approval_role is None:
                raise RuntimeError("Approved baseline is missing its approval role.")
            state = self._transport.approve_baseline(
                record.context_id,
                WorkspaceApprovalRequest(actor_role=record.baseline_approval_role),
            )
        for approved in record.approved_mutations:
            if approved.approval_evidence is None:
                raise RuntimeError(
                    "Approved workspace mutation is missing durable approval evidence."
                )
            result = self._transport.approve_mutation(
                record.context_id,
                DynamicMutationApprovalRequest(
                    mutation=approved.mutation,
                    actor_role=approved.actor_role,
                    proposal_fingerprint=(
                        approved.approval_evidence.confirmed_proposal_fingerprint
                    ),
                    approval_evidence=approved.approval_evidence,
                ),
            )
            state.graph_version = result.graph_version
        if state.graph_version != record.graph_version:
            raise RuntimeError(
                "Rehydrated authority graph does not match the persisted graph version."
            )
        rebuilt = self._transport.context_state(record.context_id)
        if rebuilt is None or not self._context_matches_record(record, rebuilt):
            raise RuntimeError(
                "Rehydrated authority graph does not match the persisted lineage."
            )

    @staticmethod
    def _intent_base_record(
        record: LiveWorkspaceRecord,
        intent: WorkspaceDecisionApprovalIntent,
    ) -> LiveWorkspaceRecord:
        base = record.model_copy(deep=True)
        base.graph_version = intent.base_graph_version
        base.decision_approval_intent = None
        return base

    @staticmethod
    def _intent_projected_record(
        record: LiveWorkspaceRecord,
        intent: WorkspaceDecisionApprovalIntent,
        *,
        graph_version: str,
    ) -> LiveWorkspaceRecord:
        projected = LiveWorkspaceOrchestrator._intent_base_record(
            record,
            intent,
        )
        projected.graph_version = graph_version
        projected.approved_mutations.append(
            ApprovedWorkspaceMutation(
                mutation=intent.mutation.model_copy(deep=True),
                actor_role=intent.actor_role,
                approval_evidence=intent.approval_evidence.model_copy(deep=True),
            )
        )
        return projected

    @staticmethod
    def _intent_request(
        intent: WorkspaceDecisionApprovalIntent,
    ) -> DynamicMutationApprovalRequest:
        return DynamicMutationApprovalRequest(
            mutation=intent.mutation,
            actor_role=intent.actor_role,
            proposal_fingerprint=intent.proposal_fingerprint,
            approval_evidence=intent.approval_evidence,
        )

    def _recover_intent_mutation(
        self,
        record: LiveWorkspaceRecord,
        intent: WorkspaceDecisionApprovalIntent,
    ) -> MutationResult:
        """Converge an uncertain remote mutation to the persisted exact input."""

        base = self._intent_base_record(record, intent)
        current = self._transport.context_state(record.context_id)
        base_matches = (
            current is not None and self._context_matches_record(base, current)
        )
        applied_matches = (
            current is not None
            and self._context_matches_record(
                self._intent_projected_record(
                    record,
                    intent,
                    graph_version=current.graph_version,
                ),
                current,
            )
        )
        if current is None or (not base_matches and not applied_matches):
            self._ensure_context(base)
            current = self._transport.context_state(record.context_id)
            if current is None or not self._context_matches_record(base, current):
                raise RuntimeError(
                    "Approval recovery could not restore its base authority graph."
                )
            applied_matches = False

        previous = intent.mutation_result
        if previous is not None and applied_matches:
            if (
                current is None
                or current.graph_version != previous.graph_version
                or current.last_report != previous.report
            ):
                raise RuntimeError(
                    "Approval recovery found inconsistent remote mutation evidence."
                )
            return previous.model_copy(deep=True)

        result = self._transport.approve_mutation(
            record.context_id,
            self._intent_request(intent),
        )
        if not result.applied or result.report is None:
            self._conflict(result.reason)
        if (
            result.report.graph_version != result.graph_version
            or result.report.changed_decision_id != intent.mutation.decision.id
        ):
            raise RuntimeError(
                "Authority returned mutation evidence for a different change."
            )
        if previous is not None and result != previous:
            raise RuntimeError(
                "Approval retry returned different mutation evidence."
            )
        return result

    @staticmethod
    def _updated_intent(
        intent: WorkspaceDecisionApprovalIntent,
        **updates: object,
    ) -> WorkspaceDecisionApprovalIntent:
        payload = intent.model_dump(mode="python")
        payload.update(updates)
        return WorkspaceDecisionApprovalIntent.model_validate(payload)

    @staticmethod
    def _has_verified_stale_grant(record: LiveWorkspaceRecord) -> bool:
        verification = record.initial_verification
        return (
            verification is not None
            and not verification.applied
            and verification.verification_code is VerificationCode.STALE_SNAPSHOT
        )

    @staticmethod
    def _assignment_path(
        record: LiveWorkspaceRecord,
        assignment: SupervisorAssignment,
    ) -> list[str]:
        report = record.invalidation_report
        if report is None:
            return []
        selected: list[str] = []
        for path in report.paths:
            if (
                assignment.task_id in path.node_ids
                and len(path.node_ids) > len(selected)
            ):
                selected = list(path.node_ids)
        if not selected:
            return []
        if record.current_plan.id not in selected:
            selected.append(record.current_plan.id)
        return selected

    @staticmethod
    def _redirect_instruction(
        assignment: SupervisorAssignment,
        plan_id: str,
        actions: list[tuple[str, str]],
    ) -> str:
        descriptions = [
            description
            for action_id, description in actions
            if action_id in assignment.action_ids
        ]
        if not descriptions:
            return f"Continue {assignment.task_title} under corrected plan {plan_id}."
        return f"Under corrected plan {plan_id}: " + " ".join(descriptions)

    def _dispatch_supervisor(
        self,
        record: LiveWorkspaceRecord,
        *,
        decision_snapshot: str,
    ) -> None:
        supervisor = record.supervisor
        if supervisor is None:
            return
        supervisor.assignments = [
            (
                self._supervisor_runtime.transition(
                    assignment,
                    state=SupervisorAssignmentState.RUNNING,
                    decision_snapshot=decision_snapshot,
                )
                if assignment.state is SupervisorAssignmentState.QUEUED
                else assignment
            )
            for assignment in supervisor.assignments
        ]
        supervisor.state = SupervisorLifecycleState.RUNNING

    def _apply_supervisor_invalidation(
        self,
        record: LiveWorkspaceRecord,
    ) -> None:
        supervisor = record.supervisor
        report = record.invalidation_report
        if supervisor is None or report is None:
            return
        invalidated = set(report.invalidated_task_ids)
        preserved = set(report.preserved_task_ids)
        changed: list[SupervisorAssignment] = []
        for assignment in supervisor.assignments:
            if (
                assignment.task_id in invalidated
                and assignment.state
                in {
                    SupervisorAssignmentState.RUNNING,
                    SupervisorAssignmentState.CONTINUING,
                }
            ):
                scopes = ", ".join(sorted(report.affected_scopes))
                reason = (
                    f"Approved decision {report.changed_decision_id} changed "
                    f"{scopes} at {report.graph_version}; Dragback invalidated "
                    f"{assignment.task_id} through the recorded provenance path."
                )
                redirect_instruction = (
                    f"Stop {assignment.task_title}. Return control to the Dragback "
                    f"supervisor and request a corrected plan for {scopes} at "
                    f"{report.graph_version}."
                )
                changed.append(
                    self._supervisor_runtime.transition(
                        assignment,
                        state=SupervisorAssignmentState.INTERRUPTED,
                        interrupt_reason=reason,
                        redirect_instruction=redirect_instruction,
                        provenance_path=self._assignment_path(record, assignment),
                        interrupt_enforced=(
                            assignment.execution_mode
                            is SupervisorExecutionMode.LIVE
                        ),
                    )
                )
            elif (
                assignment.task_id in preserved
                and assignment.state is SupervisorAssignmentState.RUNNING
            ):
                changed.append(
                    self._supervisor_runtime.transition(
                        assignment,
                        state=SupervisorAssignmentState.CONTINUING,
                    )
                )
            else:
                changed.append(assignment)
        supervisor.assignments = changed
        if invalidated:
            supervisor.state = SupervisorLifecycleState.INTERRUPTING

    def _enforce_supervisor_interrupts(
        self,
        record: LiveWorkspaceRecord,
    ) -> None:
        supervisor = record.supervisor
        if supervisor is None:
            return
        supervisor.assignments = [
            (
                self._supervisor_runtime.transition(
                    assignment,
                    state=SupervisorAssignmentState.INTERRUPTED,
                    interrupt_enforced=True,
                )
                if assignment.state is SupervisorAssignmentState.INTERRUPTED
                else assignment
            )
            for assignment in supervisor.assignments
        ]

    def _redirect_supervisor(
        self,
        record: LiveWorkspaceRecord,
    ) -> None:
        supervisor = record.supervisor
        if supervisor is None:
            return
        try:
            resolved_actions = resolve_plan_actions_for_assignments(
                supervisor.assignments,
                record.current_plan,
            )
        except ValueError as error:
            self._conflict(str(error))
        actions = [
            (action.id, action.description)
            for action in record.current_plan.actions
        ]
        changed: list[SupervisorAssignment] = []
        for assignment in supervisor.assignments:
            if assignment.state not in {
                SupervisorAssignmentState.INTERRUPTED,
                SupervisorAssignmentState.REDIRECTED,
            }:
                changed.append(assignment)
                continue
            transitioned = self._supervisor_runtime.transition(
                assignment,
                state=SupervisorAssignmentState.REDIRECTED,
                plan=record.current_plan,
                plan_actions=resolved_actions.get(assignment.task_id, []),
                decision_snapshot=record.graph_version,
                create_replacement_run=(
                    assignment.state is SupervisorAssignmentState.INTERRUPTED
                ),
            )
            instruction = self._redirect_instruction(
                transitioned,
                record.current_plan.id,
                actions,
            )
            changed.append(
                self._supervisor_runtime.transition(
                    transitioned,
                    state=SupervisorAssignmentState.REDIRECTED,
                    redirect_instruction=instruction,
                )
            )
        supervisor.assignments = changed
        if any(
            assignment.state is SupervisorAssignmentState.REDIRECTED
            for assignment in changed
        ):
            supervisor.state = SupervisorLifecycleState.REDIRECTING

    def _resume_supervisor(
        self,
        record: LiveWorkspaceRecord,
        *,
        decision_snapshot: str,
    ) -> None:
        supervisor = record.supervisor
        if supervisor is None:
            return
        supervisor.assignments = [
            (
                self._supervisor_runtime.transition(
                    assignment,
                    state=SupervisorAssignmentState.RESUMED,
                    decision_snapshot=decision_snapshot,
                )
                if assignment.state is SupervisorAssignmentState.REDIRECTED
                else assignment
            )
            for assignment in supervisor.assignments
        ]
        supervisor.state = SupervisorLifecycleState.RESUMED

    def _complete_supervisor(self, record: LiveWorkspaceRecord) -> None:
        supervisor = record.supervisor
        if supervisor is None:
            return
        supervisor.assignments = [
            (
                self._supervisor_runtime.transition(
                    assignment,
                    state=SupervisorAssignmentState.COMPLETED,
                    decision_snapshot=record.graph_version,
                )
                if assignment.state is SupervisorAssignmentState.RESUMED
                else assignment
            )
            for assignment in supervisor.assignments
        ]
        supervisor.state = SupervisorLifecycleState.COMPLETED

    def import_workspace(
        self, request: LiveWorkspaceImportRequest
    ) -> LiveWorkspaceView:
        with self._lock:
            graph_version = f"graph-v{request.graph_version}"
            record = LiveWorkspaceRecord(
                definition=request.model_copy(deep=True),
                context_id=f"live-{request.id}",
                graph_version=graph_version,
                baseline_proposal_instance_id=(
                    f"{request.id}:baseline:{uuid4().hex}"
                ),
                current_plan=request.plan.model_copy(deep=True),
                supervisor=self._supervisor_runtime.create_supervisor(
                    workspace_id=request.id,
                    supervisor_run_id=f"LIVE-{request.id.upper()}-RUN",
                    tasks=request.tasks,
                    plan=request.plan,
                    decision_snapshot=graph_version,
                ),
            )
            self._event(
                record,
                event_type="workspace.imported",
                detail=(
                    f"Imported {len(request.tasks)} tasks and plan {request.plan.id}; "
                    "the baseline remains a proposal until an authorized role approves it."
                ),
            )
            self._repository.create(record)
            return LiveWorkspaceView.from_record(record)

    def list(self) -> LiveWorkspaceList:
        with self._lock:
            return LiveWorkspaceList(
                workspaces=[
                    LiveWorkspaceView.from_record(record)
                    for record in self._repository.list()
                ]
            )

    def get(self, workspace_id: str) -> LiveWorkspaceView:
        with self._lock:
            return LiveWorkspaceView.from_record(self._repository.get(workspace_id))

    def preview_decision(
        self,
        workspace_id: str,
        decision_id: str,
    ) -> WorkspaceApprovalPreview:
        """Compute the exact scope-sensitive assignment partition without mutation."""

        with self._lock:
            record = self._repository.get(workspace_id)
            mutation = record.pending_mutation
            if (
                record.status is not LiveWorkspaceStatus.CHANGE_PROPOSED
                or mutation is None
                or mutation.decision.id != decision_id
            ):
                self._conflict("The requested Decision is not awaiting approval.")
            workspace = LiveWorkspaceView.from_record(record)
            pending = pending_from_workspace(workspace.model_dump(mode="json"))
            if pending is None:
                self._conflict("The requested Decision is not awaiting approval.")

            artifacts = {
                artifact.id: artifact
                for artifact in record.definition.graph_artifacts()
            }
            artifacts.update(
                {
                    approved.mutation.decision.id:
                        approved.mutation.decision
                    for approved in record.approved_mutations
                }
            )
            outgoing: dict[str, list[Edge]] = {}
            for edge in record.definition.graph_edges():
                if edge.kind in AUTHORITY_DOWNSTREAM_EDGE_KINDS:
                    outgoing.setdefault(edge.source_id, []).append(edge)
            for edges in outgoing.values():
                edges.sort(key=authority_edge_sort_key)

            paths: dict[str, tuple[str, ...]] = {
                mutation.supersedes_id: (
                    mutation.decision.id,
                    mutation.supersedes_id,
                )
            }
            queue: deque[str] = deque([mutation.supersedes_id])
            visited = {mutation.supersedes_id}
            while queue:
                current_id = queue.popleft()
                current_path = paths[current_id]
                for edge in outgoing.get(current_id, []):
                    child = artifacts.get(edge.target_id)
                    if child is None or not (
                        child.scopes & mutation.affected_scopes
                    ):
                        continue
                    paths.setdefault(
                        child.id,
                        (*current_path, child.id),
                    )
                    if child.id not in visited:
                        visited.add(child.id)
                        queue.append(child.id)

            interrupted: list[str] = []
            preserved: list[str] = []
            assignment_paths: dict[str, tuple[str, ...]] = {}
            assignments = (
                record.supervisor.assignments
                if record.supervisor is not None
                else []
            )
            for assignment in assignments:
                task = artifacts.get(assignment.task_id)
                path = paths.get(assignment.task_id)
                invalidated_scopes = (
                    task.invalidated_scopes | mutation.affected_scopes
                    if task is not None
                    else set()
                )
                fully_invalidated = bool(
                    task is not None
                    and task.scopes
                    and invalidated_scopes >= task.scopes
                    and path is not None
                )
                if fully_invalidated:
                    assert path is not None
                    interrupted.append(assignment.id)
                    assignment_paths[assignment.id] = path
                else:
                    preserved.append(assignment.id)

            interrupted_ids = tuple(sorted(interrupted))
            preserved_ids = tuple(sorted(preserved))
            return WorkspaceApprovalPreview(
                pending=pending,
                interrupted_assignment_ids=interrupted_ids,
                preserved_assignment_ids=preserved_ids,
                interrupted_count=len(interrupted_ids),
                preserved_count=len(preserved_ids),
                total_assignment_count=(
                    len(interrupted_ids) + len(preserved_ids)
                ),
                assignment_provenance_paths=assignment_paths,
            )

    def approve_baseline(
        self,
        workspace_id: str,
        request: WorkspaceApprovalRequest,
    ) -> LiveWorkspaceView:
        with self._lock:
            record = self._repository.get(workspace_id)
            if record.status is not LiveWorkspaceStatus.IMPORTED:
                self._conflict("The workspace baseline is not awaiting approval.")
            decision = record.definition.baseline_decision
            expected_fingerprint = stable_hash(decision)
            expected_instance_id = record.baseline_proposal_instance_id
            evidence = request.approval_evidence
            if (
                expected_instance_id is None
                or request.proposal_fingerprint != expected_fingerprint
                or request.proposal_instance_id != expected_instance_id
                or evidence is None
                or evidence.workspace_id != workspace_id
                or evidence.decision_id != decision.id
                or evidence.permission_id != request.actor_role
                or evidence.confirmed_proposal_fingerprint
                != expected_fingerprint
                or evidence.confirmed_proposal_instance_id
                != expected_instance_id
            ):
                self._conflict(
                    "The baseline approval is not bound to the current proposal."
                )
            self._ensure_context(record)
            state = self._transport.approve_baseline(record.context_id, request)
            record.baseline_approved = True
            record.baseline_approval_role = request.actor_role
            record.baseline_approval_evidence = evidence
            record.status = LiveWorkspaceStatus.BASELINE_APPROVED
            record.graph_version = state.graph_version
            self._event(
                record,
                event_type="baseline.approved",
                detail=(
                    f"{record.definition.baseline_decision.id} approved at "
                    f"{state.graph_version}."
                ),
                actor_role=request.actor_role,
                data={
                    "decision_id": decision.id,
                    "approver_user_id": evidence.approver_user_id,
                    "approval_channel": evidence.channel.value,
                    "approval_evidence_ref": evidence.evidence_ref,
                    "approved_at": evidence.approved_at.isoformat(),
                    "confirmed_proposal_fingerprint": expected_fingerprint,
                    "confirmed_proposal_instance_id": expected_instance_id,
                },
            )
            self._repository.save(record)
            return LiveWorkspaceView.from_record(record)

    def authorize(self, workspace_id: str) -> LiveWorkspaceView:
        with self._lock:
            record = self._repository.get(workspace_id)
            if record.status not in {
                LiveWorkspaceStatus.BASELINE_APPROVED,
                LiveWorkspaceStatus.AUTHORIZED,
            }:
                self._conflict("Approve the baseline before requesting authorization.")
            self._ensure_context(record)
            result = self._transport.authorize(
                record.context_id,
                AuthorizationRequest(
                    run_id=self._run_id(record),
                    task_id=record.definition.ticket.id,
                    plan=record.current_plan,
                ),
            )
            record.initial_authorization = result
            record.graph_version = result.graph_version
            if result.verdict is Verdict.ALLOW and result.grant is not None:
                record.status = LiveWorkspaceStatus.AUTHORIZED
                self._dispatch_supervisor(
                    record,
                    decision_snapshot=result.grant.payload.decision_snapshot,
                )
            self._event(
                record,
                event_type="authorization.evaluated",
                detail=f"Initial plan verdict: {result.verdict.value}.",
                data={"verdict": result.verdict.value},
            )
            self._repository.save(record)
            return LiveWorkspaceView.from_record(record)

    def propose_decision(
        self,
        workspace_id: str,
        request: WorkspaceProposalRequest,
    ) -> LiveWorkspaceView:
        with self._lock:
            record = self._repository.get(workspace_id)
            if record.status is not LiveWorkspaceStatus.AUTHORIZED:
                self._conflict(
                    "Obtain an initial authorization before proposing a decision change."
                )
            existing_ids = {
                record.definition.baseline_decision.id,
                *(item.mutation.decision.id for item in record.approved_mutations),
            }
            if request.decision.id in existing_ids:
                self._conflict("The proposed Decision ID is already present.")
            known_decisions = {
                record.definition.baseline_decision.id:
                    record.definition.baseline_decision,
                **{
                    item.mutation.decision.id: item.mutation.decision
                    for item in record.approved_mutations
                },
            }
            superseded = known_decisions.get(request.supersedes_id)
            if superseded is None:
                self._conflict(
                    "The proposed supersession target does not exist in this workspace."
                )
            if not request.affected_scopes <= superseded.scopes:
                self._conflict(
                    "The proposed change includes scopes absent from its supersession target."
                )
            record.pending_mutation = request.mutation()
            record.proposal_sequence += 1
            record.pending_proposal_instance_id = (
                f"{workspace_id}:proposal:{record.proposal_sequence}"
            )
            record.status = LiveWorkspaceStatus.CHANGE_PROPOSED
            self._event(
                record,
                event_type="decision.proposed",
                detail=(
                    f"{request.decision.id} was recorded as a proposal; "
                    "the graph has not changed."
                ),
                data={
                    "decision_id": request.decision.id,
                    "proposal_instance_id": record.pending_proposal_instance_id,
                    "proposal_fingerprint": stable_hash(record.pending_mutation),
                    "permission_id": request.decision.authority_role,
                },
            )
            self._repository.save(record)
            return LiveWorkspaceView.from_record(record)

    def cancel_pending_decision(self, workspace_id: str) -> LiveWorkspaceView:
        with self._lock:
            record = self._repository.get(workspace_id)
            mutation = record.pending_mutation
            if record.decision_approval_intent is not None:
                self._conflict(
                    "Approval has started; retry it to converge the exact proposal."
                )
            if (
                record.status is not LiveWorkspaceStatus.CHANGE_PROPOSED
                or mutation is None
            ):
                self._conflict("There is no pending Decision proposal to cancel.")
            record.pending_mutation = None
            record.pending_proposal_instance_id = None
            record.status = LiveWorkspaceStatus.AUTHORIZED
            self._event(
                record,
                event_type="decision.proposal-canceled",
                detail=(
                    f"Canceled pending proposal {mutation.decision.id}; "
                    "the authority graph was unchanged."
                ),
                data={"decision_id": mutation.decision.id},
            )
            self._repository.save(record)
            return LiveWorkspaceView.from_record(record)

    def record_approval_rejection(
        self,
        workspace_id: str,
        *,
        decision_id: str,
        disposition: str,
        approver_user_id: str,
        permission_id: str,
        approval_channel: str,
        approval_evidence_ref: str,
        confirmed_proposal_fingerprint: str | None,
        confirmed_proposal_instance_id: str | None,
        detail: str,
    ) -> LiveWorkspaceView:
        """Persist a token-free rejected approval verdict without changing authority."""

        with self._lock:
            record = self._repository.get(workspace_id)
            pending = record.pending_mutation
            expected_permission_id: str | None = None
            proposal_fingerprint: str | None
            proposal_instance_id: str | None
            if pending is not None and pending.decision.id == decision_id:
                proposal_fingerprint = stable_hash(pending)
                proposal_instance_id = record.pending_proposal_instance_id
                if (
                    confirmed_proposal_fingerprint is None
                    or confirmed_proposal_instance_id is None
                    or (
                        confirmed_proposal_fingerprint == proposal_fingerprint
                        and confirmed_proposal_instance_id
                        == proposal_instance_id
                    )
                ):
                    expected_permission_id = pending.decision.authority_role
            elif decision_id == record.definition.baseline_decision.id:
                proposal_fingerprint = stable_hash(
                    record.definition.baseline_decision
                )
                proposal_instance_id = record.baseline_proposal_instance_id
                expected_permission_id = (
                    record.definition.baseline_decision.authority_role
                )
            else:
                proposal_fingerprint = confirmed_proposal_fingerprint
                proposal_instance_id = confirmed_proposal_instance_id
            if expected_permission_id is None:
                for event in reversed(record.history):
                    data = event.data
                    if (
                        event.event_type == "decision.proposed"
                        and data.get("decision_id") == decision_id
                        and data.get("proposal_fingerprint")
                        == confirmed_proposal_fingerprint
                        and data.get("proposal_instance_id")
                        == confirmed_proposal_instance_id
                        and isinstance(data.get("permission_id"), str)
                    ):
                        expected_permission_id = str(data["permission_id"])
                        break
            if expected_permission_id is None:
                for approved in reversed(record.approved_mutations):
                    evidence = approved.approval_evidence
                    if (
                        approved.mutation.decision.id == decision_id
                        and evidence is not None
                        and evidence.confirmed_proposal_fingerprint
                        == confirmed_proposal_fingerprint
                        and evidence.confirmed_proposal_instance_id
                        == confirmed_proposal_instance_id
                    ):
                        expected_permission_id = approved.actor_role
                        break
            if (
                expected_permission_id is None
                or permission_id != expected_permission_id
            ):
                self._conflict(
                    "Rejected approval evidence is not bound to an exact "
                    "proposal permission."
                )
            rejected_at = utc_now()
            self._event(
                record,
                event_type="decision.approval-rejected",
                detail=detail,
                actor_role=permission_id,
                data={
                    "decision_id": decision_id,
                    "disposition": disposition,
                    "approver_user_id": approver_user_id,
                    "permission_id": permission_id,
                    "approval_channel": approval_channel,
                    "approval_evidence_ref": approval_evidence_ref,
                    "proposal_fingerprint": proposal_fingerprint,
                    "proposal_instance_id": proposal_instance_id,
                    "confirmed_proposal_fingerprint": (
                        confirmed_proposal_fingerprint
                    ),
                    "confirmed_proposal_instance_id": (
                        confirmed_proposal_instance_id
                    ),
                    "rejected_at": rejected_at.isoformat(),
                },
            )
            self._repository.save(record)
            return LiveWorkspaceView.from_record(record)

    def is_slack_authority_user_bound(
        self,
        workspace_id: str,
        *,
        authority_user_id: str,
    ) -> bool:
        """Check a private Slack→Hexclave binding without exposing the mapping."""

        with self._lock:
            record = self._repository.get(workspace_id)
            binding = record.definition.slack_binding
            return binding is not None and any(
                identity.hexclave_user_id == authority_user_id
                for identity in binding.user_identities
            )

    def approve_decision(
        self,
        workspace_id: str,
        decision_id: str,
        request: WorkspaceApprovalRequest,
    ) -> LiveWorkspaceView:
        with self._lock:
            record = self._repository.get(workspace_id)
            mutation = record.pending_mutation
            if (
                record.status is not LiveWorkspaceStatus.CHANGE_PROPOSED
                or mutation is None
                or mutation.decision.id != decision_id
            ):
                if self._completed_approval_matches(
                    record,
                    decision_id=decision_id,
                    request=request,
                ):
                    return LiveWorkspaceView.from_record(record)
                self._conflict("The requested Decision is not awaiting approval.")
            expected_fingerprint = stable_hash(mutation)
            evidence = request.approval_evidence
            if (
                request.proposal_fingerprint != expected_fingerprint
                or request.proposal_instance_id
                != record.pending_proposal_instance_id
                or evidence is None
                or evidence.workspace_id != workspace_id
                or evidence.decision_id != decision_id
                or evidence.permission_id != request.actor_role
                or evidence.confirmed_proposal_fingerprint
                != expected_fingerprint
                or evidence.confirmed_proposal_instance_id
                != record.pending_proposal_instance_id
            ):
                self._conflict(
                    "Approval is not bound to the exact pending proposal."
                )

            intent = record.decision_approval_intent
            if intent is None:
                self._ensure_context(record)
                intent = WorkspaceDecisionApprovalIntent(
                    mutation=mutation.model_copy(deep=True),
                    actor_role=request.actor_role,
                    proposal_fingerprint=expected_fingerprint,
                    proposal_instance_id=record.pending_proposal_instance_id,
                    approval_evidence=evidence.model_copy(deep=True),
                    base_graph_version=record.graph_version,
                )
                record.decision_approval_intent = intent
                # This write-ahead record precedes every remote mutation attempt.
                self._repository.save(record)
            elif (
                intent.mutation != mutation
                or intent.actor_role != request.actor_role
                or intent.proposal_fingerprint != expected_fingerprint
                or intent.proposal_instance_id
                != record.pending_proposal_instance_id
                or intent.approval_evidence.workspace_id != workspace_id
                or intent.approval_evidence.decision_id != decision_id
                or not self._same_approval_attempt(
                    intent.approval_evidence,
                    evidence,
                )
            ):
                self._conflict(
                    "Approval retry does not match the durable approval intent."
                )

            try:
                result = self._recover_intent_mutation(record, intent)
            except LiveWorkspaceStateConflict:
                record.decision_approval_intent = None
                self._repository.save(record)
                raise
            except ApiError as exc:
                if not exc.retryable:
                    # The authority rejected before applying; leave the proposal
                    # pending so a rejection audit or cancellation can follow.
                    record.decision_approval_intent = None
                    self._repository.save(record)
                raise
            report = result.report
            if report is None:
                raise RuntimeError(
                    "Authority applied a mutation without invalidation evidence."
                )
            intent = self._updated_intent(
                intent,
                mutation_result=result,
            )
            record.decision_approval_intent = intent
            record.graph_version = result.graph_version
            record.invalidation_report = report
            record.conflict_authorization = None
            self._apply_supervisor_invalidation(record)
            # Authorization cannot run until evidence and interrupt state are durable.
            self._repository.save(record)

            conflict = intent.authorization_result
            if conflict is None:
                conflict = self._transport.authorize(
                    record.context_id,
                    AuthorizationRequest(
                        run_id=self._run_id(record),
                        task_id=record.definition.ticket.id,
                        plan=record.current_plan,
                    ),
                )
                if (
                    conflict.graph_version != result.graph_version
                    or conflict.task_id != record.definition.ticket.id
                ):
                    raise RuntimeError(
                        "Authority evaluated a different approval snapshot."
                    )
                intent = self._updated_intent(
                    intent,
                    authorization_result=conflict,
                )
                record.decision_approval_intent = intent
                # Preserve the exact verdict/grant before final visible state.
                self._repository.save(record)

            record.approved_mutations.append(
                ApprovedWorkspaceMutation(
                    mutation=mutation.model_copy(deep=True),
                    actor_role=intent.actor_role,
                    approval_evidence=intent.approval_evidence,
                )
            )
            record.pending_mutation = None
            record.pending_proposal_instance_id = None
            record.graph_version = result.graph_version
            record.invalidation_report = report
            record.conflict_authorization = conflict
            record.status = LiveWorkspaceStatus.CHANGE_APPLIED
            record.decision_approval_intent = None
            evidence = intent.approval_evidence
            self._event(
                record,
                event_type="decision.approved",
                detail=(
                    f"{decision_id} advanced the graph to {result.graph_version}; "
                    f"the current plan verdict is {conflict.verdict.value}."
                ),
                actor_role=intent.actor_role,
                data={
                    "decision_id": decision_id,
                    "verdict": conflict.verdict.value,
                    "approver_user_id": evidence.approver_user_id,
                    "approval_channel": evidence.channel.value,
                    "approval_evidence_ref": evidence.evidence_ref,
                    "approved_at": evidence.approved_at.isoformat(),
                    "confirmed_proposal_fingerprint": expected_fingerprint,
                    "confirmed_proposal_instance_id": (
                        evidence.confirmed_proposal_instance_id
                    ),
                    "invalidated_task_ids": report.invalidated_task_ids,
                    "preserved_task_ids": report.preserved_task_ids,
                },
            )
            self._repository.save(record)
            return LiveWorkspaceView.from_record(record)

    def verify_initial_grant(self, workspace_id: str) -> LiveWorkspaceView:
        with self._lock:
            record = self._repository.get(workspace_id)
            if record.status not in {
                LiveWorkspaceStatus.CHANGE_APPLIED,
                LiveWorkspaceStatus.INITIAL_GRANT_REJECTED,
            }:
                self._conflict("Apply an approved decision change before verification.")
            if (
                record.status is LiveWorkspaceStatus.INITIAL_GRANT_REJECTED
                and self._has_verified_stale_grant(record)
            ):
                return LiveWorkspaceView.from_record(record)
            authorization = record.initial_authorization
            if authorization is None or authorization.grant is None:
                self._conflict("The workspace has no initial ALLOW grant to verify.")
            self._ensure_context(record)
            execution = self._transport.execute(
                context_id=record.context_id,
                token=authorization.grant.token,
                run_id=self._run_id(record),
                task_id=record.definition.ticket.id,
                plan=record.definition.plan,
            )
            record.initial_verification = execution
            if (
                not execution.applied
                and execution.verification_code is VerificationCode.STALE_SNAPSHOT
            ):
                record.status = LiveWorkspaceStatus.INITIAL_GRANT_REJECTED
                self._enforce_supervisor_interrupts(record)
            else:
                record.status = LiveWorkspaceStatus.CHANGE_APPLIED
            self._event(
                record,
                event_type="initial-grant.verified",
                detail=(
                    f"Executor verification returned "
                    f"{execution.verification_code.value}."
                ),
                data={
                    "applied": execution.applied,
                    "verification_code": execution.verification_code.value,
                },
            )
            self._repository.save(record)
            return LiveWorkspaceView.from_record(record)

    def update_plan(
        self,
        workspace_id: str,
        request: WorkspacePlanUpdateRequest,
    ) -> LiveWorkspaceView:
        with self._lock:
            record = self._repository.get(workspace_id)
            if record.status not in {
                LiveWorkspaceStatus.INITIAL_GRANT_REJECTED,
                LiveWorkspaceStatus.PLAN_UPDATED,
            }:
                self._conflict(
                    "Verify the initial grant as STALE_SNAPSHOT before updating the plan."
                )
            if not self._has_verified_stale_grant(record):
                self._conflict(
                    "A verified STALE_SNAPSHOT result is required before updating the plan."
                )
            if request.plan.ticket_id != record.definition.ticket.id:
                self._conflict("The corrected plan is bound to a different ticket.")
            record.current_plan = request.plan.model_copy(deep=True)
            record.replacement_authorization = None
            record.replacement_verification = None
            record.status = LiveWorkspaceStatus.PLAN_UPDATED
            self._redirect_supervisor(record)
            self._event(
                record,
                event_type="plan.updated",
                detail=f"Corrected plan {request.plan.id} is ready for authority review.",
                data={"plan_id": request.plan.id},
            )
            self._repository.save(record)
            return LiveWorkspaceView.from_record(record)

    def reauthorize(self, workspace_id: str) -> LiveWorkspaceView:
        with self._lock:
            record = self._repository.get(workspace_id)
            if (
                record.status is LiveWorkspaceStatus.REAUTHORIZED
                and record.replacement_authorization is not None
            ):
                # Keep retries idempotent. Rotating the authorization ID here would
                # bypass Callwright's at-most-once attempt record for this plan.
                return LiveWorkspaceView.from_record(record)
            if record.status is not LiveWorkspaceStatus.PLAN_UPDATED:
                self._conflict("Submit a corrected plan before reauthorization.")
            if not self._has_verified_stale_grant(record):
                self._conflict(
                    "A verified STALE_SNAPSHOT result is required before reauthorization."
                )
            self._ensure_context(record)
            result = self._transport.authorize(
                record.context_id,
                AuthorizationRequest(
                    run_id=self._run_id(record),
                    task_id=record.definition.ticket.id,
                    plan=record.current_plan,
                ),
            )
            record.replacement_authorization = result
            if result.verdict is Verdict.ALLOW and result.grant is not None:
                record.status = LiveWorkspaceStatus.REAUTHORIZED
                self._resume_supervisor(
                    record,
                    decision_snapshot=result.grant.payload.decision_snapshot,
                )
            else:
                record.status = LiveWorkspaceStatus.PLAN_UPDATED
            self._event(
                record,
                event_type="plan.reauthorized",
                detail=f"Corrected plan verdict: {result.verdict.value}.",
                data={"verdict": result.verdict.value},
            )
            self._repository.save(record)
            return LiveWorkspaceView.from_record(record)

    def verify_replacement_grant(self, workspace_id: str) -> LiveWorkspaceView:
        with self._lock:
            record = self._repository.get(workspace_id)
            if record.status not in {
                LiveWorkspaceStatus.REAUTHORIZED,
                LiveWorkspaceStatus.COMPLETE,
            }:
                self._conflict("Obtain a replacement ALLOW grant before verification.")
            if not self._has_verified_stale_grant(record):
                self._conflict(
                    "A verified STALE_SNAPSHOT result is required before completion."
                )
            authorization = record.replacement_authorization
            if authorization is None or authorization.grant is None:
                self._conflict("The workspace has no replacement grant to verify.")
            self._ensure_context(record)
            execution = self._transport.execute(
                context_id=record.context_id,
                token=authorization.grant.token,
                run_id=self._run_id(record),
                task_id=record.definition.ticket.id,
                plan=record.current_plan,
            )
            record.replacement_verification = execution
            if (
                execution.applied
                and execution.verification_code is VerificationCode.VALID
            ):
                record.status = LiveWorkspaceStatus.COMPLETE
                self._complete_supervisor(record)
            self._event(
                record,
                event_type="replacement-grant.verified",
                detail=(
                    f"Executor verification returned "
                    f"{execution.verification_code.value}."
                ),
                data={
                    "applied": execution.applied,
                    "verification_code": execution.verification_code.value,
                },
            )
            self._repository.save(record)
            return LiveWorkspaceView.from_record(record)
