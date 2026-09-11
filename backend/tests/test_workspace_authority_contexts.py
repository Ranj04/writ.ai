"""LRU bound on ``DynamicAuthorityContextRegistry`` (workspaces, not scenarios).

``test_scenario_authority_contexts.py`` covers ``ScenarioAuthorityContextRegistry``,
a different class in ``scenarios/authority_contexts.py``. These tests cover the
per-workspace registry behind ``authority_api.workspace_contexts``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from writai.config import settings
from writai.domain import (
    AgentPlan,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    AuthorizationRequest,
    AuthorizationResult,
    GrantVerificationRequest,
    MutationResult,
    PlanAction,
    VerificationCode,
)
from writai.hashing import stable_hash
from writai.intake.approval import ApprovalChannel, ApprovalEvidence
from writai.services import authority_api
from writai.workspaces.authority_contexts import (
    DynamicAuthorityContextCreateRequest,
    DynamicAuthorityContextNotFound,
    DynamicAuthorityContextRegistry,
    DynamicAuthorityContextState,
    DynamicMutationApprovalRequest,
)
from writai.workspaces.models import (
    LiveWorkspaceImportRequest,
    LiveWorkspaceStatus,
    WorkspaceApprovalRequest,
    WorkspaceExecutionResult,
    WorkspaceProposalRequest,
)
from writai.workspaces.orchestrator import LiveWorkspaceOrchestrator
from writai.workspaces.repository import SqliteLiveWorkspaceRepository


def _registry(max_contexts: int) -> DynamicAuthorityContextRegistry:
    return DynamicAuthorityContextRegistry(
        grant_secret="workspace-context-test-secret",
        grant_ttl_seconds=3600,
        authority_threshold=0.75,
        max_contexts=max_contexts,
    )


def _workspace_import(workspace_id: str = "lru-workspace") -> LiveWorkspaceImportRequest:
    scopes = {"scope.changed", "scope.preserved"}
    return LiveWorkspaceImportRequest(
        id=workspace_id,
        name="LRU workspace",
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
                    attributes={"task_id": "TASK-CHANGED", "mode": "automatic"},
                ),
                PlanAction(
                    id="ACTION-PRESERVED",
                    description="Write the concise output",
                    scopes={"scope.preserved"},
                    attributes={"task_id": "TASK-PRESERVED", "format": "concise"},
                ),
            ],
        ),
    )


def _context_request(context_id: str) -> DynamicAuthorityContextCreateRequest:
    definition = _workspace_import()
    return DynamicAuthorityContextCreateRequest(
        context_id=context_id,
        version=definition.graph_version,
        artifacts=definition.graph_artifacts(),
        edges=definition.graph_edges(),
        authority_policy=definition.authority_policy,
        baseline_decision_id=definition.baseline_decision.id,
    )


def test_the_registry_evicts_the_least_recently_used_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = _registry(max_contexts=3)
    with caplog.at_level(logging.INFO, logger="writai.workspaces.authority_contexts"):
        for context_id in ("ctx-1", "ctx-2", "ctx-3", "ctx-4"):
            registry.create(_context_request(context_id))

    with pytest.raises(DynamicAuthorityContextNotFound):
        registry.state("ctx-1")
    for survivor in ("ctx-2", "ctx-3", "ctx-4"):
        assert registry.state(survivor).context_id == survivor
    evictions = [message for message in caplog.messages if "Evicted" in message]
    assert evictions == ["Evicted least recently used authority context ctx-1 (limit 3)."]


def test_using_a_context_protects_it_from_eviction() -> None:
    """LRU, not FIFO: a use moves the context to the back of the queue."""

    registry = _registry(max_contexts=3)
    for context_id in ("ctx-1", "ctx-2", "ctx-3"):
        registry.create(_context_request(context_id))

    registry.state("ctx-1")
    registry.create(_context_request("ctx-4"))

    assert registry.state("ctx-1").context_id == "ctx-1"
    with pytest.raises(DynamicAuthorityContextNotFound):
        registry.state("ctx-2")
    for survivor in ("ctx-3", "ctx-4"):
        assert registry.state(survivor).context_id == survivor


def test_authorize_and_verify_count_as_use() -> None:
    """The enforcement-facing paths pass through the same LRU touch."""

    registry = _registry(max_contexts=2)
    registry.create(_context_request("ctx-1"))
    registry.create(_context_request("ctx-2"))
    plan = _workspace_import().plan

    # An unapproved baseline answers HUMAN_REVIEW, but the call still counts.
    registry.authorize(
        "ctx-1", AuthorizationRequest(run_id="RUN-1", task_id="TICKET-1", plan=plan)
    )
    registry.create(_context_request("ctx-3"))
    assert registry.state("ctx-1").context_id == "ctx-1"
    with pytest.raises(DynamicAuthorityContextNotFound):
        registry.state("ctx-2")

    registry.verify_grant(
        "ctx-3",
        GrantVerificationRequest(token="not-a-token", run_id="R", task_id="T", plan=plan),
    )
    registry.create(_context_request("ctx-4"))
    assert registry.state("ctx-3").context_id == "ctx-3"
    with pytest.raises(DynamicAuthorityContextNotFound):
        registry.state("ctx-1")


def test_a_limit_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_contexts"):
        _registry(max_contexts=0)


class _RegistryTransport:
    """In-process ``LiveWorkspaceTransport`` over one registry.

    ``execute`` verifies the grant against the registry so the test proves the
    rebuilt context still recognises a token the evicted one signed.
    """

    def __init__(self, registry: DynamicAuthorityContextRegistry) -> None:
        self.registry = registry

    def context_state(self, context_id: str) -> DynamicAuthorityContextState | None:
        try:
            return self.registry.state(context_id)
        except DynamicAuthorityContextNotFound:
            return None

    def create_context(
        self, request: DynamicAuthorityContextCreateRequest
    ) -> DynamicAuthorityContextState:
        return self.registry.create(request)

    def delete_context(self, context_id: str) -> None:
        self.registry.delete(context_id)

    def approve_baseline(
        self, context_id: str, request: WorkspaceApprovalRequest
    ) -> DynamicAuthorityContextState:
        return self.registry.approve_baseline(context_id, request)

    def approve_mutation(
        self, context_id: str, request: DynamicMutationApprovalRequest
    ) -> MutationResult:
        return self.registry.approve_mutation(context_id, request)

    def authorize(
        self, context_id: str, request: AuthorizationRequest
    ) -> AuthorizationResult:
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
        verification = self.registry.verify_grant(
            context_id,
            GrantVerificationRequest(
                token=token, run_id=run_id, task_id=task_id, plan=plan
            ),
        )
        return WorkspaceExecutionResult(
            applied=verification.valid,
            reason=verification.reason,
            verification_code=verification.code,
            execution_mode="simulated",
        )


def _evidence(
    *, workspace_id: str, decision_id: str, fingerprint: str, instance_id: str
) -> ApprovalEvidence:
    return ApprovalEvidence(
        workspace_id=workspace_id,
        decision_id=decision_id,
        approver_user_id="USER-APPROVER",
        permission_id="approver",
        channel=ApprovalChannel.WORKSPACE_UI,
        evidence_ref=f"workspace-ui://{workspace_id}/{decision_id}",
        approved_at=datetime(2026, 7, 3, tzinfo=UTC),
        confirmed_proposal_fingerprint=fingerprint,
        confirmed_proposal_instance_id=instance_id,
    )


def _apply_one_change(orchestrator: LiveWorkspaceOrchestrator) -> str:
    """Import, approve the baseline, authorize, propose and approve one change."""

    imported = orchestrator.import_workspace(_workspace_import())
    baseline_instance = imported.baseline_proposal_instance_id
    assert baseline_instance is not None
    orchestrator.approve_baseline(
        imported.id,
        WorkspaceApprovalRequest(
            actor_role="approver",
            proposal_fingerprint=imported.baseline_proposal_fingerprint,
            proposal_instance_id=baseline_instance,
            approval_evidence=_evidence(
                workspace_id=imported.id,
                decision_id="DEC-BASE",
                fingerprint=imported.baseline_proposal_fingerprint,
                instance_id=baseline_instance,
            ),
        ),
    )
    assert orchestrator.authorize(imported.id).status is LiveWorkspaceStatus.AUTHORIZED
    pending = orchestrator.propose_decision(
        imported.id,
        WorkspaceProposalRequest(
            decision=Artifact(
                id="DEC-CHANGE",
                kind=ArtifactKind.DECISION,
                title="Require a manual path",
                scopes={"scope.changed"},
                approval_status=ApprovalStatus.PROPOSAL,
                authority_role="approver",
                effective_at=datetime(2026, 7, 2, tzinfo=UTC),
                source_ref="manual://decision/change",
                attributes={"requirements": {"scope.changed": {"mode": "manual"}}},
            ),
            supersedes_id="DEC-BASE",
            affected_scopes={"scope.changed"},
        ),
    )
    assert pending.pending_mutation is not None
    fingerprint = pending.pending_proposal_fingerprint
    instance_id = pending.pending_proposal_instance_id
    assert fingerprint == stable_hash(pending.pending_mutation)
    assert instance_id is not None
    applied = orchestrator.approve_decision(
        imported.id,
        "DEC-CHANGE",
        WorkspaceApprovalRequest(
            actor_role="approver",
            proposal_fingerprint=fingerprint,
            proposal_instance_id=instance_id,
            approval_evidence=_evidence(
                workspace_id=imported.id,
                decision_id="DEC-CHANGE",
                fingerprint=fingerprint,
                instance_id=instance_id,
            ),
        ),
    )
    assert applied.status is LiveWorkspaceStatus.CHANGE_APPLIED
    return imported.id


def test_an_evicted_workspace_context_is_rebuilt_from_its_record(
    tmp_path: Path,
) -> None:
    """End to end through the orchestrator: evict, then use, and get the same graph."""

    registry = _registry(max_contexts=2)
    repository = SqliteLiveWorkspaceRepository(tmp_path / "live-workspaces.sqlite3")
    orchestrator = LiveWorkspaceOrchestrator(
        repository=repository,
        transport=_RegistryTransport(registry),
    )
    workspace_id = _apply_one_change(orchestrator)
    before_view = orchestrator.get(workspace_id)
    context_id = repository.get(workspace_id).context_id
    before = registry.state(context_id)
    assert before.graph_version == "graph-v18" == before_view.graph_version
    assert set(before.approval_evidence) == {"DEC-CHANGE"}
    assert [m.mutation.decision.id for m in before_view.approved_mutations] == ["DEC-CHANGE"]

    # Force eviction with max_contexts unrelated contexts.
    for index in range(registry.max_contexts):
        registry.create(_context_request(f"filler-{index}"))
    with pytest.raises(DynamicAuthorityContextNotFound):
        registry.state(context_id)

    # A route that needs the context rebuilds it from the durable record, then
    # verifies the ORIGINAL grant against the rebuilt graph: stale, as it must be.
    after_view = orchestrator.verify_initial_grant(workspace_id)
    assert after_view.status is LiveWorkspaceStatus.INITIAL_GRANT_REJECTED
    assert after_view.initial_verification is not None
    assert after_view.initial_verification.verification_code is VerificationCode.STALE_SNAPSHOT

    after = registry.state(context_id)
    assert after.graph_version == before.graph_version
    assert set(after.approval_evidence) == set(before.approval_evidence)
    assert after.artifacts == before.artifacts
    assert after.edges == before.edges
    assert after.baseline_approved is before.baseline_approved
    assert after_view.graph_version == before_view.graph_version
    assert after_view.approved_mutations == before_view.approved_mutations


def test_the_default_limit_is_the_configured_setting() -> None:
    constructed_like_the_service = DynamicAuthorityContextRegistry(
        grant_secret=settings.grant_secret,
        grant_ttl_seconds=settings.grant_ttl_seconds,
        authority_threshold=settings.authority_threshold,
        max_contexts=settings.max_authority_contexts,
    )
    assert constructed_like_the_service.max_contexts == settings.max_authority_contexts
    assert authority_api.workspace_contexts.max_contexts == settings.max_authority_contexts
    # A caller that passes no limit gets the same configured bound, never "unbounded".
    assert _registry(max_contexts=3).max_contexts == 3
    unspecified = DynamicAuthorityContextRegistry(
        grant_secret="x", grant_ttl_seconds=1, authority_threshold=0.5
    )
    assert unspecified.max_contexts == settings.max_authority_contexts
