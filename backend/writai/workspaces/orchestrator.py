from __future__ import annotations

from collections import deque
from threading import RLock
from typing import NoReturn
from uuid import uuid4

from writai.domain import (
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    AuthorizationRequest,
    DecisionMutation,
    Edge,
    EdgeKind,
    MutationResult,
    Verdict,
    VerificationCode,
    utc_now,
)
from writai.hashing import stable_hash
from writai.intake.approval import ApprovalEvidence, pending_from_workspace
from writai.provenance import (
    AUTHORITY_DOWNSTREAM_EDGE_KINDS,
    authority_edge_sort_key,
)
from writai.services.support import ApiError
from writai.workspaces.authority_contexts import (
    DynamicAuthorityContextCreateRequest,
    DynamicAuthorityContextState,
    DynamicMutationApprovalRequest,
)
from writai.workspaces.models import (
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
from writai.workspaces.repository import LiveWorkspaceRepository
from writai.workspaces.supervisor import (
    FixtureSupervisorRuntime,
    SupervisorAssignment,
    SupervisorAssignmentState,
    SupervisorExecutionMode,
    SupervisorLifecycleState,
    SupervisorRuntimeAdapter,
    WorkspaceSupervisor,
    resolve_plan_actions_for_assignments,
)
from writai.workspaces.transport import (
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
    def _pending_mutation(
        record: LiveWorkspaceRecord,
        decision_id: str,
    ) -> DecisionMutation | None:
        """The proposal awaiting approval for ``decision_id``, if there is one."""

        mutation = record.pending_mutation
        if (
            record.status is not LiveWorkspaceStatus.CHANGE_PROPOSED
            or mutation is None
            or mutation.decision.id != decision_id
        ):
            return None
        return mutation

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

    @staticmethod
    def _context_lineage(record: LiveWorkspaceRecord) -> tuple[object, ...]:
        """The record fields ``_ensure_context`` rehydrates the authority context from."""

        return (
            record.definition,
            record.graph_version,
            record.baseline_approved,
            record.baseline_approval_role,
            record.approved_mutations,
        )

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

    @staticmethod
    def _supervisor_to_transition(
        record: LiveWorkspaceRecord,
    ) -> WorkspaceSupervisor | None:
        """A copy of ``record``'s supervisor for the runtime to transition.

        The supervisor helpers below call the runtime adapter, which is a
        network boundary once the adapter is a live one, so they run before
        the store transaction on the record as read and never mutate it. The
        callable inside ``mutate()`` applies what they return through
        ``_apply_supervisor_transitions``.
        """

        if record.supervisor is None:
            return None
        return record.supervisor.model_copy(deep=True)

    def _apply_supervisor_transitions(
        self,
        current: LiveWorkspaceRecord,
        *,
        read: LiveWorkspaceRecord,
        transitioned: WorkspaceSupervisor | None,
    ) -> None:
        """Put runtime transitions computed from ``read`` onto the fresh ``current``.

        The transitions were computed from ``read``'s assignments and graph
        version. If either moved between that read and this transaction,
        applying them would overwrite the write that moved it, so this
        conflicts instead and the caller re-reads and recomputes.
        """

        if (
            current.supervisor != read.supervisor
            or current.graph_version != read.graph_version
        ):
            self._conflict(
                "The supervisor changed while the runtime transition was in flight."
            )
        current.supervisor = transitioned

    def _dispatch_supervisor(
        self,
        record: LiveWorkspaceRecord,
        *,
        decision_snapshot: str,
    ) -> WorkspaceSupervisor | None:
        supervisor = self._supervisor_to_transition(record)
        if supervisor is None:
            return None
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
        return supervisor

    def _apply_supervisor_invalidation(
        self,
        record: LiveWorkspaceRecord,
    ) -> WorkspaceSupervisor | None:
        supervisor = self._supervisor_to_transition(record)
        report = record.invalidation_report
        if supervisor is None or report is None:
            return supervisor
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
                    f"{scopes} at {report.graph_version}; writ.ai invalidated "
                    f"{assignment.task_id} through the recorded provenance path."
                )
                redirect_instruction = (
                    f"Stop {assignment.task_title}. Return control to the writ.ai "
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
        return supervisor

    def _enforce_supervisor_interrupts(
        self,
        record: LiveWorkspaceRecord,
    ) -> WorkspaceSupervisor | None:
        supervisor = self._supervisor_to_transition(record)
        if supervisor is None:
            return None
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
        return supervisor

    def _redirect_supervisor(
        self,
        record: LiveWorkspaceRecord,
    ) -> WorkspaceSupervisor | None:
        supervisor = self._supervisor_to_transition(record)
        if supervisor is None:
            return None
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
        return supervisor

    def _resume_supervisor(
        self,
        record: LiveWorkspaceRecord,
        *,
        decision_snapshot: str,
    ) -> WorkspaceSupervisor | None:
        supervisor = self._supervisor_to_transition(record)
        if supervisor is None:
            return None
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
        return supervisor

    def _complete_supervisor(
        self,
        record: LiveWorkspaceRecord,
    ) -> WorkspaceSupervisor | None:
        supervisor = self._supervisor_to_transition(record)
        if supervisor is None:
            return None
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
        return supervisor

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

            def require_bound_proposal(
                record: LiveWorkspaceRecord,
            ) -> tuple[Artifact, str, str, ApprovalEvidence]:
                """Check the request against ``record``'s own proposal binding.

                The fingerprint and instance id are read from the record that
                is passed in, never captured from an earlier read, so the
                re-check inside the store transaction sees the fresh record.
                """

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
                return decision, expected_fingerprint, expected_instance_id, evidence

            record = self._repository.get(workspace_id)
            require_bound_proposal(record)
            self._ensure_context(record)
            # The authority call stays outside the store transaction so no
            # writer waits on the HTTP round trip. The binding is re-checked
            # on the fresh read below: a second approver reads
            # BASELINE_APPROVED and conflicts instead of overwriting this
            # approval, and a proposal replaced while the call was in flight
            # no longer matches the request.
            state = self._transport.approve_baseline(record.context_id, request)

            def approve(current: LiveWorkspaceRecord) -> LiveWorkspaceRecord:
                decision, expected_fingerprint, expected_instance_id, evidence = (
                    require_bound_proposal(current)
                )
                current.baseline_approved = True
                current.baseline_approval_role = request.actor_role
                current.baseline_approval_evidence = evidence
                current.status = LiveWorkspaceStatus.BASELINE_APPROVED
                current.graph_version = state.graph_version
                self._event(
                    current,
                    event_type="baseline.approved",
                    detail=(
                        f"{decision.id} approved at {state.graph_version}."
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
                return current

            record = self._repository.mutate(workspace_id, approve)
            return LiveWorkspaceView.from_record(record)

    def authorize(self, workspace_id: str) -> LiveWorkspaceView:
        with self._lock:

            def require_authorizable(record: LiveWorkspaceRecord) -> None:
                if record.status not in {
                    LiveWorkspaceStatus.BASELINE_APPROVED,
                    LiveWorkspaceStatus.AUTHORIZED,
                }:
                    self._conflict(
                        "Approve the baseline before requesting authorization."
                    )

            record = self._repository.get(workspace_id)
            require_authorizable(record)
            self._ensure_context(record)
            # The authority call stays outside the store transaction; the
            # verdict, grant and audit event are then applied to a fresh read
            # so a write landing in between is kept rather than overwritten.
            result = self._transport.authorize(
                record.context_id,
                AuthorizationRequest(
                    run_id=self._run_id(record),
                    task_id=record.definition.ticket.id,
                    plan=record.current_plan,
                ),
            )
            # The supervisor dispatch is a runtime call, so it stays outside
            # the transaction too: computed from the record as read, applied
            # only if the fresh read still carries that supervisor.
            grant = result.grant if result.verdict is Verdict.ALLOW else None
            dispatched = (
                self._dispatch_supervisor(
                    record,
                    decision_snapshot=grant.payload.decision_snapshot,
                )
                if grant is not None
                else None
            )

            def record_authorization(
                current: LiveWorkspaceRecord,
            ) -> LiveWorkspaceRecord:
                require_authorizable(current)
                if grant is not None:
                    self._apply_supervisor_transitions(
                        current, read=record, transitioned=dispatched
                    )
                    current.status = LiveWorkspaceStatus.AUTHORIZED
                current.initial_authorization = result
                current.graph_version = result.graph_version
                self._event(
                    current,
                    event_type="authorization.evaluated",
                    detail=f"Initial plan verdict: {result.verdict.value}.",
                    data={"verdict": result.verdict.value},
                )
                return current

            record = self._repository.mutate(workspace_id, record_authorization)
            return LiveWorkspaceView.from_record(record)

    def propose_decision(
        self,
        workspace_id: str,
        request: WorkspaceProposalRequest,
    ) -> LiveWorkspaceView:
        with self._lock:

            def propose(record: LiveWorkspaceRecord) -> LiveWorkspaceRecord:
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
                return record

            # Status check, proposal sequence and audit event share one
            # repository mutation: the proposal_sequence increment is what
            # makes proposal instance ids unique, so it must not be lost.
            record = self._repository.mutate(workspace_id, propose)
            return LiveWorkspaceView.from_record(record)

    def cancel_pending_decision(self, workspace_id: str) -> LiveWorkspaceView:
        with self._lock:

            def cancel(record: LiveWorkspaceRecord) -> LiveWorkspaceRecord:
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
                return record

            # The intent check and the cancellation share one mutation, so an
            # approval intent written in between is seen, not overwritten.
            record = self._repository.mutate(workspace_id, cancel)
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

            def record_rejection(
                record: LiveWorkspaceRecord,
            ) -> LiveWorkspaceRecord:
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
                return record

            # A rejection is pure audit: the only thing this write carries is
            # the event, so a plain save would trade it for whatever landed
            # in between. The permission binding and the event share one
            # mutation.
            record = self._repository.mutate(workspace_id, record_rejection)
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
            current = self._repository.get(workspace_id)
            if self._pending_mutation(current, decision_id) is None:
                if self._completed_approval_matches(
                    current,
                    decision_id=decision_id,
                    request=request,
                ):
                    return LiveWorkspaceView.from_record(current)
                self._conflict("The requested Decision is not awaiting approval.")

            def require_bound_proposal(
                record: LiveWorkspaceRecord,
            ) -> tuple[DecisionMutation, str, str, ApprovalEvidence]:
                """Check the request against ``record``'s own pending proposal.

                Read from the record passed in, never from an earlier read,
                so the re-check inside the store transaction sees the fresh
                record.
                """

                mutation = self._pending_mutation(record, decision_id)
                if mutation is None:
                    self._conflict("The requested Decision is not awaiting approval.")
                expected_fingerprint = stable_hash(mutation)
                expected_instance_id = record.pending_proposal_instance_id
                evidence = request.approval_evidence
                if (
                    expected_instance_id is None
                    or request.proposal_fingerprint != expected_fingerprint
                    or request.proposal_instance_id != expected_instance_id
                    or evidence is None
                    or evidence.workspace_id != workspace_id
                    or evidence.decision_id != decision_id
                    or evidence.permission_id != request.actor_role
                    or evidence.confirmed_proposal_fingerprint
                    != expected_fingerprint
                    or evidence.confirmed_proposal_instance_id
                    != expected_instance_id
                ):
                    self._conflict(
                        "Approval is not bound to the exact pending proposal."
                    )
                return mutation, expected_fingerprint, expected_instance_id, evidence

            require_bound_proposal(current)
            # On a first attempt the authority context is rehydrated here,
            # outside the store transaction, from the record as read. A retry
            # skips it: the context may already hold the applied mutation,
            # and _recover_intent_mutation is what reconciles that. The intent
            # write below requires the fresh read to carry the lineage the
            # context was rehydrated for.
            rehydrated_for: LiveWorkspaceRecord | None = None
            if current.decision_approval_intent is None:
                self._ensure_context(current)
                rehydrated_for = current

            def record_intent(record: LiveWorkspaceRecord) -> LiveWorkspaceRecord:
                mutation, expected_fingerprint, expected_instance_id, evidence = (
                    require_bound_proposal(record)
                )
                intent = record.decision_approval_intent
                if intent is None:
                    if rehydrated_for is None or self._context_lineage(
                        record
                    ) != self._context_lineage(rehydrated_for):
                        self._conflict(
                            "The authority lineage changed while the approval "
                            "was being recorded."
                        )
                    record.decision_approval_intent = WorkspaceDecisionApprovalIntent(
                        mutation=mutation.model_copy(deep=True),
                        actor_role=request.actor_role,
                        proposal_fingerprint=expected_fingerprint,
                        proposal_instance_id=expected_instance_id,
                        approval_evidence=evidence.model_copy(deep=True),
                        base_graph_version=record.graph_version,
                    )
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
                return record

            # This write-ahead record precedes every remote mutation attempt. It
            # is validated against and written with the same read of the pending
            # proposal, so no write landing in between can drop the intent.
            record = self._repository.mutate(workspace_id, record_intent)
            intent = record.decision_approval_intent
            if intent is None:
                raise RuntimeError("The durable approval intent was not recorded.")
            mutation = intent.mutation
            expected_fingerprint = intent.proposal_fingerprint
            expected_instance_id = intent.proposal_instance_id

            # Every later write re-reads the record inside the store transaction
            # and requires the intent it is advancing to still be the one
            # written above; the transport calls between them stay outside.
            def holds_intent(current: LiveWorkspaceRecord) -> bool:
                held = current.decision_approval_intent
                return (
                    held is not None
                    and held.proposal_fingerprint == expected_fingerprint
                    and held.proposal_instance_id == expected_instance_id
                )

            def require_intent(current: LiveWorkspaceRecord) -> None:
                if not holds_intent(current):
                    self._conflict(
                        "The durable approval intent changed while the "
                        "approval was in flight."
                    )

            def clear_intent(current: LiveWorkspaceRecord) -> LiveWorkspaceRecord:
                # Only this approval's own intent is cleared; one another
                # writer replaced or removed in between is left as found.
                if holds_intent(current):
                    current.decision_approval_intent = None
                return current

            try:
                result = self._recover_intent_mutation(record, intent)
            except LiveWorkspaceStateConflict:
                self._repository.mutate(workspace_id, clear_intent)
                raise
            except ApiError as exc:
                if not exc.retryable:
                    # The authority rejected before applying; leave the proposal
                    # pending so a rejection audit or cancellation can follow.
                    self._repository.mutate(workspace_id, clear_intent)
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
            # The supervisor interrupts are runtime calls, so they stay outside
            # the transaction: computed from the record as read with the
            # report attached, applied only if the fresh read still carries
            # that supervisor.
            interrupted = record.model_copy(deep=True)
            interrupted.invalidation_report = report
            interrupted_supervisor = self._apply_supervisor_invalidation(interrupted)

            def record_mutation(current: LiveWorkspaceRecord) -> LiveWorkspaceRecord:
                require_intent(current)
                self._apply_supervisor_transitions(
                    current, read=record, transitioned=interrupted_supervisor
                )
                current.decision_approval_intent = intent
                current.graph_version = result.graph_version
                current.invalidation_report = report
                current.conflict_authorization = None
                return current

            # Authorization cannot run until evidence and interrupt state are durable.
            record = self._repository.mutate(workspace_id, record_mutation)

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
                authorized_intent = intent

                def record_authorization(
                    current: LiveWorkspaceRecord,
                ) -> LiveWorkspaceRecord:
                    require_intent(current)
                    current.decision_approval_intent = authorized_intent
                    return current

                # Preserve the exact verdict/grant before final visible state.
                record = self._repository.mutate(workspace_id, record_authorization)

            def complete(current: LiveWorkspaceRecord) -> LiveWorkspaceRecord:
                require_intent(current)
                current.approved_mutations.append(
                    ApprovedWorkspaceMutation(
                        mutation=mutation.model_copy(deep=True),
                        actor_role=intent.actor_role,
                        approval_evidence=intent.approval_evidence,
                    )
                )
                current.pending_mutation = None
                current.pending_proposal_instance_id = None
                current.graph_version = result.graph_version
                current.invalidation_report = report
                current.conflict_authorization = conflict
                current.status = LiveWorkspaceStatus.CHANGE_APPLIED
                current.decision_approval_intent = None
                evidence = intent.approval_evidence
                self._event(
                    current,
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
                return current

            record = self._repository.mutate(workspace_id, complete)
            return LiveWorkspaceView.from_record(record)

    def verify_initial_grant(self, workspace_id: str) -> LiveWorkspaceView:
        with self._lock:

            def already_verified(record: LiveWorkspaceRecord) -> bool:
                if record.status not in {
                    LiveWorkspaceStatus.CHANGE_APPLIED,
                    LiveWorkspaceStatus.INITIAL_GRANT_REJECTED,
                }:
                    self._conflict(
                        "Apply an approved decision change before verification."
                    )
                return (
                    record.status is LiveWorkspaceStatus.INITIAL_GRANT_REJECTED
                    and self._has_verified_stale_grant(record)
                )

            record = self._repository.get(workspace_id)
            if already_verified(record):
                return LiveWorkspaceView.from_record(record)
            authorization = record.initial_authorization
            if authorization is None or authorization.grant is None:
                self._conflict("The workspace has no initial ALLOW grant to verify.")
            self._ensure_context(record)
            # The executor call stays outside the store transaction; its result
            # is applied to a fresh read. Nothing reachable from these statuses
            # replaces the initial grant, so the status re-check is sufficient.
            execution = self._transport.execute(
                context_id=record.context_id,
                token=authorization.grant.token,
                run_id=self._run_id(record),
                task_id=record.definition.ticket.id,
                plan=record.definition.plan,
            )
            # Enforcing the interrupts is a runtime call, so it stays outside
            # the transaction: computed from the record as read, applied only
            # if the fresh read still carries that supervisor.
            stale = (
                not execution.applied
                and execution.verification_code is VerificationCode.STALE_SNAPSHOT
            )
            enforced = self._enforce_supervisor_interrupts(record) if stale else None

            def record_verification(
                current: LiveWorkspaceRecord,
            ) -> LiveWorkspaceRecord:
                if already_verified(current):
                    # Another writer verified the stale grant in between; its
                    # enforced interrupts are the ones to keep.
                    return current
                if stale:
                    self._apply_supervisor_transitions(
                        current, read=record, transitioned=enforced
                    )
                    current.status = LiveWorkspaceStatus.INITIAL_GRANT_REJECTED
                else:
                    current.status = LiveWorkspaceStatus.CHANGE_APPLIED
                current.initial_verification = execution
                self._event(
                    current,
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
                return current

            record = self._repository.mutate(workspace_id, record_verification)
            return LiveWorkspaceView.from_record(record)

    def update_plan(
        self,
        workspace_id: str,
        request: WorkspacePlanUpdateRequest,
    ) -> LiveWorkspaceView:
        with self._lock:

            def require_updatable(record: LiveWorkspaceRecord) -> None:
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

            record = self._repository.get(workspace_id)
            require_updatable(record)
            # The redirect transitions are runtime calls, so they stay outside
            # the transaction: computed from the record as read under the
            # corrected plan, applied only if the fresh read still carries the
            # assignments they were computed from. A redirect the hook
            # delivers in between conflicts here and the caller retries from
            # the delivered state rather than overwriting it.
            redirected = record.model_copy(deep=True)
            redirected.current_plan = request.plan.model_copy(deep=True)
            redirected_supervisor = self._redirect_supervisor(redirected)

            def update(current: LiveWorkspaceRecord) -> LiveWorkspaceRecord:
                require_updatable(current)
                self._apply_supervisor_transitions(
                    current, read=record, transitioned=redirected_supervisor
                )
                current.current_plan = request.plan.model_copy(deep=True)
                current.replacement_authorization = None
                current.replacement_verification = None
                current.status = LiveWorkspaceStatus.PLAN_UPDATED
                self._event(
                    current,
                    event_type="plan.updated",
                    detail=f"Corrected plan {request.plan.id} is ready for authority review.",
                    data={"plan_id": request.plan.id},
                )
                return current

            record = self._repository.mutate(workspace_id, update)
            return LiveWorkspaceView.from_record(record)

    def reauthorize(self, workspace_id: str) -> LiveWorkspaceView:
        with self._lock:

            def already_reauthorized(record: LiveWorkspaceRecord) -> bool:
                if (
                    record.status is LiveWorkspaceStatus.REAUTHORIZED
                    and record.replacement_authorization is not None
                ):
                    # Keep retries idempotent. Rotating the authorization ID
                    # here would bypass Callwright's at-most-once attempt
                    # record for this plan.
                    return True
                if record.status is not LiveWorkspaceStatus.PLAN_UPDATED:
                    self._conflict("Submit a corrected plan before reauthorization.")
                if not self._has_verified_stale_grant(record):
                    self._conflict(
                        "A verified STALE_SNAPSHOT result is required before reauthorization."
                    )
                return False

            record = self._repository.get(workspace_id)
            if already_reauthorized(record):
                return LiveWorkspaceView.from_record(record)
            self._ensure_context(record)
            # The authority call stays outside the store transaction; the
            # verdict is applied to a fresh read.
            result = self._transport.authorize(
                record.context_id,
                AuthorizationRequest(
                    run_id=self._run_id(record),
                    task_id=record.definition.ticket.id,
                    plan=record.current_plan,
                ),
            )
            evaluated_plan = record.current_plan
            # Resuming the supervisor is a runtime call, so it stays outside
            # the transaction: computed from the record as read, applied only
            # if the fresh read still carries that supervisor.
            grant = result.grant if result.verdict is Verdict.ALLOW else None
            resumed = (
                self._resume_supervisor(
                    record,
                    decision_snapshot=grant.payload.decision_snapshot,
                )
                if grant is not None
                else None
            )

            def record_reauthorization(
                current: LiveWorkspaceRecord,
            ) -> LiveWorkspaceRecord:
                if already_reauthorized(current):
                    # Another writer reauthorized in between; its grant is the
                    # one the executor's attempt record will see.
                    return current
                if current.current_plan != evaluated_plan:
                    # PLAN_UPDATED admits a further update_plan, so the plan the
                    # authority evaluated may no longer be the stored one.
                    self._conflict(
                        "The corrected plan changed while it was being reauthorized."
                    )
                if grant is not None:
                    self._apply_supervisor_transitions(
                        current, read=record, transitioned=resumed
                    )
                    current.status = LiveWorkspaceStatus.REAUTHORIZED
                else:
                    current.status = LiveWorkspaceStatus.PLAN_UPDATED
                current.replacement_authorization = result
                self._event(
                    current,
                    event_type="plan.reauthorized",
                    detail=f"Corrected plan verdict: {result.verdict.value}.",
                    data={"verdict": result.verdict.value},
                )
                return current

            record = self._repository.mutate(workspace_id, record_reauthorization)
            return LiveWorkspaceView.from_record(record)

    def verify_replacement_grant(self, workspace_id: str) -> LiveWorkspaceView:
        with self._lock:

            def require_verifiable(record: LiveWorkspaceRecord) -> None:
                if record.status not in {
                    LiveWorkspaceStatus.REAUTHORIZED,
                    LiveWorkspaceStatus.COMPLETE,
                }:
                    self._conflict(
                        "Obtain a replacement ALLOW grant before verification."
                    )
                if not self._has_verified_stale_grant(record):
                    self._conflict(
                        "A verified STALE_SNAPSHOT result is required before completion."
                    )

            record = self._repository.get(workspace_id)
            require_verifiable(record)
            authorization = record.replacement_authorization
            if authorization is None or authorization.grant is None:
                self._conflict("The workspace has no replacement grant to verify.")
            self._ensure_context(record)
            # The executor call stays outside the store transaction; its result
            # is applied to a fresh read. Nothing reachable from these statuses
            # replaces the plan or the grant, so the status re-check suffices.
            execution = self._transport.execute(
                context_id=record.context_id,
                token=authorization.grant.token,
                run_id=self._run_id(record),
                task_id=record.definition.ticket.id,
                plan=record.current_plan,
            )
            # Completing the supervisor is a runtime call, so it stays outside
            # the transaction: computed from the record as read, applied only
            # if the fresh read still carries that supervisor.
            valid = (
                execution.applied
                and execution.verification_code is VerificationCode.VALID
            )
            completed = self._complete_supervisor(record) if valid else None

            def record_verification(
                current: LiveWorkspaceRecord,
            ) -> LiveWorkspaceRecord:
                require_verifiable(current)
                if valid:
                    self._apply_supervisor_transitions(
                        current, read=record, transitioned=completed
                    )
                    current.status = LiveWorkspaceStatus.COMPLETE
                current.replacement_verification = execution
                self._event(
                    current,
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
                return current

            record = self._repository.mutate(workspace_id, record_verification)
            return LiveWorkspaceView.from_record(record)
