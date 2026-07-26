from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event as ThreadEvent
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from writai.authority.engine import IntentAuthority
from writai.domain import (
    AgentPlan,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    AuthorizationRequest,
    AuthorizationResult,
    DecisionMutation,
    GrantVerificationRequest,
    InvalidationReport,
    PlanAction,
    Verdict,
    VerificationCode,
)
from writai.grants import GrantSigner
from writai.graph.memory import MemoryGraphStore
from writai.integrations.callwright import FixtureCallwrightClient
from writai.notify.escalate import (
    AuthorizedInterruptEscalation,
    GrantGatedExecutorPort,
    InterruptAcknowledgementReader,
    InterruptEscalationDisposition,
    InterruptEscalationIntent,
    InterruptEscalationScanner,
    build_interrupt_escalation_plan,
)
from writai.services import executor_api
from writai.services.escalation_api import (
    compose_interrupt_escalation_router,
)
from writai.services.support import (
    INTERNAL_SERVICE_AUTH_HEADER,
    install_api_support,
    internal_service_token,
)
from writai.workspaces.models import (
    ApprovedWorkspaceMutation,
    LiveWorkspaceImportRequest,
    LiveWorkspaceRecord,
    LiveWorkspaceStatus,
    WorkspaceEvent,
    WorkspaceExecutionResult,
)
from writai.workspaces.repository import JsonFileLiveWorkspaceRepository
from writai.workspaces.runtimes.claude_code import (
    ClaudeCodeSupervisorRuntime,
)
from writai.workspaces.session_binding import (
    ClaudeCodeSessionRegistry,
    SupervisorAssignmentTarget,
)
from writai.workspaces.session_enforcement import (
    ClaudeCodeSessionEnforcement,
    RepositorySupervisorAssignmentGateway,
)
from writai.workspaces.supervisor import (
    SupervisorAssignment,
    SupervisorAssignmentState,
    SupervisorExecutionMode,
    SupervisorLifecycleState,
    SupervisorRuntimeProvider,
    WorkspaceSupervisor,
)
from writai.workspaces.transport import LiveWorkspaceTransport

NOW = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
SCOPE = "incident.escalation"


class StaticEscalationSource:
    def __init__(
        self,
        *candidates: AuthorizedInterruptEscalation,
    ) -> None:
        self.candidates = candidates

    def list_candidates(
        self,
        *,
        now: datetime,
    ) -> tuple[AuthorizedInterruptEscalation, ...]:
        assert now.utcoffset() is not None
        return self.candidates


class NeverAcknowledged:
    def __init__(self) -> None:
        self.calls = 0

    def is_acknowledged(
        self,
        *,
        session_id: str,
        interrupt_key: str,
    ) -> bool:
        assert session_id
        assert interrupt_key
        self.calls += 1
        return False


class AcknowledgeOnSecondCheck(NeverAcknowledged):
    def is_acknowledged(
        self,
        *,
        session_id: str,
        interrupt_key: str,
    ) -> bool:
        super().is_acknowledged(
            session_id=session_id,
            interrupt_key=interrupt_key,
        )
        return self.calls == 2


class FailingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(
        self,
        *,
        context_id: str,
        token: str,
        run_id: str,
        task_id: str,
        plan: AgentPlan,
    ) -> WorkspaceExecutionResult:
        self.calls += 1
        raise AssertionError(
            "an ineligible escalation must not reach the executor"
        )


class InProcessExecutor:
    """Exercise the real executor API without a network listener."""

    def __init__(self) -> None:
        self.client = TestClient(executor_api.app)
        self.calls = 0

    def execute(
        self,
        *,
        context_id: str,
        token: str,
        run_id: str,
        task_id: str,
        plan: AgentPlan,
    ) -> WorkspaceExecutionResult:
        self.calls += 1
        response = self.client.post(
            "/execute",
            json={
                "context_id": context_id,
                "context_kind": "workspace",
                "token": token,
                "run_id": run_id,
                "task_id": task_id,
                "plan": plan.model_dump(mode="json"),
            },
        )
        assert response.status_code == 200
        return WorkspaceExecutionResult.model_validate(response.json())


class LocalAuthorityExecutorTransport:
    """Concrete in-process authority and real executor-API composition."""

    def __init__(
        self,
        *,
        authority: IntentAuthority,
        executor: InProcessExecutor,
        context_id: str,
    ) -> None:
        self.authority = authority
        self.executor = executor
        self.context_id = context_id
        self.authorize_calls = 0
        self.execute_event = ThreadEvent()

    def authorize(
        self,
        context_id: str,
        request: AuthorizationRequest,
    ) -> AuthorizationResult:
        assert context_id == self.context_id
        self.authorize_calls += 1
        return self.authority.evaluate_plan(
            run_id=request.run_id,
            task_id=request.task_id,
            plan=request.plan,
        )

    def execute(
        self,
        *,
        context_id: str,
        token: str,
        run_id: str,
        task_id: str,
        plan: AgentPlan,
    ) -> WorkspaceExecutionResult:
        assert context_id == self.context_id
        result = self.executor.execute(
            context_id=context_id,
            token=token,
            run_id=run_id,
            task_id=task_id,
            plan=plan,
        )
        self.execute_event.set()
        return result


def _intent(
    *,
    state: SupervisorAssignmentState = SupervisorAssignmentState.INTERRUPTED,
    assignment_scopes: frozenset[str] = frozenset({SCOPE}),
    affected_scopes: frozenset[str] = frozenset({SCOPE}),
    interrupted_at: datetime = NOW - timedelta(minutes=10),
) -> InterruptEscalationIntent:
    return InterruptEscalationIntent(
        workspace_id="incident-workspace",
        authority_context_id="incident-workspace-context",
        session_id="claude-session-1",
        interrupt_key="interrupt-key-1",
        assignment_id="ASSIGNMENT-TASK-ESCALATION",
        assignment_state=state,
        run_id="RUN-ESCALATION-1",
        task_id="TASK-ESCALATION",
        decision_snapshot="graph-v18",
        origin_decision_id="DECISION-18",
        origin_event_id="sha256:origin-event-1",
        interrupted_at=interrupted_at,
        assignment_scopes=assignment_scopes,
        affected_scopes=affected_scopes,
        interrupt_reason=(
            "Approved decision DECISION-18 invalidated the active incident response."
        ),
        provenance_path=(
            "DECISION-18",
            "DECISION-17",
            "SPEC-ESCALATION",
            "TASK-ESCALATION",
        ),
        invalidated_artifact_ids=(
            "TASK-ESCALATION",
            "PLAN-ESCALATION",
        ),
        preserved_artifact_ids=("TASK-UNRELATED",),
        evidence_refs=("slack://T001/C001/1720012345.000100",),
        requirements_by_scope={SCOPE: {}},
        phone_number_ref="demo-venue",
    )


def _authority_for(
    intent: InterruptEscalationIntent,
) -> tuple[IntentAuthority, AuthorizedInterruptEscalation]:
    graph = MemoryGraphStore()
    graph.reset(
        version=18,
        artifacts=[
            Artifact(
                id="DECISION-ESCALATION-BASELINE",
                kind=ArtifactKind.DECISION,
                title="Escalation policy",
                scopes=set(intent.impacted_scopes),
                approval_status=ApprovalStatus.APPROVED,
                authority_role="approve_compliance",
                effective_at=NOW - timedelta(days=1),
                attributes={
                    "requirements": {
                        scope: {} for scope in intent.impacted_scopes
                    }
                },
            ),
            Artifact(
                id=intent.task_id,
                kind=ArtifactKind.TASK,
                title="Escalate an unacknowledged interrupt",
                scopes=set(intent.impacted_scopes),
            ),
        ],
        edges=[],
    )
    authority = IntentAuthority(
        graph=graph,
        signer=GrantSigner("interrupt-escalation-test-secret", ttl_seconds=3600),
    )
    plan = build_interrupt_escalation_plan(intent)
    authorization = authority.evaluate_plan(
        run_id=intent.run_id,
        task_id=intent.task_id,
        plan=plan,
    )
    assert authorization.grant is not None
    return (
        authority,
        AuthorizedInterruptEscalation(
            intent=intent,
            grant_token=SecretStr(authorization.grant.token),
        ),
    )


def _wire_executor(
    monkeypatch: pytest.MonkeyPatch,
    authority: IntentAuthority,
) -> tuple[InProcessExecutor, FixtureCallwrightClient]:
    fixture = FixtureCallwrightClient()
    monkeypatch.setattr(executor_api, "callwright_client", fixture)
    monkeypatch.setattr(
        executor_api,
        "settings",
        replace(
            executor_api.settings,
            execution_provider="fixture",
            callwright_demo_phone_number=None,
        ),
    )

    def verify(**kwargs: object) -> object:
        request = kwargs["payload"]
        assert isinstance(request, GrantVerificationRequest)
        return authority.verify_grant(
            token=request.token,
            run_id=request.run_id,
            task_id=request.task_id,
            plan=request.plan,
        )

    monkeypatch.setattr(executor_api, "post_model", verify)
    return InProcessExecutor(), fixture


def _workspace_definition() -> LiveWorkspaceImportRequest:
    baseline_at = NOW - timedelta(days=1)
    return LiveWorkspaceImportRequest(
        id="incident-workspace",
        name="Incident response",
        authority_policy={SCOPE: {"approve_compliance"}},
        baseline_decision=Artifact(
            id="DECISION-17",
            kind=ArtifactKind.DECISION,
            title="Incident escalation baseline",
            scopes={SCOPE},
            approval_status=ApprovalStatus.PROPOSAL,
            authority_role="approve_compliance",
            effective_at=baseline_at,
            source_ref="manual://incident/decision-17",
            attributes={
                "requirements": {
                    SCOPE: {"human_approval": False},
                }
            },
        ),
        specification=Artifact(
            id="SPEC-ESCALATION",
            kind=ArtifactKind.SPECIFICATION,
            title="Incident escalation specification",
            scopes={SCOPE},
            source_ref="manual://incident/spec",
        ),
        ticket=Artifact(
            id="TICKET-ESCALATION",
            kind=ArtifactKind.TICKET,
            title="Implement incident escalation",
            scopes={SCOPE},
            source_ref="manual://incident/ticket",
        ),
        tasks=[
            Artifact(
                id="TASK-ESCALATION",
                kind=ArtifactKind.TASK,
                title="Implement escalation",
                scopes={SCOPE},
                source_ref="manual://incident/task",
            )
        ],
        plan=AgentPlan(
            id="PLAN-17",
            ticket_id="TICKET-ESCALATION",
            objective="Implement incident escalation",
            actions=[
                PlanAction(
                    id="ACTION-17",
                    description="Implement the baseline behavior",
                    scopes={SCOPE},
                    attributes={"human_approval": False},
                )
            ],
        ),
        graph_version=17,
    )


def _repository_runtime(
    tmp_path: Path,
    *,
    state: SupervisorAssignmentState = SupervisorAssignmentState.INTERRUPTED,
    assignment_scopes: set[str] | None = None,
    interrupt_age: timedelta = timedelta(minutes=10),
    interrupt_assignment_ids: list[str] | None = None,
    include_supervisor_interrupt_event: bool = True,
) -> tuple[
    JsonFileLiveWorkspaceRepository,
    ClaudeCodeSessionRegistry,
    IntentAuthority,
]:
    definition = _workspace_definition()
    scopes = assignment_scopes if assignment_scopes is not None else {SCOPE}
    assignment = SupervisorAssignment(
        id="ASSIGNMENT-TASK-ESCALATION",
        task_id="TASK-ESCALATION",
        task_title="Implement escalation",
        agent_name="Claude escalation agent",
        runtime_provider=SupervisorRuntimeProvider.CLAUDE_CODE,
        execution_mode=SupervisorExecutionMode.LIVE,
        run_id="RUN-ESCALATION-1",
        state=state,
        scopes=scopes,
        plan_id="PLAN-17",
        decision_snapshot="graph-v17",
        interrupt_reason=(
            "Approved decision DECISION-18 requires human escalation approval."
        ),
        redirect_instruction=None,
        provenance_path=[
            "DECISION-18",
            "DECISION-17",
            "SPEC-ESCALATION",
            "TASK-ESCALATION",
        ],
        interrupt_enforced=True,
    )
    history = [
        WorkspaceEvent(
            sequence=1,
            event_type="decision.approved",
            detail="DECISION-18 was approved.",
            created_at=NOW - timedelta(minutes=20),
            data={
                "decision_id": "DECISION-18",
                "invalidated_task_ids": [assignment.task_id],
                "preserved_task_ids": ["TASK-UNRELATED"],
                "approval_evidence_ref": (
                    "slack://T001/C001/1720012345.000100"
                ),
            },
        )
    ]
    if include_supervisor_interrupt_event:
        history.append(
            WorkspaceEvent(
                sequence=2,
                event_type="supervisor.interrupt.enforced",
                detail="DECISION-18 interrupted the affected assignment.",
                created_at=NOW - interrupt_age,
                data={
                    "decision_id": "DECISION-18",
                    "request_fingerprint": "sha256:fixture",
                    "interrupted_assignment_ids": (
                        interrupt_assignment_ids
                        if interrupt_assignment_ids is not None
                        else [assignment.id]
                    ),
                    "preserved_assignment_ids": [],
                },
            )
        )
    record = LiveWorkspaceRecord(
        definition=definition,
        context_id="incident-workspace-context",
        status=LiveWorkspaceStatus.CHANGE_APPLIED,
        graph_version="graph-v18",
        baseline_approved=True,
        current_plan=definition.plan,
        approved_mutations=[
            ApprovedWorkspaceMutation(
                mutation=DecisionMutation(
                    decision=Artifact(
                        id="DECISION-18",
                        kind=ArtifactKind.DECISION,
                        title="Human escalation approval",
                        scopes={SCOPE},
                        approval_status=ApprovalStatus.PROPOSAL,
                        authority_role="approve_compliance",
                        effective_at=NOW - timedelta(minutes=20),
                        source_ref=(
                            "slack://T001/C001/1720012345.000100"
                        ),
                        attributes={
                            "requirements": {
                                SCOPE: {"human_approval": True},
                            }
                        },
                    ),
                    supersedes_id="DECISION-17",
                    affected_scopes={SCOPE},
                ),
                actor_role="approve_compliance",
            )
        ],
        conflict_authorization=AuthorizationResult(
            verdict=Verdict.REPLAN,
            reason="The incident plan must now require human approval.",
            graph_version="graph-v18",
            task_id=definition.ticket.id,
            current_requirements={SCOPE: {"human_approval": True}},
        ),
        invalidation_report=InvalidationReport(
            graph_version="graph-v18",
            changed_decision_id="DECISION-18",
            superseded_decision_id="DECISION-17",
            affected_scopes={SCOPE},
            affected_artifact_ids=[
                "DECISION-17",
                "SPEC-ESCALATION",
                "TICKET-ESCALATION",
                "TASK-ESCALATION",
                "PLAN-17",
            ],
            invalidated_task_ids=["TASK-ESCALATION"],
            stopped_work_artifact_ids=["TASK-ESCALATION", "PLAN-17"],
            preserved_artifact_ids=["TASK-UNRELATED"],
            evidence_refs=["slack://T001/C001/1720012345.000100"],
        ),
        supervisor=WorkspaceSupervisor(
            id="SUPERVISOR-INCIDENT",
            state=SupervisorLifecycleState.INTERRUPTING,
            adapter="claude-code-hook-runtime",
            execution_mode=SupervisorExecutionMode.LIVE,
            assignments=[assignment],
        ),
        history=history,
    )
    repository = JsonFileLiveWorkspaceRepository(
        tmp_path / "live-workspaces.json"
    )
    repository.create(record)
    sessions = ClaudeCodeSessionRegistry()
    cwd = tmp_path / "agent-worktree"
    cwd.mkdir()
    sessions.attach(
        cwd=cwd,
        workspace_id=definition.id,
        assignment_id=assignment.id,
    )
    sessions.register(
        session_id="claude-session-1",
        cwd=cwd,
        branch="",
        candidates=[
            SupervisorAssignmentTarget(
                workspace_id=definition.id,
                assignment=assignment,
            )
        ],
    )

    graph = MemoryGraphStore()
    graph.reset(
        version=18,
        artifacts=[
            Artifact(
                id="DECISION-18",
                kind=ArtifactKind.DECISION,
                title="Human escalation approval",
                scopes={SCOPE},
                approval_status=ApprovalStatus.APPROVED,
                authority_role="approve_compliance",
                effective_at=NOW - timedelta(minutes=10),
                source_ref="slack://T001/C001/1720012345.000100",
                attributes={
                    "requirements": {
                        SCOPE: {"human_approval": True},
                    }
                },
            ),
            Artifact(
                id="TASK-ESCALATION",
                kind=ArtifactKind.TASK,
                title="Implement escalation",
                scopes={SCOPE},
            ),
        ],
        edges=[],
    )
    authority = IntentAuthority(
        graph=graph,
        signer=GrantSigner("production-wiring-test-secret", ttl_seconds=3600),
    )
    return repository, sessions, authority


def _scanner(
    *,
    source: StaticEscalationSource,
    acknowledgements: InterruptAcknowledgementReader,
    executor: GrantGatedExecutorPort,
    enabled: bool = True,
    threshold: timedelta = timedelta(minutes=5),
) -> InterruptEscalationScanner:
    return InterruptEscalationScanner(
        source=source,
        acknowledgements=acknowledgements,
        executor=executor,
        threshold=threshold,
        callwright_live_calls_enabled=enabled,
    )


def test_plan_is_deterministic_scope_bound_and_explainable() -> None:
    intent = _intent()

    first = build_interrupt_escalation_plan(intent)
    second = build_interrupt_escalation_plan(intent)

    assert first == second
    assert first.ticket_id == intent.task_id
    assert len(first.actions) == 2
    requirement_action, action = first.actions
    assert requirement_action.scopes == {SCOPE}
    assert requirement_action.attributes == {}
    assert action.scopes == set()
    assert action.attributes["provider"] == "voyagr-callwright"
    assert action.attributes["phone_number_ref"] == "demo-venue"
    rendered = " ".join(action.attributes["instructions"])
    assert intent.interrupt_reason in rendered
    assert "DECISION-18 -> DECISION-17" in rendered
    assert intent.evidence_refs[0] in rendered
    assert "TASK-UNRELATED" in rendered


def test_explanation_evidence_is_required() -> None:
    payload = _intent().model_dump()
    payload["evidence_refs"] = ()

    with pytest.raises(ValidationError, match="at least 1 item"):
        InterruptEscalationIntent.model_validate(payload)


def test_threshold_and_live_feature_gate_make_zero_executor_requests() -> None:
    waiting = AuthorizedInterruptEscalation(
        intent=_intent(interrupted_at=NOW - timedelta(minutes=4)),
        grant_token=SecretStr("unused-waiting-grant"),
    )
    due = AuthorizedInterruptEscalation(
        intent=_intent(),
        grant_token=SecretStr("unused-disabled-grant"),
    )
    executor = FailingExecutor()
    acknowledgements = NeverAcknowledged()

    waiting_result = _scanner(
        source=StaticEscalationSource(waiting),
        acknowledgements=acknowledgements,
        executor=executor,
    ).scan(now=NOW)[0]
    disabled_result = _scanner(
        source=StaticEscalationSource(due),
        acknowledgements=acknowledgements,
        executor=executor,
        enabled=False,
    ).scan(now=NOW)[0]

    assert waiting_result.disposition is InterruptEscalationDisposition.WAITING
    assert disabled_result.disposition is InterruptEscalationDisposition.DISABLED
    assert executor.calls == 0


def test_out_of_scope_and_inactive_assignments_never_escalate() -> None:
    out_of_scope = AuthorizedInterruptEscalation(
        intent=_intent(
            assignment_scopes=frozenset({"unrelated.scope"}),
        ),
        grant_token=SecretStr("unused-out-of-scope-grant"),
    )
    redirected = AuthorizedInterruptEscalation(
        intent=_intent(state=SupervisorAssignmentState.REDIRECTED),
        grant_token=SecretStr("unused-inactive-grant"),
    )
    executor = FailingExecutor()

    out_of_scope_result = _scanner(
        source=StaticEscalationSource(out_of_scope),
        acknowledgements=NeverAcknowledged(),
        executor=executor,
    ).scan(now=NOW)[0]
    redirected_result = _scanner(
        source=StaticEscalationSource(redirected),
        acknowledgements=NeverAcknowledged(),
        executor=executor,
    ).scan(now=NOW)[0]

    assert {
        out_of_scope_result.disposition,
        redirected_result.disposition,
    } == {
        InterruptEscalationDisposition.OUT_OF_SCOPE,
        InterruptEscalationDisposition.INACTIVE,
    }
    assert executor.calls == 0


def test_actual_session_registry_acknowledgement_prevents_call(
    tmp_path: Path,
) -> None:
    intent = _intent()
    candidate = AuthorizedInterruptEscalation(
        intent=intent,
        grant_token=SecretStr("unused-acknowledged-grant"),
    )
    assignment = SupervisorAssignment(
        id=intent.assignment_id,
        task_id=intent.task_id,
        task_title="Escalate an interrupt",
        agent_name="Claude escalation agent",
        runtime_provider=SupervisorRuntimeProvider.CLAUDE_CODE,
        execution_mode=SupervisorExecutionMode.LIVE,
        run_id=intent.run_id,
        state=SupervisorAssignmentState.INTERRUPTED,
        scopes=set(intent.assignment_scopes),
        plan_id="PLAN-ORIGINAL",
        decision_snapshot=intent.decision_snapshot,
        interrupt_reason=intent.interrupt_reason,
        provenance_path=list(intent.provenance_path),
        interrupt_enforced=True,
    )
    registry = ClaudeCodeSessionRegistry()
    registry.attach(
        cwd=tmp_path,
        workspace_id=intent.workspace_id,
        assignment_id=intent.assignment_id,
    )
    registry.register(
        session_id=intent.session_id,
        cwd=tmp_path,
        branch="",
        candidates=[
            SupervisorAssignmentTarget(
                workspace_id=intent.workspace_id,
                assignment=assignment,
            )
        ],
    )
    registry.acknowledge(
        session_id=intent.session_id,
        interrupt_key=intent.interrupt_key,
    )
    executor = FailingExecutor()

    result = _scanner(
        source=StaticEscalationSource(candidate),
        acknowledgements=registry,
        executor=executor,
    ).scan(now=NOW)[0]

    assert result.disposition is InterruptEscalationDisposition.ACKNOWLEDGED
    assert executor.calls == 0


def test_acknowledgement_is_rechecked_immediately_before_executor() -> None:
    candidate = AuthorizedInterruptEscalation(
        intent=_intent(),
        grant_token=SecretStr("unused-racing-acknowledgement-grant"),
    )
    acknowledgements = AcknowledgeOnSecondCheck()
    executor = FailingExecutor()

    result = _scanner(
        source=StaticEscalationSource(candidate),
        acknowledgements=acknowledgements,
        executor=executor,
    ).scan(now=NOW)[0]

    assert result.disposition is InterruptEscalationDisposition.ACKNOWLEDGED
    assert acknowledgements.calls == 2
    assert executor.calls == 0


def test_stale_snapshot_grant_is_rejected_before_callwright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, candidate = _authority_for(_intent())
    executor, fixture = _wire_executor(monkeypatch, authority)
    authority.graph.increment_version()

    result = _scanner(
        source=StaticEscalationSource(candidate),
        acknowledgements=NeverAcknowledged(),
        executor=executor,
    ).scan(now=NOW)[0]

    assert result.disposition is InterruptEscalationDisposition.BLOCKED
    assert result.verification_code is VerificationCode.STALE_SNAPSHOT
    assert fixture.submission_count == 0
    assert executor.calls == 1
    assert candidate.grant_token.get_secret_value() not in result.model_dump_json()


def test_changed_interrupt_evidence_breaks_the_grant_plan_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, candidate = _authority_for(_intent())
    executor, fixture = _wire_executor(monkeypatch, authority)
    changed_intent = candidate.intent.model_copy(
        update={"evidence_refs": ("slack://different-evidence",)},
        deep=True,
    )
    changed_candidate = AuthorizedInterruptEscalation(
        intent=changed_intent,
        grant_token=candidate.grant_token,
    )

    result = _scanner(
        source=StaticEscalationSource(changed_candidate),
        acknowledgements=NeverAcknowledged(),
        executor=executor,
    ).scan(now=NOW)[0]

    assert result.disposition is InterruptEscalationDisposition.BLOCKED
    assert result.verification_code is VerificationCode.PLAN_HASH_MISMATCH
    assert fixture.submission_count == 0


def test_repeated_scans_reuse_executor_receipt_and_dial_at_most_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, candidate = _authority_for(_intent())
    executor, fixture = _wire_executor(monkeypatch, authority)
    scanner = _scanner(
        source=StaticEscalationSource(candidate, candidate),
        acknowledgements=NeverAcknowledged(),
        executor=executor,
    )

    first = scanner.scan(now=NOW)
    second = scanner.scan(now=NOW + timedelta(seconds=30))

    assert len(first) == 1
    assert len(second) == 1
    assert first[0].disposition is InterruptEscalationDisposition.SIMULATED
    assert second[0].disposition is InterruptEscalationDisposition.SIMULATED
    assert first[0].verification_code is VerificationCode.VALID
    assert first[0].call_receipt == second[0].call_receipt
    assert fixture.submission_count == 1
    assert executor.calls == 2
    assert candidate.grant_token.get_secret_value() not in first[0].model_dump_json()


def test_production_composition_authorizes_and_escalates_once_after_threshold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, sessions, authority = _repository_runtime(tmp_path)
    in_process_executor, fixture = _wire_executor(monkeypatch, authority)
    transport = LocalAuthorityExecutorTransport(
        authority=authority,
        executor=in_process_executor,
        context_id="incident-workspace-context",
    )
    config = replace(
        executor_api.settings,
        grant_secret="production-wiring-test-secret",
        callwright_live_calls_enabled=True,
    )
    grant_store = tmp_path / "interrupt-escalation-grants.json"

    def application() -> TestClient:
        app = FastAPI()
        install_api_support(app)
        app.include_router(
            compose_interrupt_escalation_router(
                repository=repository,
                sessions=sessions,
                config=config,
                transport=cast(LiveWorkspaceTransport, transport),
                threshold_seconds=300,
                grant_store_path=grant_store,
                clock=lambda: NOW,
            )
        )
        return TestClient(app)

    headers = {
        INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
            config.grant_secret
        )
    }
    first = application().post(
        "/internal/supervisor/escalations/scan",
        headers=headers,
    )
    # Recompose the production path to prove the persisted grant survives a
    # scanner/service restart and preserves Callwright's authorization ID.
    second = application().post(
        "/internal/supervisor/escalations/scan",
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    first_result = first.json()["results"][0]
    second_result = second.json()["results"][0]
    assert first_result["disposition"] == "simulated"
    assert second_result["disposition"] == "simulated"
    assert first_result["call_receipt"] == second_result["call_receipt"]
    assert first_result["impacted_scopes"] == [SCOPE]
    assert "DECISION-18" in first_result["provenance_path"]
    assert first_result["evidence_refs"] == [
        "slack://T001/C001/1720012345.000100"
    ]
    assert transport.authorize_calls == 1
    assert in_process_executor.calls == 2
    assert fixture.submission_count == 1
    assert grant_store.exists()
    assert grant_store.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("case", "state", "assignment_scopes"),
    [
        (
            "acknowledged",
            SupervisorAssignmentState.INTERRUPTED,
            {SCOPE},
        ),
        (
            "out-of-scope",
            SupervisorAssignmentState.INTERRUPTED,
            {"unrelated.scope"},
        ),
        (
            "current",
            SupervisorAssignmentState.RUNNING,
            {SCOPE},
        ),
    ],
)
def test_production_source_suppresses_ineligible_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
    state: SupervisorAssignmentState,
    assignment_scopes: set[str],
) -> None:
    repository, sessions, authority = _repository_runtime(
        tmp_path,
        state=state,
        assignment_scopes=assignment_scopes,
    )
    if case == "acknowledged":
        enforcement = ClaudeCodeSessionEnforcement(
            registry=sessions,
            assignments=RepositorySupervisorAssignmentGateway(
                repository=repository,
                runtime=ClaudeCodeSupervisorRuntime(),
            ),
        )
        enforcement.acknowledge(session_id="claude-session-1")
    in_process_executor, fixture = _wire_executor(monkeypatch, authority)
    transport = LocalAuthorityExecutorTransport(
        authority=authority,
        executor=in_process_executor,
        context_id="incident-workspace-context",
    )
    config = replace(
        executor_api.settings,
        grant_secret="production-wiring-test-secret",
        callwright_live_calls_enabled=True,
    )
    app = FastAPI()
    install_api_support(app)
    app.include_router(
        compose_interrupt_escalation_router(
            repository=repository,
            sessions=sessions,
            config=config,
            transport=cast(LiveWorkspaceTransport, transport),
            threshold_seconds=300,
            grant_store_path=tmp_path / "grants.json",
            clock=lambda: NOW,
        )
    )

    response = TestClient(app).post(
        "/internal/supervisor/escalations/scan",
        headers={
            INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
                config.grant_secret
            )
        },
    )

    assert response.status_code == 200
    assert response.json()["results"] == []
    assert transport.authorize_calls == 0
    assert in_process_executor.calls == 0
    assert fixture.submission_count == 0
