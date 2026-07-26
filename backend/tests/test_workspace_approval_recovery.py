from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dragback.domain import (
    AgentPlan,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    AuthorizationRequest,
    AuthorizationResult,
    MutationResult,
    PlanAction,
)
from dragback.hashing import stable_hash
from dragback.intake.approval import (
    ApprovalChannel,
    ApprovalDisposition,
    ApprovalEvidence,
)
from dragback.workspaces.authority_contexts import (
    DynamicAuthorityContextCreateRequest,
    DynamicAuthorityContextNotFound,
    DynamicAuthorityContextRegistry,
    DynamicAuthorityContextState,
    DynamicMutationApprovalRequest,
)
from dragback.workspaces.models import (
    LiveWorkspaceImportRequest,
    LiveWorkspaceRecord,
    LiveWorkspaceStatus,
    SlackUserIdentityBinding,
    WorkspaceApprovalRequest,
    WorkspaceExecutionResult,
    WorkspaceProposalRequest,
    WorkspaceSlackBinding,
)
from dragback.workspaces.orchestrator import LiveWorkspaceOrchestrator
from dragback.workspaces.repository import (
    JsonFileLiveWorkspaceRepository,
    LiveWorkspaceRepository,
)
from dragback.workspaces.supervisor import (
    FixtureSupervisorRuntime,
    SupervisorAssignment,
    SupervisorAssignmentState,
    SupervisorExecutionMode,
    WorkspaceSupervisor,
)


class RegistryTransport:
    def __init__(self) -> None:
        self.registry = DynamicAuthorityContextRegistry(
            grant_secret="approval-recovery-secret",
            grant_ttl_seconds=3600,
            authority_threshold=0.75,
        )
        self.mutation_calls = 0
        self.mutation_effects = 0
        self.authorize_calls = 0
        self.fail_next_authorize = False
        self.before_authorize: Callable[[], None] | None = None

    def context_state(
        self,
        context_id: str,
    ) -> DynamicAuthorityContextState | None:
        try:
            return self.registry.state(context_id)
        except DynamicAuthorityContextNotFound:
            return None

    def create_context(
        self,
        request: DynamicAuthorityContextCreateRequest,
    ) -> DynamicAuthorityContextState:
        return self.registry.create(request)

    def delete_context(self, context_id: str) -> None:
        self.registry.delete(context_id)

    def approve_baseline(
        self,
        context_id: str,
        request: WorkspaceApprovalRequest,
    ) -> DynamicAuthorityContextState:
        return self.registry.approve_baseline(context_id, request)

    def approve_mutation(
        self,
        context_id: str,
        request: DynamicMutationApprovalRequest,
    ) -> MutationResult:
        before = self.registry.state(context_id).graph_version
        self.mutation_calls += 1
        result = self.registry.approve_mutation(context_id, request)
        if result.graph_version != before:
            self.mutation_effects += 1
        return result

    def authorize(
        self,
        context_id: str,
        request: AuthorizationRequest,
    ) -> AuthorizationResult:
        self.authorize_calls += 1
        if self.before_authorize is not None:
            self.before_authorize()
        if self.fail_next_authorize:
            self.fail_next_authorize = False
            raise ConnectionError("authority response was unavailable")
        return self.registry.authorize(context_id, request)

    def execute(
        self,
        *,
        context_id: str,
        token: str,
        run_id: str,
        task_id: str,
        plan: AgentPlan,
    ) -> WorkspaceExecutionResult:
        raise AssertionError("approval recovery tests do not execute grants")


class CountingSupervisorRuntime:
    adapter_name: str = "fixture-agent-runtime"
    execution_mode: SupervisorExecutionMode = SupervisorExecutionMode.SIMULATED

    def __init__(self) -> None:
        self._delegate = FixtureSupervisorRuntime()
        self.interrupt_transitions = 0

    def create_supervisor(
        self,
        *,
        workspace_id: str,
        supervisor_run_id: str,
        tasks: list[Artifact],
        plan: AgentPlan,
        decision_snapshot: str,
    ) -> WorkspaceSupervisor:
        return self._delegate.create_supervisor(
            workspace_id=workspace_id,
            supervisor_run_id=supervisor_run_id,
            tasks=tasks,
            plan=plan,
            decision_snapshot=decision_snapshot,
        )

    def transition(
        self,
        assignment: SupervisorAssignment,
        *,
        state: SupervisorAssignmentState,
        plan: AgentPlan | None = None,
        plan_actions: list[PlanAction] | None = None,
        decision_snapshot: str | None = None,
        create_replacement_run: bool = False,
        interrupt_reason: str | None = None,
        redirect_instruction: str | None = None,
        provenance_path: list[str] | None = None,
        interrupt_enforced: bool | None = None,
    ) -> SupervisorAssignment:
        if state is SupervisorAssignmentState.INTERRUPTED:
            self.interrupt_transitions += 1
        return self._delegate.transition(
            assignment,
            state=state,
            plan=plan,
            plan_actions=plan_actions,
            decision_snapshot=decision_snapshot,
            create_replacement_run=create_replacement_run,
            interrupt_reason=interrupt_reason,
            redirect_instruction=redirect_instruction,
            provenance_path=provenance_path,
            interrupt_enforced=interrupt_enforced,
        )


class FailFinalSaveRepository:
    def __init__(self, delegate: LiveWorkspaceRepository) -> None:
        self.delegate = delegate
        self.fail_final_once = False

    def create(self, record: LiveWorkspaceRecord) -> None:
        self.delegate.create(record)

    def save(self, record: LiveWorkspaceRecord) -> None:
        if (
            self.fail_final_once
            and record.status is LiveWorkspaceStatus.CHANGE_APPLIED
            and record.decision_approval_intent is None
        ):
            self.fail_final_once = False
            raise OSError("final workspace save failed")
        self.delegate.save(record)

    def get(self, workspace_id: str) -> LiveWorkspaceRecord:
        return self.delegate.get(workspace_id)

    def list(self) -> list[LiveWorkspaceRecord]:
        return self.delegate.list()


def _workspace_import(*, with_slack: bool = False) -> LiveWorkspaceImportRequest:
    scopes = {"scope.changed", "scope.preserved"}
    slack_binding = (
        WorkspaceSlackBinding(
            workspace_id="approval-recovery",
            slack_team_id="T-RECOVERY",
            composio_connection_user_id="recovery-connection",
            hexclave_team_id="hex-recovery",
            user_identities=(
                SlackUserIdentityBinding(
                    slack_user_id="U-APPROVER",
                    hexclave_user_id="approver",
                    evidence_ref="hexclave://identity/U-APPROVER",
                ),
            ),
        )
        if with_slack
        else None
    )
    return LiveWorkspaceImportRequest(
        id="approval-recovery",
        name="Approval recovery",
        authority_policy={scope: {"approver"} for scope in scopes},
        baseline_decision=Artifact(
            id="DEC-BASE",
            kind=ArtifactKind.DECISION,
            title="Baseline",
            scopes=scopes,
            approval_status=ApprovalStatus.PROPOSAL,
            authority_role="approver",
            effective_at=datetime(2026, 7, 1, tzinfo=UTC),
            source_ref="manual://decision/base",
            attributes={
                "requirements": {
                    "scope.changed": {"mode": "automatic"},
                    "scope.preserved": {"format": "concise"},
                }
            },
        ),
        specification=Artifact(
            id="SPEC-1",
            kind=ArtifactKind.SPECIFICATION,
            title="Specification",
            scopes=scopes,
            source_ref="manual://specification/1",
        ),
        ticket=Artifact(
            id="TICKET-1",
            kind=ArtifactKind.TICKET,
            title="Implement the workflow",
            scopes=scopes,
            source_ref="manual://ticket/1",
        ),
        tasks=[
            Artifact(
                id="TASK-CHANGED",
                kind=ArtifactKind.TASK,
                title="Changed task",
                scopes={"scope.changed"},
                source_ref="manual://task/changed",
            ),
            Artifact(
                id="TASK-PRESERVED",
                kind=ArtifactKind.TASK,
                title="Preserved task",
                scopes={"scope.preserved"},
                source_ref="manual://task/preserved",
            ),
        ],
        plan=AgentPlan(
            id="PLAN-1",
            ticket_id="TICKET-1",
            objective="Implement the baseline",
            actions=[
                PlanAction(
                    id="ACTION-CHANGED",
                    description="Run the automatic path",
                    scopes={"scope.changed"},
                    attributes={
                        "task_id": "TASK-CHANGED",
                        "mode": "automatic",
                    },
                ),
                PlanAction(
                    id="ACTION-PRESERVED",
                    description="Write the concise output",
                    scopes={"scope.preserved"},
                    attributes={
                        "task_id": "TASK-PRESERVED",
                        "format": "concise",
                    },
                ),
            ],
        ),
        slack_binding=slack_binding,
    )


def _proposal() -> WorkspaceProposalRequest:
    return WorkspaceProposalRequest(
        decision=Artifact(
            id="DEC-CHANGE",
            kind=ArtifactKind.DECISION,
            title="Require a manual path",
            scopes={"scope.changed"},
            approval_status=ApprovalStatus.PROPOSAL,
            authority_role="approver",
            effective_at=datetime(2026, 7, 2, tzinfo=UTC),
            source_ref="manual://decision/change",
            attributes={
                "requirements": {
                    "scope.changed": {"mode": "manual"},
                }
            },
        ),
        supersedes_id="DEC-BASE",
        affected_scopes={"scope.changed"},
    )


def _approval_evidence(
    *,
    workspace_id: str,
    decision_id: str,
    fingerprint: str,
    instance_id: str,
    approved_at: datetime,
) -> ApprovalEvidence:
    return ApprovalEvidence(
        workspace_id=workspace_id,
        decision_id=decision_id,
        approver_user_id="USER-APPROVER",
        permission_id="approver",
        channel=ApprovalChannel.WORKSPACE_UI,
        evidence_ref=f"workspace-ui://{workspace_id}/{decision_id}",
        approved_at=approved_at,
        confirmed_proposal_fingerprint=fingerprint,
        confirmed_proposal_instance_id=instance_id,
    )


def _prepare_change(
    *,
    repository: LiveWorkspaceRepository,
    transport: RegistryTransport,
    runtime: CountingSupervisorRuntime,
) -> WorkspaceApprovalRequest:
    orchestrator = LiveWorkspaceOrchestrator(
        repository=repository,
        transport=transport,
        supervisor_runtime=runtime,
    )
    imported = orchestrator.import_workspace(_workspace_import())
    baseline_instance = imported.baseline_proposal_instance_id
    assert baseline_instance is not None
    baseline_fingerprint = imported.baseline_proposal_fingerprint
    orchestrator.approve_baseline(
        imported.id,
        WorkspaceApprovalRequest(
            actor_role="approver",
            proposal_fingerprint=baseline_fingerprint,
            proposal_instance_id=baseline_instance,
            approval_evidence=_approval_evidence(
                workspace_id=imported.id,
                decision_id=imported.baseline_decision.id,
                fingerprint=baseline_fingerprint,
                instance_id=baseline_instance,
                approved_at=datetime(2026, 7, 3, tzinfo=UTC),
            ),
        ),
    )
    authorized = orchestrator.authorize(imported.id)
    assert authorized.status is LiveWorkspaceStatus.AUTHORIZED
    pending = orchestrator.propose_decision(imported.id, _proposal())
    fingerprint = pending.pending_proposal_fingerprint
    instance_id = pending.pending_proposal_instance_id
    assert pending.pending_mutation is not None
    assert fingerprint == stable_hash(pending.pending_mutation)
    assert instance_id is not None
    transport.authorize_calls = 0
    return WorkspaceApprovalRequest(
        actor_role="approver",
        proposal_fingerprint=fingerprint,
        proposal_instance_id=instance_id,
        approval_evidence=_approval_evidence(
            workspace_id=imported.id,
            decision_id="DEC-CHANGE",
            fingerprint=fingerprint,
            instance_id=instance_id,
            approved_at=datetime(2026, 7, 4, tzinfo=UTC),
        ),
    )


def _assert_durable_checkpoint(record: LiveWorkspaceRecord) -> None:
    intent = record.decision_approval_intent
    assert intent is not None
    assert intent.mutation_result is not None
    assert intent.approval_evidence.decision_id == "DEC-CHANGE"
    assert record.status is LiveWorkspaceStatus.CHANGE_PROPOSED
    assert record.pending_mutation is not None
    assert record.approved_mutations == []
    assert record.conflict_authorization is None
    assert record.invalidation_report == intent.mutation_result.report
    assert record.graph_version == intent.mutation_result.graph_version
    assert record.supervisor is not None
    states = {
        assignment.task_id: assignment.state
        for assignment in record.supervisor.assignments
    }
    assert states == {
        "TASK-CHANGED": SupervisorAssignmentState.INTERRUPTED,
        "TASK-PRESERVED": SupervisorAssignmentState.CONTINUING,
    }
    assert not any(
        event.event_type == "decision.approved" for event in record.history
    )


def _retry_request(request: WorkspaceApprovalRequest) -> WorkspaceApprovalRequest:
    evidence = request.approval_evidence
    assert evidence is not None
    return request.model_copy(
        update={
            "approval_evidence": evidence.model_copy(
                update={"approved_at": evidence.approved_at + timedelta(minutes=1)}
            )
        },
        deep=True,
    )


def _assert_final(record: LiveWorkspaceRecord) -> None:
    assert record.status is LiveWorkspaceStatus.CHANGE_APPLIED
    assert record.decision_approval_intent is None
    assert record.pending_mutation is None
    assert record.conflict_authorization is not None
    assert len(record.approved_mutations) == 1
    assert record.approved_mutations[0].mutation.decision.id == "DEC-CHANGE"
    assert record.supervisor is not None
    assert {
        assignment.task_id: assignment.state
        for assignment in record.supervisor.assignments
    } == {
        "TASK-CHANGED": SupervisorAssignmentState.INTERRUPTED,
        "TASK-PRESERVED": SupervisorAssignmentState.CONTINUING,
    }
    assert sum(
        event.event_type == "decision.approved" for event in record.history
    ) == 1


def test_authorize_failure_recovers_from_durable_interrupt_checkpoint(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "workspaces.json"
    repository = JsonFileLiveWorkspaceRepository(store_path)
    transport = RegistryTransport()
    runtime = CountingSupervisorRuntime()
    request = _prepare_change(
        repository=repository,
        transport=transport,
        runtime=runtime,
    )
    orchestrator = LiveWorkspaceOrchestrator(
        repository=repository,
        transport=transport,
        supervisor_runtime=runtime,
    )

    def assert_before_authorize() -> None:
        _assert_durable_checkpoint(
            JsonFileLiveWorkspaceRepository(store_path).get("approval-recovery")
        )

    transport.before_authorize = assert_before_authorize
    transport.fail_next_authorize = True
    with pytest.raises(ConnectionError, match="unavailable"):
        orchestrator.approve_decision(
            "approval-recovery",
            "DEC-CHANGE",
            request,
        )

    _assert_durable_checkpoint(repository.get("approval-recovery"))
    assert transport.mutation_calls == 1
    assert transport.mutation_effects == 1
    assert runtime.interrupt_transitions == 1
    public = orchestrator.get("approval-recovery").model_dump(mode="json")
    assert "decision_approval_intent" not in public
    assert public["conflict_authorization"] is None

    restarted = LiveWorkspaceOrchestrator(
        repository=JsonFileLiveWorkspaceRepository(store_path),
        transport=transport,
        supervisor_runtime=runtime,
    )
    restarted.approve_decision(
        "approval-recovery",
        "DEC-CHANGE",
        _retry_request(request),
    )
    final = JsonFileLiveWorkspaceRepository(store_path).get("approval-recovery")
    _assert_final(final)
    assert final.approved_mutations[0].approval_evidence == request.approval_evidence
    assert transport.mutation_calls == 1
    assert transport.mutation_effects == 1
    assert transport.authorize_calls == 2
    assert runtime.interrupt_transitions == 1


def test_final_repository_save_failure_reuses_durable_authorization(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "workspaces.json"
    durable_repository = JsonFileLiveWorkspaceRepository(store_path)
    repository = FailFinalSaveRepository(durable_repository)
    transport = RegistryTransport()
    runtime = CountingSupervisorRuntime()
    request = _prepare_change(
        repository=repository,
        transport=transport,
        runtime=runtime,
    )
    orchestrator = LiveWorkspaceOrchestrator(
        repository=repository,
        transport=transport,
        supervisor_runtime=runtime,
    )
    repository.fail_final_once = True

    with pytest.raises(OSError, match="final workspace save"):
        orchestrator.approve_decision(
            "approval-recovery",
            "DEC-CHANGE",
            request,
        )

    checkpoint = durable_repository.get("approval-recovery")
    _assert_durable_checkpoint(checkpoint)
    assert checkpoint.decision_approval_intent is not None
    assert checkpoint.decision_approval_intent.authorization_result is not None
    assert transport.mutation_calls == 1
    assert transport.mutation_effects == 1
    assert transport.authorize_calls == 1
    assert runtime.interrupt_transitions == 1

    restarted = LiveWorkspaceOrchestrator(
        repository=JsonFileLiveWorkspaceRepository(store_path),
        transport=transport,
        supervisor_runtime=runtime,
    )
    restarted.approve_decision(
        "approval-recovery",
        "DEC-CHANGE",
        _retry_request(request),
    )
    _assert_final(JsonFileLiveWorkspaceRepository(store_path).get("approval-recovery"))
    assert transport.mutation_calls == 1
    assert transport.mutation_effects == 1
    assert transport.authorize_calls == 1
    assert runtime.interrupt_transitions == 1


def test_public_view_redacts_slack_identity_mappings(tmp_path: Path) -> None:
    repository = JsonFileLiveWorkspaceRepository(tmp_path / "workspaces.json")
    orchestrator = LiveWorkspaceOrchestrator(
        repository=repository,
        transport=RegistryTransport(),
    )
    imported = orchestrator.import_workspace(_workspace_import(with_slack=True))

    assert imported.slack_binding is not None
    assert imported.slack_binding.user_identities == ()
    stored = repository.get("approval-recovery")
    assert stored.definition.slack_binding is not None
    assert len(stored.definition.slack_binding.user_identities) == 1


def test_rejected_approval_is_durable_and_does_not_change_pending_authority(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "workspaces.json"
    repository = JsonFileLiveWorkspaceRepository(store_path)
    transport = RegistryTransport()
    runtime = CountingSupervisorRuntime()
    request = _prepare_change(
        repository=repository,
        transport=transport,
        runtime=runtime,
    )
    orchestrator = LiveWorkspaceOrchestrator(
        repository=repository,
        transport=transport,
        supervisor_runtime=runtime,
    )
    before = repository.get("approval-recovery")

    view = orchestrator.record_approval_rejection(
        "approval-recovery",
        decision_id="DEC-CHANGE",
        disposition=ApprovalDisposition.IGNORED_NOT_AUTHORIZED.value,
        approver_user_id="USER-DENIED",
        permission_id="approver",
        approval_channel=ApprovalChannel.WORKSPACE_UI.value,
        approval_evidence_ref="workspace-ui://approval-recovery/DEC-CHANGE",
        confirmed_proposal_fingerprint=request.proposal_fingerprint,
        confirmed_proposal_instance_id=request.proposal_instance_id,
        detail="The authenticated user cannot approve this Decision.",
    )

    after = JsonFileLiveWorkspaceRepository(store_path).get(
        "approval-recovery"
    )
    assert after.status is before.status
    assert after.graph_version == before.graph_version
    assert after.pending_mutation == before.pending_mutation
    assert after.pending_proposal_instance_id == (
        before.pending_proposal_instance_id
    )
    assert transport.mutation_calls == 0
    event = after.history[-1]
    assert event.event_type == "decision.approval-rejected"
    assert event.data == {
        "decision_id": "DEC-CHANGE",
        "disposition": "IGNORED_NOT_AUTHORIZED",
        "approver_user_id": "USER-DENIED",
        "permission_id": "approver",
        "approval_channel": "workspace-ui",
        "approval_evidence_ref": (
            "workspace-ui://approval-recovery/DEC-CHANGE"
        ),
        "proposal_fingerprint": request.proposal_fingerprint,
        "proposal_instance_id": request.proposal_instance_id,
        "confirmed_proposal_fingerprint": request.proposal_fingerprint,
        "confirmed_proposal_instance_id": request.proposal_instance_id,
        "rejected_at": event.data["rejected_at"],
    }
    assert "token" not in event.model_dump_json().casefold()
    assert view.history[-1] == event
