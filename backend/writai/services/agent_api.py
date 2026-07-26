from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Annotated, Any

from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from writai.auth.hexclave import (
    HexclaveConfigurationError,
    HexclavePermissionChecker,
    HexclavePermissionError,
    HexclaveUserApiKeyIdentityResolver,
)
from writai.config import settings
from writai.domain import (
    AgentPlan,
    AgentRun,
    AuthorizationRequest,
    AuthorizationResult,
    LoopState,
    Verdict,
)
from writai.fixtures import load_graph_fixture
from writai.intake.approval import (
    ApprovalAttemptRequest,
    ApprovalChannel,
    ApprovalCoordinator,
    ApprovalDisposition,
    ApprovalEvidence,
    PendingApproval,
    pending_from_workspace,
)
from writai.intake.crustdata import (
    CrustDataAuthenticationError,
    CrustDataAuthenticationNotConfigured,
    CrustDataPayloadError,
    CrustDataPersonObservationService,
    CrustDataReplayRequest,
    CrustDataWebhookBearerVerifier,
    approval_evidence_from_workspace_records,
)
from writai.intake.replay import (
    CrustDataDeliveryReplayError,
    JsonCrustDataDeliveryReplayStore,
)
from writai.loop.workflow import replan_for_requirements
from writai.notify.email import (
    ApprovalLinkConfigurationError,
    ApprovalLinkStoreError,
    ApprovalRecipientDirectory,
    ChannelApprovalAssertionClaims,
    ChannelApprovalAssertionSigner,
    ExpiredApprovalLink,
    InvalidApprovalLink,
    SqliteChannelApprovalAssertionUseStore,
)
from writai.scenarios.run_models import (
    ScenarioAdvanceRequest,
    ScenarioRunAllRequest,
    ScenarioRunRequest,
)
from writai.scenarios.runner import (
    ScenarioRunConflict,
    ScenarioRunner,
    ScenarioRunNotFound,
)
from writai.services.escalation_api import (
    compose_interrupt_escalation_router,
)
from writai.services.events import EventBroker, snapshot_event, stream_events
from writai.services.supervisor_api import build_supervisor_session_router
from writai.services.support import (
    CORRELATION_ID_HEADER,
    DEMO_FRONTEND_ORIGINS,
    ApiError,
    correlated_payload,
    install_api_support,
    post_model,
    require_internal_service,
)
from writai.workspaces.models import (
    LiveWorkspaceImportRequest,
    WorkspaceApprovalPreview,
    WorkspaceApprovalRequest,
    WorkspaceEmptyRequest,
    WorkspacePlanUpdateRequest,
    WorkspaceProposalRequest,
)
from writai.workspaces.orchestrator import (
    LiveWorkspaceOrchestrator,
    LiveWorkspaceStateConflict,
)
from writai.workspaces.repository import (
    JsonFileLiveWorkspaceRepository,
    LiveWorkspaceConflict,
    LiveWorkspaceNotFound,
)
from writai.workspaces.runtimes.claude_code import (
    ClaudeCodeSupervisorRuntime,
)
from writai.workspaces.session_binding import ClaudeCodeSessionRegistry
from writai.workspaces.session_enforcement import (
    ClaudeCodeSessionEnforcement,
    RepositorySupervisorAssignmentGateway,
)

app = FastAPI(title="writ.ai Agent Service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=DEMO_FRONTEND_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[CORRELATION_ID_HEADER],
)
install_api_support(app)

run_state: AgentRun | None = None
last_authorization: AuthorizationResult | None = None
initial_grant_token: str | None = None
initial_plan: AgentPlan | None = None
event_broker = EventBroker()
workspace_event_brokers: dict[str, EventBroker] = {}
workspace_event_brokers_lock = RLock()
# Process-local clients make the checker's bounded TTL effective across requests.
hexclave_client_lock = RLock()
hexclave_permission_checkers: dict[
    tuple[str, str, bytes, float, str], HexclavePermissionChecker
] = {}
hexclave_identity_resolvers: dict[
    tuple[str, str, bytes], HexclaveUserApiKeyIdentityResolver
] = {}
state_lock = RLock()
decision_approval_lock = RLock()
approval_assertion_store_lock = RLock()
approval_assertion_use_stores: dict[
    Path, SqliteChannelApprovalAssertionUseStore
] = {}
crustdata_replay_store_lock = RLock()
crustdata_replay_stores: dict[Path, JsonCrustDataDeliveryReplayStore] = {}
scenario_runner = ScenarioRunner()
workspace_repository = JsonFileLiveWorkspaceRepository(settings.workspace_store)
supervisor_runtime = ClaudeCodeSupervisorRuntime()
workspace_orchestrator = LiveWorkspaceOrchestrator(
    repository=workspace_repository,
    supervisor_runtime=supervisor_runtime,
)
supervisor_session_registry = ClaudeCodeSessionRegistry()
supervisor_session_enforcement = ClaudeCodeSessionEnforcement(
    registry=supervisor_session_registry,
    assignments=RepositorySupervisorAssignmentGateway(
        repository=workspace_repository,
        runtime=supervisor_runtime,
    ),
)
app.include_router(
    build_supervisor_session_router(supervisor_session_enforcement)
)
app.include_router(
    compose_interrupt_escalation_router(
        repository=workspace_repository,
        sessions=supervisor_session_registry,
    )
)


class EmptyRequest(BaseModel):
    pass


class InternalChannelAssertionRequest(BaseModel):
    """Opaque capability minted only after channel evidence is verified."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    assertion: SecretStr


class _TrustedChannelApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approver_user_id: str
    channel: ApprovalChannel
    evidence_ref: str
    confirmed_proposal_fingerprint: str
    confirmed_proposal_instance_id: str


class ApprovalAttemptEnvelope(BaseModel):
    """Route envelope that lets authenticated missing confirmations be audited."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approval_token: SecretStr
    channel: ApprovalChannel
    evidence_ref: str = Field(min_length=1, max_length=1_000)
    confirmed_proposal_fingerprint: str | None = Field(
        default=None,
        min_length=71,
        max_length=71,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    confirmed_proposal_instance_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
    )


class AuthorityResetState(BaseModel):
    graph_version: str


class _AgentWorkspaceApprovalPort:
    """Apply through the orchestrator while carrying immutable approval evidence."""

    def approve_decision(
        self,
        *,
        pending: PendingApproval,
        evidence: ApprovalEvidence,
    ) -> Mapping[str, Any]:
        workspace = workspace_orchestrator.approve_decision(
            pending.workspace_id,
            pending.decision_id,
            WorkspaceApprovalRequest(
                actor_role=pending.permission_id,
                proposal_fingerprint=pending.proposal_fingerprint,
                approval_evidence=evidence,
                proposal_instance_id=pending.proposal_instance_id,
            ),
        )
        return workspace.model_dump(mode="json")


def _workspace_permission_checker(
    workspace: Mapping[str, Any],
) -> HexclavePermissionChecker:
    binding = workspace.get("slack_binding")
    bound_team_id = (
        binding.get("hexclave_team_id")
        if isinstance(binding, Mapping)
        else None
    )
    team_id = (
        bound_team_id
        if isinstance(bound_team_id, str)
        else settings.hexclave_team_id or ""
    )
    secret_fingerprint = sha256(
        (settings.hexclave_secret_key or "").encode()
    ).digest()
    cache_key = (
        settings.hexclave_api_url,
        settings.hexclave_project_id or "",
        secret_fingerprint,
        settings.hexclave_permission_cache_ttl_seconds,
        team_id,
    )
    with hexclave_client_lock:
        checker = hexclave_permission_checkers.get(cache_key)
        if checker is None:
            checker = HexclavePermissionChecker(
                project_id=settings.hexclave_project_id or "",
                secret_key=settings.hexclave_secret_key or "",
                team_id=team_id,
                api_url=settings.hexclave_api_url,
                cache_ttl_seconds=settings.hexclave_permission_cache_ttl_seconds,
            )
            hexclave_permission_checkers[cache_key] = checker
        return checker


def _require_workspace_slack_identity(
    workspace: Mapping[str, Any],
    *,
    workspace_id: str,
    approver_user_id: str,
) -> None:
    binding = workspace.get("slack_binding")
    if (
        not isinstance(binding, Mapping)
        or binding.get("workspace_id") != workspace_id
        or not isinstance(binding.get("hexclave_team_id"), str)
        or not binding["hexclave_team_id"].strip()
    ):
        raise ApiError(
            status_code=409,
            code="SLACK_WORKSPACE_BINDING_MISSING",
            message="The Workspace has no valid Slack approval binding.",
        )
    identities = binding.get("user_identities")
    visible_match = isinstance(identities, list) and any(
        isinstance(item, Mapping)
        and item.get("hexclave_user_id") == approver_user_id
        for item in identities
    )
    private_match = (
        workspace_orchestrator.is_slack_authority_user_bound(
            workspace_id,
            authority_user_id=approver_user_id,
        )
        if hasattr(
            workspace_orchestrator,
            "is_slack_authority_user_bound",
        )
        else False
    )
    if not visible_match and not private_match:
        raise ApiError(
            status_code=403,
            code="SLACK_IDENTITY_BINDING_NOT_FOUND",
            message="The derived Slack approver is not bound to this Workspace.",
        )


def _approval_identity_resolver() -> HexclaveUserApiKeyIdentityResolver:
    secret_fingerprint = sha256(
        (settings.hexclave_secret_key or "").encode()
    ).digest()
    cache_key = (
        settings.hexclave_api_url,
        settings.hexclave_project_id or "",
        secret_fingerprint,
    )
    with hexclave_client_lock:
        resolver = hexclave_identity_resolvers.get(cache_key)
        if resolver is None:
            resolver = HexclaveUserApiKeyIdentityResolver(
                project_id=settings.hexclave_project_id or "",
                secret_key=settings.hexclave_secret_key or "",
                api_url=settings.hexclave_api_url,
            )
            hexclave_identity_resolvers[cache_key] = resolver
        return resolver


def _approved_attempt_matches(
    workspace: Mapping[str, Any],
    *,
    decision_id: str,
    fingerprint: str | None,
    proposal_instance_id: str | None,
) -> bool:
    approved = workspace.get("approved_mutations")
    if not isinstance(approved, list):
        return False
    for item in approved:
        if not isinstance(item, Mapping):
            continue
        mutation = item.get("mutation")
        evidence = item.get("approval_evidence")
        if not isinstance(mutation, Mapping) or not isinstance(evidence, Mapping):
            continue
        decision = mutation.get("decision")
        if not isinstance(decision, Mapping) or decision.get("id") != decision_id:
            continue
        return (
            evidence.get("confirmed_proposal_fingerprint") == fingerprint
            and evidence.get("confirmed_proposal_instance_id")
            == proposal_instance_id
        )
    return False


def _approved_baseline_attempt_matches(
    workspace: Mapping[str, Any],
    *,
    fingerprint: str | None,
    proposal_instance_id: str | None,
) -> bool:
    evidence = workspace.get("baseline_approval_evidence")
    if not isinstance(evidence, Mapping):
        return False
    return (
        evidence.get("confirmed_proposal_fingerprint") == fingerprint
        and evidence.get("confirmed_proposal_instance_id")
        == proposal_instance_id
    )


def _baseline_required_permission(workspace: Mapping[str, Any]) -> tuple[str, str]:
    decision = workspace.get("baseline_decision")
    policy = workspace.get("authority_policy")
    if not isinstance(decision, Mapping) or not isinstance(policy, Mapping):
        raise ApiError(
            status_code=409,
            code="BASELINE_APPROVAL_METADATA_INVALID",
            message="The Workspace baseline has no valid authority metadata.",
        )
    decision_id = decision.get("id")
    permission_id = decision.get("authority_role")
    raw_scopes = decision.get("scopes")
    if (
        not isinstance(decision_id, str)
        or not decision_id.strip()
        or not isinstance(permission_id, str)
        or not permission_id.strip()
        or not isinstance(raw_scopes, list)
        or not raw_scopes
    ):
        raise ApiError(
            status_code=409,
            code="BASELINE_APPROVAL_METADATA_INVALID",
            message="The Workspace baseline has no valid authority metadata.",
        )
    scopes = {
        scope.strip()
        for scope in raw_scopes
        if isinstance(scope, str) and scope.strip()
    }
    if len(scopes) != len(raw_scopes) or any(
        not isinstance(policy.get(scope), list)
        or permission_id not in policy[scope]
        for scope in scopes
    ):
        raise ApiError(
            status_code=409,
            code="BASELINE_APPROVAL_METADATA_INVALID",
            message="The baseline permission does not govern every affected scope.",
        )
    return decision_id.strip(), permission_id.strip()


def _decision_permission_id(
    workspace: Mapping[str, Any],
    *,
    decision_id: str,
    proposal_fingerprint: str | None,
    proposal_instance_id: str | None,
) -> str | None:
    history = workspace.get("history")
    if (
        proposal_fingerprint is not None
        and proposal_instance_id is not None
        and isinstance(history, list)
    ):
        for event in reversed(history):
            if not isinstance(event, Mapping):
                continue
            data = event.get("data")
            if (
                event.get("event_type") == "decision.proposed"
                and isinstance(data, Mapping)
                and data.get("decision_id") == decision_id
                and data.get("proposal_fingerprint") == proposal_fingerprint
                and data.get("proposal_instance_id") == proposal_instance_id
            ):
                permission_id = data.get("permission_id")
                if isinstance(permission_id, str) and permission_id.strip():
                    return permission_id.strip()

    pending = workspace.get("pending_mutation")
    pending_matches = (
        proposal_fingerprint is None
        or proposal_instance_id is None
        or (
            workspace.get("pending_proposal_fingerprint")
            == proposal_fingerprint
            and workspace.get("pending_proposal_instance_id")
            == proposal_instance_id
        )
    )
    candidates: list[tuple[object, Mapping[str, Any] | None]] = [
        (pending if pending_matches else None, None),
        (workspace.get("baseline_decision"), None),
    ]
    approved = workspace.get("approved_mutations")
    if isinstance(approved, list):
        candidates.extend(
            (
                item.get("mutation"),
                (
                    item.get("approval_evidence")
                    if isinstance(item.get("approval_evidence"), Mapping)
                    else None
                ),
            )
            for item in approved
            if isinstance(item, Mapping)
        )
    for candidate, evidence in candidates:
        if not isinstance(candidate, Mapping):
            continue
        decision = candidate.get("decision", candidate)
        if not isinstance(decision, Mapping) or decision.get("id") != decision_id:
            continue
        if (
            evidence is not None
            and proposal_fingerprint is not None
            and proposal_instance_id is not None
            and (
                evidence.get("confirmed_proposal_fingerprint")
                != proposal_fingerprint
                or evidence.get("confirmed_proposal_instance_id")
                != proposal_instance_id
            )
        ):
            continue
        permission_id = decision.get("authority_role")
        if isinstance(permission_id, str) and permission_id.strip():
            return permission_id.strip()
    return None


def _record_approval_rejection(
    *,
    workspace_id: str,
    decision_id: str,
    disposition: ApprovalDisposition,
    approver_user_id: str,
    permission_id: str,
    channel: ApprovalChannel,
    evidence_ref: str,
    confirmed_proposal_fingerprint: str | None,
    confirmed_proposal_instance_id: str | None,
    detail: str,
) -> None:
    workspace = workspace_orchestrator.record_approval_rejection(
        workspace_id,
        decision_id=decision_id,
        disposition=disposition.value,
        approver_user_id=approver_user_id,
        permission_id=permission_id,
        approval_channel=channel.value,
        approval_evidence_ref=evidence_ref,
        confirmed_proposal_fingerprint=confirmed_proposal_fingerprint,
        confirmed_proposal_instance_id=confirmed_proposal_instance_id,
        detail=detail,
    )
    _publish_workspace(workspace_id, workspace)


def _authorize(run: AgentRun) -> AuthorizationResult:
    return post_model(
        url=f"{settings.authority_url}/authorize",
        payload=AuthorizationRequest(run_id=run.run_id, task_id=run.ticket_id, plan=run.plan),
        response_model=AuthorizationResult,
        upstream_name="Intent authority",
        upstream_code="AUTHORITY",
        timeout_seconds=settings.service_timeout_seconds,
    )


def _apply_result(run: AgentRun, result: AuthorizationResult) -> None:
    run.graph_snapshot = result.graph_version
    run.grant_token = result.grant.token if result.grant else None
    if result.verdict is Verdict.ALLOW:
        run.state = LoopState.ACT
    elif result.verdict is Verdict.REPLAN:
        run.state = LoopState.REPLAN
    elif result.verdict is Verdict.BLOCK:
        run.state = LoopState.BLOCKED
    else:
        run.state = LoopState.HUMAN_REVIEW


def _reset_local_state() -> None:
    global run_state, last_authorization, initial_grant_token, initial_plan
    with state_lock:
        run_state = None
        last_authorization = None
        initial_grant_token = None
        initial_plan = None


def _state_body() -> dict[str, object]:
    with state_lock:
        return {
            "run": run_state.model_dump(mode="json") if run_state else None,
            "last_authorization": (
                last_authorization.model_dump(mode="json") if last_authorization else None
            ),
            "initial_grant_token": initial_grant_token,
            "initial_plan": initial_plan.model_dump(mode="json") if initial_plan else None,
        }


def _publish_state(event_type: str) -> None:
    with state_lock:
        event_broker.publish(event_type, _state_body())


def _workspace_event_broker(workspace_id: str) -> EventBroker:
    with workspace_event_brokers_lock:
        broker = workspace_event_brokers.get(workspace_id)
        if broker is None:
            broker = EventBroker()
            workspace_event_brokers[workspace_id] = broker
        return broker


def _redact_workspace_event_value(value: Any, *, key: str = "") -> Any:
    normalized_key = key.casefold()
    if normalized_key in {"token", "signed_token", "grant_token", "signature"}:
        return "[REDACTED]"
    if "token" in normalized_key:
        return "[REDACTED]"
    if isinstance(value, BaseModel):
        return _redact_workspace_event_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_workspace_event_value(
                child_value,
                key=str(child_key),
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_workspace_event_value(item) for item in value]
    return value


def _workspace_event_data(
    workspace: BaseModel | Mapping[str, Any],
) -> dict[str, Any]:
    redacted = _redact_workspace_event_value(workspace)
    if not isinstance(redacted, dict):
        raise TypeError("workspace event data must be an object")
    return redacted


def _publish_workspace(
    workspace_id: str,
    workspace: BaseModel | Mapping[str, Any],
) -> None:
    with workspace_event_brokers_lock:
        _workspace_event_broker(workspace_id).publish(
            "live-workspace.supervisor.changed",
            _workspace_event_data(workspace),
        )


def _workspace_response(
    workspace_id: str,
    workspace: BaseModel | Mapping[str, Any],
) -> dict[str, Any]:
    _publish_workspace(workspace_id, workspace)
    return correlated_payload(workspace)


def _crustdata_replay_store() -> JsonCrustDataDeliveryReplayStore:
    workspace_path = Path(settings.workspace_store).expanduser()
    suffix = workspace_path.suffix or ".json"
    path = workspace_path.with_name(
        f"{workspace_path.stem}-crustdata-deliveries{suffix}"
    )
    with crustdata_replay_store_lock:
        store = crustdata_replay_stores.get(path)
        if store is None:
            store = JsonCrustDataDeliveryReplayStore(path)
            crustdata_replay_stores[path] = store
        return store


def _require_demo_reset() -> None:
    if not settings.demo_reset_enabled:
        raise ApiError(
            status_code=403,
            code="DEMO_RESET_DISABLED",
            message="Demo reset is disabled in this environment.",
        )


@app.get("/health")
def health() -> dict[str, str]:
    return correlated_payload({"status": "ok"})


@app.post("/demo/reset")
def reset_demo() -> dict[str, object]:
    _require_demo_reset()
    with state_lock:
        _reset_local_state()
        _publish_state("loop.state.reset")
    return correlated_payload({"reset": True})


@app.post("/demo/reset-all")
def reset_all() -> dict[str, object]:
    """Reset authority first, then clear the agent only after graph-v17 is confirmed."""

    _require_demo_reset()
    with state_lock:
        authority_state = post_model(
            url=f"{settings.authority_url}/graph/reset",
            payload=EmptyRequest(),
            response_model=AuthorityResetState,
            upstream_name="Intent authority",
            upstream_code="AUTHORITY",
            timeout_seconds=settings.service_timeout_seconds,
        )
        if authority_state.graph_version != "graph-v17":
            raise ApiError(
                status_code=502,
                code="AUTHORITY_RESET_INVALID",
                message="Intent authority reset to an unexpected graph version.",
            )
        _reset_local_state()
        _publish_state("loop.state.reset")
    return correlated_payload(
        {
            "reset": True,
            "graph_version": authority_state.graph_version,
        }
    )


@app.post("/demo/start")
def start_demo() -> dict[str, object]:
    global run_state, last_authorization, initial_grant_token, initial_plan
    with state_lock:
        _, _, _, fixture_run = load_graph_fixture()
        run_state = fixture_run.model_copy(deep=True)
        initial_plan = run_state.plan.model_copy(deep=True)
        run_state.state = LoopState.VERIFY
        last_authorization = _authorize(run_state)
        _apply_result(run_state, last_authorization)
        initial_grant_token = run_state.grant_token
        run_state.history.append(f"Initial verification: {last_authorization.verdict.value}")
        _publish_state("loop.state.changed")
        return state_payload()


@app.post("/demo/tests-pass")
def tests_pass() -> dict[str, object]:
    with state_lock:
        if run_state is None:
            raise ApiError(
                status_code=409,
                code="DEMO_NOT_STARTED",
                message="Start the demo first.",
            )
        if (
            last_authorization is None
            or last_authorization.verdict is not Verdict.ALLOW
            or run_state.grant_token is None
        ):
            raise ApiError(
                status_code=409,
                code="AUTHORIZATION_REQUIRED",
                message="A current ALLOW authorization with a grant is required.",
            )
        run_state.tests_passed = True
        run_state.state = LoopState.ACT
        run_state.history.append("Implementation complete; tests passed.")
        _publish_state("loop.state.changed")
        return state_payload()


@app.post("/demo/recheck")
def recheck() -> dict[str, object]:
    global last_authorization
    with state_lock:
        if run_state is None:
            raise ApiError(
                status_code=409,
                code="DEMO_NOT_STARTED",
                message="Start the demo first.",
            )
        run_state.state = LoopState.VERIFY
        last_authorization = _authorize(run_state)
        _apply_result(run_state, last_authorization)
        run_state.history.append(f"Reauthorization: {last_authorization.verdict.value}")
        _publish_state("loop.state.changed")
        return state_payload()


@app.post("/demo/replan")
def replan() -> dict[str, object]:
    global last_authorization
    with state_lock:
        if run_state is None or last_authorization is None:
            raise ApiError(
                status_code=409,
                code="RECHECK_REQUIRED",
                message="Recheck the current plan first.",
            )
        if last_authorization.verdict is not Verdict.REPLAN:
            raise ApiError(
                status_code=409,
                code="REPLAN_NOT_AUTHORIZED",
                message="The current authority verdict is not REPLAN.",
            )
        run_state.plan = replan_for_requirements(
            run_state.plan, last_authorization.current_requirements
        )
        run_state.state = LoopState.VERIFY
        last_authorization = _authorize(run_state)
        _apply_result(run_state, last_authorization)
        run_state.history.append(
            f"Corrected plan verification: {last_authorization.verdict.value}"
        )
        _publish_state("loop.state.changed")
        return state_payload()


@app.get("/demo/state")
def state_payload() -> dict[str, object]:
    with state_lock:
        state = _state_body()
    return correlated_payload(state)


@app.get("/events")
def events(request: Request) -> StreamingResponse:
    with state_lock:
        initial = snapshot_event(
            sequence=event_broker.current_sequence,
            event_type="loop.state.snapshot",
            data=_state_body(),
        )
    return StreamingResponse(
        stream_events(request=request, broker=event_broker, initial=initial),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/intake/crustdata/person/replay")
def replay_crustdata_person_delivery(
    replay: CrustDataReplayRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    """Replay one authenticated watcher fixture into human review flags."""

    try:
        CrustDataWebhookBearerVerifier(
            expected_bearer=settings.crustdata_webhook_bearer or ""
        ).require(authorization)
    except CrustDataAuthenticationNotConfigured as exc:
        raise ApiError(
            status_code=503,
            code="CRUSTDATA_AUTHENTICATION_NOT_CONFIGURED",
            message=str(exc),
        ) from exc
    except CrustDataAuthenticationError as exc:
        raise ApiError(
            status_code=401,
            code="CRUSTDATA_AUTHENTICATION_FAILED",
            message=str(exc),
        ) from exc

    try:
        result = CrustDataPersonObservationService(
            replay_store=_crustdata_replay_store(),
            expected_api_version=settings.crustdata_api_version,
        ).process(
            replay,
            approval_evidence=approval_evidence_from_workspace_records(
                workspace_repository.list()
            ),
        )
    except CrustDataPayloadError as exc:
        raise ApiError(
            status_code=422,
            code="INVALID_CRUSTDATA_PAYLOAD",
            message=str(exc),
        ) from exc
    except CrustDataDeliveryReplayError as exc:
        raise ApiError(
            status_code=503,
            code="CRUSTDATA_REPLAY_LEDGER_UNAVAILABLE",
            message=str(exc),
            retryable=True,
        ) from exc

    if not result.duplicate:
        event_broker.publish(
            "crustdata.person-review.flagged"
            if result.flags
            else "crustdata.person-review.observed",
            result,
        )
    return correlated_payload(result)


def _scenario_api_error(exc: Exception) -> ApiError:
    if isinstance(exc, (KeyError, ScenarioRunNotFound)):
        return ApiError(
            status_code=404,
            code="SCENARIO_NOT_FOUND",
            message=str(exc).strip("'"),
        )
    if isinstance(exc, ScenarioRunConflict):
        return ApiError(
            status_code=409,
            code="SCENARIO_RUN_CONFLICT",
            message=str(exc),
        )
    return ApiError(
        status_code=500,
        code="SCENARIO_RUN_FAILED",
        message="The scenario runner could not complete the request.",
    )


@app.get("/scenario-lab/scenarios")
def scenario_catalog() -> dict[str, object]:
    return correlated_payload(scenario_runner.catalog())


@app.get("/scenario-lab/scenarios/{scenario_id}")
def scenario_definition(scenario_id: str) -> dict[str, object]:
    try:
        definition = scenario_runner.definition(scenario_id)
    except KeyError as exc:
        raise _scenario_api_error(exc) from exc
    return correlated_payload(definition)


@app.post("/scenario-lab/runs", status_code=201)
def scenario_start(request: ScenarioRunRequest) -> dict[str, object]:
    try:
        run = scenario_runner.start(request.scenario_id)
    except (KeyError, ScenarioRunConflict) as exc:
        raise _scenario_api_error(exc) from exc
    return correlated_payload(run)


def _workspace_api_error(exc: Exception) -> ApiError:
    if isinstance(exc, LiveWorkspaceNotFound):
        return ApiError(
            status_code=404,
            code="LIVE_WORKSPACE_NOT_FOUND",
            message=f"Unknown Live Workspace: {exc.workspace_id}",
        )
    if isinstance(exc, (LiveWorkspaceConflict, LiveWorkspaceStateConflict)):
        return ApiError(
            status_code=409,
            code="LIVE_WORKSPACE_CONFLICT",
            message=str(exc),
        )
    return ApiError(
        status_code=500,
        code="LIVE_WORKSPACE_FAILED",
        message="The Live Workspace operation could not be completed.",
    )


@app.post("/live-workspaces/import", status_code=201)
def import_live_workspace(
    request: LiveWorkspaceImportRequest,
) -> dict[str, object]:
    try:
        workspace = workspace_orchestrator.import_workspace(request)
    except (LiveWorkspaceConflict, LiveWorkspaceStateConflict) as exc:
        raise _workspace_api_error(exc) from exc
    return _workspace_response(workspace.id, workspace)


@app.get("/live-workspaces")
def list_live_workspaces() -> dict[str, object]:
    return correlated_payload(workspace_orchestrator.list())


@app.get("/live-workspaces/{workspace_id}")
def get_live_workspace(workspace_id: str) -> dict[str, object]:
    try:
        workspace = workspace_orchestrator.get(workspace_id)
    except LiveWorkspaceNotFound as exc:
        raise _workspace_api_error(exc) from exc
    return correlated_payload(workspace)


@app.get("/live-workspaces/{workspace_id}/events")
def live_workspace_events(
    workspace_id: str,
    request: Request,
) -> StreamingResponse:
    with workspace_event_brokers_lock:
        try:
            workspace = workspace_orchestrator.get(workspace_id)
        except LiveWorkspaceNotFound as exc:
            raise _workspace_api_error(exc) from exc
        broker = _workspace_event_broker(workspace_id)
        initial = snapshot_event(
            sequence=broker.current_sequence,
            event_type="live-workspace.supervisor.snapshot",
            data=_workspace_event_data(workspace),
        )
    return StreamingResponse(
        stream_events(request=request, broker=broker, initial=initial),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/live-workspaces/{workspace_id}/baseline/approve")
def approve_live_workspace_baseline(
    workspace_id: str,
    request: ApprovalAttemptEnvelope,
) -> dict[str, object]:
    if request.channel not in {
        ApprovalChannel.CLI,
        ApprovalChannel.WORKSPACE_UI,
    }:
        raise ApiError(
            status_code=400,
            code="INVALID_APPROVAL_CHANNEL",
            message=(
                "Slack and notification approvals must use their signed "
                "channel-specific intake route."
            ),
        )
    try:
        approver_user_id = _approval_identity_resolver().resolve_user_id(
            approval_token=request.approval_token.get_secret_value()
        )
    except HexclaveConfigurationError as exc:
        raise ApiError(
            status_code=503,
            code="HEXCLAVE_NOT_CONFIGURED",
            message=str(exc),
        ) from exc
    except HexclavePermissionError as exc:
        raise ApiError(
            status_code=401,
            code="APPROVAL_AUTHENTICATION_FAILED",
            message="Approval authentication failed.",
        ) from exc

    with decision_approval_lock:
        try:
            current = workspace_orchestrator.get(workspace_id)
        except LiveWorkspaceNotFound as exc:
            raise _workspace_api_error(exc) from exc
        current_body = current.model_dump(mode="json")
        decision_id, permission_id = _baseline_required_permission(current_body)
        if _approved_baseline_attempt_matches(
            current_body,
            fingerprint=request.confirmed_proposal_fingerprint,
            proposal_instance_id=request.confirmed_proposal_instance_id,
        ):
            return _workspace_response(workspace_id, current)
        if current.baseline_approved:
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=ApprovalDisposition.STALE_CONFIRMATION,
                approver_user_id=approver_user_id,
                permission_id=permission_id,
                channel=request.channel,
                evidence_ref=request.evidence_ref,
                confirmed_proposal_fingerprint=(
                    request.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    request.confirmed_proposal_instance_id
                ),
                detail="The Workspace baseline is no longer awaiting approval.",
            )
            raise ApiError(
                status_code=409,
                code="BASELINE_NOT_AWAITING_APPROVAL",
                message="The Workspace baseline is not awaiting approval.",
            )

        expected_fingerprint = current.baseline_proposal_fingerprint
        expected_instance_id = current.baseline_proposal_instance_id
        if (
            request.confirmed_proposal_fingerprint is None
            or request.confirmed_proposal_instance_id is None
        ):
            detail = (
                "Approval did not confirm the baseline fingerprint and "
                "proposal instance."
            )
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=ApprovalDisposition.MISSING_CONFIRMATION,
                approver_user_id=approver_user_id,
                permission_id=permission_id,
                channel=request.channel,
                evidence_ref=request.evidence_ref,
                confirmed_proposal_fingerprint=(
                    request.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    request.confirmed_proposal_instance_id
                ),
                detail=detail,
            )
            raise ApiError(
                status_code=409,
                code="MISSING_BASELINE_CONFIRMATION",
                message=detail,
            )
        attempt = ApprovalAttemptRequest.model_validate(
            request.model_dump(mode="python")
        )
        if (
            attempt.confirmed_proposal_fingerprint != expected_fingerprint
            or expected_instance_id is None
            or attempt.confirmed_proposal_instance_id
            != expected_instance_id
        ):
            detail = (
                "The confirmed baseline no longer matches the pending proposal."
            )
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=ApprovalDisposition.STALE_CONFIRMATION,
                approver_user_id=approver_user_id,
                permission_id=permission_id,
                channel=attempt.channel,
                evidence_ref=attempt.evidence_ref,
                confirmed_proposal_fingerprint=(
                    attempt.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    attempt.confirmed_proposal_instance_id
                ),
                detail=detail,
            )
            raise ApiError(
                status_code=409,
                code="STALE_BASELINE_CONFIRMATION",
                message=detail,
            )
        try:
            checker = _workspace_permission_checker(current_body)
        except HexclaveConfigurationError as exc:
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=(
                    ApprovalDisposition.PERMISSION_CHECK_UNAVAILABLE
                ),
                approver_user_id=approver_user_id,
                permission_id=permission_id,
                channel=attempt.channel,
                evidence_ref=attempt.evidence_ref,
                confirmed_proposal_fingerprint=(
                    attempt.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    attempt.confirmed_proposal_instance_id
                ),
                detail="Hexclave permission verification is unavailable.",
            )
            raise ApiError(
                status_code=503,
                code="HEXCLAVE_NOT_CONFIGURED",
                message=str(exc),
            ) from exc
        try:
            allowed = checker.has_permission(
                user_id=approver_user_id,
                permission_id=permission_id,
            )
        except HexclavePermissionError as exc:
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=(
                    ApprovalDisposition.PERMISSION_CHECK_UNAVAILABLE
                ),
                approver_user_id=approver_user_id,
                permission_id=permission_id,
                channel=attempt.channel,
                evidence_ref=attempt.evidence_ref,
                confirmed_proposal_fingerprint=(
                    attempt.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    attempt.confirmed_proposal_instance_id
                ),
                detail="Hexclave permission verification is unavailable.",
            )
            raise ApiError(
                status_code=503,
                code="PERMISSION_CHECK_UNAVAILABLE",
                message="Hexclave permission verification is unavailable.",
                retryable=True,
            ) from exc
        if not allowed:
            detail = "The authenticated user cannot approve this baseline."
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=(
                    ApprovalDisposition.IGNORED_NOT_AUTHORIZED
                ),
                approver_user_id=approver_user_id,
                permission_id=permission_id,
                channel=attempt.channel,
                evidence_ref=attempt.evidence_ref,
                confirmed_proposal_fingerprint=(
                    attempt.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    attempt.confirmed_proposal_instance_id
                ),
                detail=detail,
            )
            raise ApiError(
                status_code=403,
                code="APPROVER_NOT_AUTHORIZED",
                message=detail,
            )

        evidence = ApprovalEvidence(
            workspace_id=workspace_id,
            decision_id=decision_id,
            approver_user_id=approver_user_id,
            permission_id=permission_id,
            channel=attempt.channel,
            evidence_ref=attempt.evidence_ref,
            approved_at=datetime.now(UTC),
            confirmed_proposal_fingerprint=expected_fingerprint,
            confirmed_proposal_instance_id=expected_instance_id,
        )
        try:
            workspace = workspace_orchestrator.approve_baseline(
                workspace_id,
                WorkspaceApprovalRequest(
                    actor_role=permission_id,
                    proposal_fingerprint=expected_fingerprint,
                    approval_evidence=evidence,
                    proposal_instance_id=expected_instance_id,
                ),
            )
        except LiveWorkspaceStateConflict as exc:
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=ApprovalDisposition.STALE_CONFIRMATION,
                approver_user_id=approver_user_id,
                permission_id=permission_id,
                channel=attempt.channel,
                evidence_ref=attempt.evidence_ref,
                confirmed_proposal_fingerprint=(
                    attempt.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    attempt.confirmed_proposal_instance_id
                ),
                detail=str(exc),
            )
            raise _workspace_api_error(exc) from exc
        except LiveWorkspaceNotFound as exc:
            raise _workspace_api_error(exc) from exc
        return _workspace_response(workspace_id, workspace)


@app.post("/live-workspaces/{workspace_id}/authorize")
def authorize_live_workspace(
    workspace_id: str,
    _request: WorkspaceEmptyRequest,
) -> dict[str, object]:
    try:
        workspace = workspace_orchestrator.authorize(workspace_id)
    except (LiveWorkspaceNotFound, LiveWorkspaceStateConflict) as exc:
        raise _workspace_api_error(exc) from exc
    return _workspace_response(workspace_id, workspace)


@app.post("/live-workspaces/{workspace_id}/decisions/propose")
def propose_live_workspace_decision(
    workspace_id: str,
    request: WorkspaceProposalRequest,
) -> dict[str, object]:
    try:
        workspace = workspace_orchestrator.propose_decision(workspace_id, request)
    except (LiveWorkspaceNotFound, LiveWorkspaceStateConflict) as exc:
        raise _workspace_api_error(exc) from exc
    return _workspace_response(workspace_id, workspace)


@app.get(
    "/live-workspaces/{workspace_id}/decisions/{decision_id}/preview",
    response_model=None,
)
def preview_live_workspace_decision(
    workspace_id: str,
    decision_id: str,
) -> dict[str, object]:
    try:
        preview: WorkspaceApprovalPreview = (
            workspace_orchestrator.preview_decision(
                workspace_id,
                decision_id,
            )
        )
    except (LiveWorkspaceNotFound, LiveWorkspaceStateConflict) as exc:
        raise _workspace_api_error(exc) from exc
    return correlated_payload(preview)


@app.post(
    "/internal/live-workspaces/{workspace_id}/decisions/"
    "{decision_id}/approve-from-slack"
)
def approve_live_workspace_decision_from_slack(
    workspace_id: str,
    decision_id: str,
    approval: InternalChannelAssertionRequest,
    request: Request,
) -> dict[str, object]:
    """Apply a verified Slack reaction through the shared coordinator."""

    require_internal_service(request, secret=settings.grant_secret)
    claims = _decode_channel_approval_assertion(
        approval=approval,
        workspace_id=workspace_id,
        decision_id=decision_id,
    )
    if claims.channel is not ApprovalChannel.SLACK_REACTION:
        raise ApiError(
            status_code=401,
            code="INVALID_APPROVAL_ASSERTION",
            message="The channel approval assertion targets another channel.",
        )
    _consume_channel_approval_assertion(claims)
    return _approve_live_workspace_decision_from_trusted_channel(
        workspace_id=workspace_id,
        decision_id=decision_id,
        approval=_TrustedChannelApproval(
            approver_user_id=claims.approver_user_id,
            channel=claims.channel,
            evidence_ref=claims.evidence_ref,
            confirmed_proposal_fingerprint=claims.proposal_fingerprint,
            confirmed_proposal_instance_id=claims.proposal_instance_id,
        ),
    )


@app.post(
    "/internal/live-workspaces/{workspace_id}/decisions/"
    "{decision_id}/approve-from-notification"
)
def approve_live_workspace_decision_from_notification(
    workspace_id: str,
    decision_id: str,
    approval: InternalChannelAssertionRequest,
    request: Request,
) -> dict[str, object]:
    """Verify an opaque forwarding assertion before reaching mutation code."""

    require_internal_service(request, secret=settings.grant_secret)
    claims = _decode_channel_approval_assertion(
        approval=approval,
        workspace_id=workspace_id,
        decision_id=decision_id,
    )
    if claims.channel not in {
        ApprovalChannel.PUSH,
        ApprovalChannel.EMAIL,
    }:
        raise ApiError(
            status_code=401,
            code="INVALID_APPROVAL_ASSERTION",
            message="The channel approval assertion targets another channel.",
        )
    try:
        ApprovalRecipientDirectory.from_json(
            settings.approval_recipient_bindings,
            ntfy_server=settings.ntfy_server,
        ).verify_assertion(claims)
    except InvalidApprovalLink as exc:
        raise ApiError(
            status_code=401,
            code="INVALID_APPROVAL_ASSERTION",
            message=str(exc),
        ) from exc
    except ApprovalLinkConfigurationError as exc:
        raise ApiError(
            status_code=503,
            code="APPROVAL_ASSERTIONS_NOT_CONFIGURED",
            message=str(exc),
        ) from exc
    _consume_channel_approval_assertion(claims)
    return _approve_live_workspace_decision_from_trusted_channel(
        workspace_id=workspace_id,
        decision_id=decision_id,
        approval=_TrustedChannelApproval(
            approver_user_id=claims.approver_user_id,
            channel=claims.channel,
            evidence_ref=claims.evidence_ref,
            confirmed_proposal_fingerprint=claims.proposal_fingerprint,
            confirmed_proposal_instance_id=claims.proposal_instance_id,
        ),
    )


def _decode_channel_approval_assertion(
    *,
    approval: InternalChannelAssertionRequest,
    workspace_id: str,
    decision_id: str,
) -> ChannelApprovalAssertionClaims:
    secret = settings.approval_link_secret or ""
    if not secret or secret == settings.grant_secret:
        raise ApiError(
            status_code=503,
            code="APPROVAL_ASSERTIONS_NOT_CONFIGURED",
            message="Channel approval assertions are not safely configured.",
        )
    try:
        return ChannelApprovalAssertionSigner(secret=secret).decode(
            approval.assertion.get_secret_value(),
            expected_workspace_id=workspace_id,
            expected_decision_id=decision_id,
        )
    except ExpiredApprovalLink as exc:
        raise ApiError(
            status_code=401,
            code="APPROVAL_ASSERTION_EXPIRED",
            message=str(exc),
        ) from exc
    except InvalidApprovalLink as exc:
        raise ApiError(
            status_code=401,
            code="INVALID_APPROVAL_ASSERTION",
            message=str(exc),
        ) from exc
    except ApprovalLinkConfigurationError as exc:
        raise ApiError(
            status_code=503,
            code="APPROVAL_ASSERTIONS_NOT_CONFIGURED",
            message=str(exc),
        ) from exc


def _approval_assertion_use_store(
) -> SqliteChannelApprovalAssertionUseStore:
    path = Path(settings.approval_assertion_store).expanduser()
    with approval_assertion_store_lock:
        store = approval_assertion_use_stores.get(path)
        if store is None:
            store = SqliteChannelApprovalAssertionUseStore(path)
            approval_assertion_use_stores[path] = store
        return store


def _consume_channel_approval_assertion(
    claims: ChannelApprovalAssertionClaims,
) -> None:
    try:
        consumed = _approval_assertion_use_store().consume(
            claims,
            consumed_at=datetime.now(UTC),
        )
    except ApprovalLinkStoreError as exc:
        raise ApiError(
            status_code=503,
            code="APPROVAL_ASSERTION_LEDGER_UNAVAILABLE",
            message=str(exc),
            retryable=True,
        ) from exc
    if not consumed:
        raise ApiError(
            status_code=409,
            code="APPROVAL_ASSERTION_ALREADY_USED",
            message="The channel approval assertion has already been used.",
        )


def _approve_live_workspace_decision_from_trusted_channel(
    *,
    workspace_id: str,
    decision_id: str,
    approval: _TrustedChannelApproval,
) -> dict[str, object]:
    """Reload current state and use the one shared permission-checked path."""

    channel = approval.channel
    with decision_approval_lock:
        try:
            current = workspace_orchestrator.get(workspace_id)
        except LiveWorkspaceNotFound as exc:
            raise _workspace_api_error(exc) from exc
        current_body = current.model_dump(mode="json")
        if _approved_attempt_matches(
            current_body,
            decision_id=decision_id,
            fingerprint=approval.confirmed_proposal_fingerprint,
            proposal_instance_id=approval.confirmed_proposal_instance_id,
        ):
            return correlated_payload(
                {
                    "disposition": ApprovalDisposition.ALREADY_APPROVED.value,
                    "workspace": current_body,
                }
            )

        pending = pending_from_workspace(current_body)
        if pending is None or pending.decision_id != decision_id:
            permission_id = _decision_permission_id(
                current_body,
                decision_id=decision_id,
                proposal_fingerprint=(
                    approval.confirmed_proposal_fingerprint
                ),
                proposal_instance_id=(
                    approval.confirmed_proposal_instance_id
                ),
            )
            if permission_id is not None:
                _record_approval_rejection(
                    workspace_id=workspace_id,
                    decision_id=decision_id,
                    disposition=ApprovalDisposition.STALE_CONFIRMATION,
                    approver_user_id=approval.approver_user_id,
                    permission_id=permission_id,
                    channel=channel,
                    evidence_ref=approval.evidence_ref,
                    confirmed_proposal_fingerprint=(
                        approval.confirmed_proposal_fingerprint
                    ),
                    confirmed_proposal_instance_id=(
                        approval.confirmed_proposal_instance_id
                    ),
                    detail=(
                        "The requested Decision is no longer awaiting approval."
                    ),
                )
            return correlated_payload(
                {
                    "disposition": (
                        ApprovalDisposition.STALE_CONFIRMATION.value
                    ),
                    "workspace": current_body,
                }
            )
        if (
            approval.confirmed_proposal_fingerprint
            != pending.proposal_fingerprint
            or approval.confirmed_proposal_instance_id
            != pending.proposal_instance_id
        ):
            stale_permission_id = _decision_permission_id(
                current_body,
                decision_id=decision_id,
                proposal_fingerprint=(
                    approval.confirmed_proposal_fingerprint
                ),
                proposal_instance_id=(
                    approval.confirmed_proposal_instance_id
                ),
            )
            if stale_permission_id is not None:
                _record_approval_rejection(
                    workspace_id=workspace_id,
                    decision_id=decision_id,
                    disposition=ApprovalDisposition.STALE_CONFIRMATION,
                    approver_user_id=approval.approver_user_id,
                    permission_id=stale_permission_id,
                    channel=channel,
                    evidence_ref=approval.evidence_ref,
                    confirmed_proposal_fingerprint=(
                        approval.confirmed_proposal_fingerprint
                    ),
                    confirmed_proposal_instance_id=(
                        approval.confirmed_proposal_instance_id
                    ),
                    detail=(
                        "The signed-channel confirmation no longer matches the pending "
                        "Decision."
                    ),
                )
            return correlated_payload(
                {
                    "disposition": (
                        ApprovalDisposition.STALE_CONFIRMATION.value
                    ),
                    "workspace": current_body,
                }
            )
        if channel is ApprovalChannel.SLACK_REACTION:
            try:
                _require_workspace_slack_identity(
                    current_body,
                    workspace_id=workspace_id,
                    approver_user_id=approval.approver_user_id,
                )
            except ApiError as exc:
                if exc.status_code != 403:
                    raise
                _record_approval_rejection(
                    workspace_id=workspace_id,
                    decision_id=decision_id,
                    disposition=(
                        ApprovalDisposition.IGNORED_NOT_AUTHORIZED
                    ),
                    approver_user_id=approval.approver_user_id,
                    permission_id=pending.permission_id,
                    channel=channel,
                    evidence_ref=approval.evidence_ref,
                    confirmed_proposal_fingerprint=(
                        approval.confirmed_proposal_fingerprint
                    ),
                    confirmed_proposal_instance_id=(
                        approval.confirmed_proposal_instance_id
                    ),
                    detail=exc.message,
                )
                return correlated_payload(
                    {
                        "disposition": (
                            ApprovalDisposition.IGNORED_NOT_AUTHORIZED.value
                        ),
                        "workspace": None,
                    }
                )
        try:
            checker = _workspace_permission_checker(current_body)
        except HexclaveConfigurationError as exc:
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=(
                    ApprovalDisposition.PERMISSION_CHECK_UNAVAILABLE
                ),
                approver_user_id=approval.approver_user_id,
                permission_id=pending.permission_id,
                channel=channel,
                evidence_ref=approval.evidence_ref,
                confirmed_proposal_fingerprint=(
                    approval.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    approval.confirmed_proposal_instance_id
                ),
                detail=str(exc),
            )
            return correlated_payload(
                {
                    "disposition": (
                        ApprovalDisposition.PERMISSION_CHECK_UNAVAILABLE.value
                    ),
                    "workspace": None,
                }
            )

        try:
            result = ApprovalCoordinator(
                permission_checker=checker,
                workspace_port=_AgentWorkspaceApprovalPort(),
                clock=lambda: datetime.now(UTC),
            ).approve(
                pending=pending,
                approver_user_id=approval.approver_user_id,
                channel=channel,
                evidence_ref=approval.evidence_ref,
            )
        except LiveWorkspaceStateConflict as exc:
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=ApprovalDisposition.STALE_CONFIRMATION,
                approver_user_id=approval.approver_user_id,
                permission_id=pending.permission_id,
                channel=channel,
                evidence_ref=approval.evidence_ref,
                confirmed_proposal_fingerprint=(
                    approval.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    approval.confirmed_proposal_instance_id
                ),
                detail=str(exc),
            )
            return correlated_payload(
                {
                    "disposition": (
                        ApprovalDisposition.STALE_CONFIRMATION.value
                    ),
                    "workspace": None,
                }
            )
        if result.disposition in {
            ApprovalDisposition.IGNORED_NOT_AUTHORIZED,
            ApprovalDisposition.PERMISSION_CHECK_UNAVAILABLE,
        }:
            detail = (
                "The channel user is not authorized for this Decision."
                if result.disposition
                is ApprovalDisposition.IGNORED_NOT_AUTHORIZED
                else "Hexclave permission verification is unavailable."
            )
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=result.disposition,
                approver_user_id=approval.approver_user_id,
                permission_id=pending.permission_id,
                channel=channel,
                evidence_ref=approval.evidence_ref,
                confirmed_proposal_fingerprint=(
                    approval.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    approval.confirmed_proposal_instance_id
                ),
                detail=detail,
            )
        if result.workspace is not None:
            _publish_workspace(workspace_id, result.workspace)
        return correlated_payload(
            {
                "disposition": result.disposition.value,
                "workspace": (
                    dict(result.workspace)
                    if result.workspace is not None
                    else None
                ),
            }
        )


@app.post("/live-workspaces/{workspace_id}/decisions/{decision_id}/approve")
def approve_live_workspace_decision(
    workspace_id: str,
    decision_id: str,
    request: ApprovalAttemptEnvelope,
) -> dict[str, object]:
    if request.channel not in {
        ApprovalChannel.CLI,
        ApprovalChannel.WORKSPACE_UI,
    }:
        raise ApiError(
            status_code=400,
            code="INVALID_APPROVAL_CHANNEL",
            message=(
                "Slack and notification approvals must use their signed "
                "channel-specific intake route."
            ),
        )
    try:
        approver_user_id = _approval_identity_resolver().resolve_user_id(
            approval_token=request.approval_token.get_secret_value()
        )
    except HexclaveConfigurationError as exc:
        raise ApiError(
            status_code=503,
            code="HEXCLAVE_NOT_CONFIGURED",
            message=str(exc),
        ) from exc
    except HexclavePermissionError as exc:
        raise ApiError(
            status_code=401,
            code="APPROVAL_AUTHENTICATION_FAILED",
            message="Approval authentication failed.",
        ) from exc

    with decision_approval_lock:
        try:
            current = workspace_orchestrator.get(workspace_id)
        except LiveWorkspaceNotFound as exc:
            raise _workspace_api_error(exc) from exc
        current_body = current.model_dump(mode="json")
        if _approved_attempt_matches(
            current_body,
            decision_id=decision_id,
            fingerprint=request.confirmed_proposal_fingerprint,
            proposal_instance_id=request.confirmed_proposal_instance_id,
        ):
            return _workspace_response(workspace_id, current)

        pending = pending_from_workspace(current_body)
        if pending is None or pending.decision_id != decision_id:
            permission_id = _decision_permission_id(
                current_body,
                decision_id=decision_id,
                proposal_fingerprint=(
                    request.confirmed_proposal_fingerprint
                ),
                proposal_instance_id=(
                    request.confirmed_proposal_instance_id
                ),
            )
            if permission_id is not None:
                _record_approval_rejection(
                    workspace_id=workspace_id,
                    decision_id=decision_id,
                    disposition=ApprovalDisposition.STALE_CONFIRMATION,
                    approver_user_id=approver_user_id,
                    permission_id=permission_id,
                    channel=request.channel,
                    evidence_ref=request.evidence_ref,
                    confirmed_proposal_fingerprint=(
                        request.confirmed_proposal_fingerprint
                    ),
                    confirmed_proposal_instance_id=(
                        request.confirmed_proposal_instance_id
                    ),
                    detail=(
                        "The requested Decision is no longer awaiting approval."
                    ),
                )
            raise ApiError(
                status_code=409,
                code="PENDING_DECISION_NOT_FOUND",
                message="The requested Decision is not awaiting approval.",
            )
        if (
            request.confirmed_proposal_fingerprint is None
            or request.confirmed_proposal_instance_id is None
        ):
            detail = (
                "Approval did not confirm the Decision fingerprint and "
                "proposal instance."
            )
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=ApprovalDisposition.MISSING_CONFIRMATION,
                approver_user_id=approver_user_id,
                permission_id=pending.permission_id,
                channel=request.channel,
                evidence_ref=request.evidence_ref,
                confirmed_proposal_fingerprint=(
                    request.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    request.confirmed_proposal_instance_id
                ),
                detail=detail,
            )
            raise ApiError(
                status_code=409,
                code="MISSING_PROPOSAL_CONFIRMATION",
                message=detail,
            )
        attempt = ApprovalAttemptRequest.model_validate(
            request.model_dump(mode="python")
        )
        if (
            attempt.confirmed_proposal_fingerprint
            != pending.proposal_fingerprint
            or attempt.confirmed_proposal_instance_id
            != pending.proposal_instance_id
        ):
            detail = (
                "The confirmed proposal no longer matches the pending Decision."
            )
            stale_permission_id = _decision_permission_id(
                current_body,
                decision_id=decision_id,
                proposal_fingerprint=(
                    attempt.confirmed_proposal_fingerprint
                ),
                proposal_instance_id=(
                    attempt.confirmed_proposal_instance_id
                ),
            )
            if stale_permission_id is not None:
                _record_approval_rejection(
                    workspace_id=workspace_id,
                    decision_id=decision_id,
                    disposition=ApprovalDisposition.STALE_CONFIRMATION,
                    approver_user_id=approver_user_id,
                    permission_id=stale_permission_id,
                    channel=attempt.channel,
                    evidence_ref=attempt.evidence_ref,
                    confirmed_proposal_fingerprint=(
                        attempt.confirmed_proposal_fingerprint
                    ),
                    confirmed_proposal_instance_id=(
                        attempt.confirmed_proposal_instance_id
                    ),
                    detail=detail,
                )
            raise ApiError(
                status_code=409,
                code="STALE_PROPOSAL_CONFIRMATION",
                message=detail,
            )
        try:
            checker = _workspace_permission_checker(current_body)
        except HexclaveConfigurationError as exc:
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=(
                    ApprovalDisposition.PERMISSION_CHECK_UNAVAILABLE
                ),
                approver_user_id=approver_user_id,
                permission_id=pending.permission_id,
                channel=attempt.channel,
                evidence_ref=attempt.evidence_ref,
                confirmed_proposal_fingerprint=(
                    attempt.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    attempt.confirmed_proposal_instance_id
                ),
                detail="Hexclave permission verification is unavailable.",
            )
            raise ApiError(
                status_code=503,
                code="HEXCLAVE_NOT_CONFIGURED",
                message=str(exc),
            ) from exc
        try:
            result = ApprovalCoordinator(
                permission_checker=checker,
                workspace_port=_AgentWorkspaceApprovalPort(),
                clock=lambda: datetime.now(UTC),
            ).approve(
                pending=pending,
                approver_user_id=approver_user_id,
                channel=attempt.channel,
                evidence_ref=attempt.evidence_ref,
            )
        except LiveWorkspaceStateConflict as exc:
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=ApprovalDisposition.STALE_CONFIRMATION,
                approver_user_id=approver_user_id,
                permission_id=pending.permission_id,
                channel=attempt.channel,
                evidence_ref=attempt.evidence_ref,
                confirmed_proposal_fingerprint=(
                    attempt.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    attempt.confirmed_proposal_instance_id
                ),
                detail=str(exc),
            )
            raise _workspace_api_error(exc) from exc
        except LiveWorkspaceNotFound as exc:
            raise _workspace_api_error(exc) from exc
        if result.disposition is ApprovalDisposition.IGNORED_NOT_AUTHORIZED:
            detail = "The authenticated user cannot approve this Decision."
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=result.disposition,
                approver_user_id=approver_user_id,
                permission_id=pending.permission_id,
                channel=attempt.channel,
                evidence_ref=attempt.evidence_ref,
                confirmed_proposal_fingerprint=(
                    attempt.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    attempt.confirmed_proposal_instance_id
                ),
                detail=detail,
            )
            raise ApiError(
                status_code=403,
                code="APPROVER_NOT_AUTHORIZED",
                message=detail,
            )
        if (
            result.disposition
            is ApprovalDisposition.PERMISSION_CHECK_UNAVAILABLE
        ):
            _record_approval_rejection(
                workspace_id=workspace_id,
                decision_id=decision_id,
                disposition=result.disposition,
                approver_user_id=approver_user_id,
                permission_id=pending.permission_id,
                channel=attempt.channel,
                evidence_ref=attempt.evidence_ref,
                confirmed_proposal_fingerprint=(
                    attempt.confirmed_proposal_fingerprint
                ),
                confirmed_proposal_instance_id=(
                    attempt.confirmed_proposal_instance_id
                ),
                detail="Hexclave permission verification is unavailable.",
            )
            raise ApiError(
                status_code=503,
                code="PERMISSION_CHECK_UNAVAILABLE",
                message="Hexclave permission verification is unavailable.",
                retryable=True,
            )
        if result.workspace is None:
            raise ApiError(
                status_code=500,
                code="APPROVAL_RESULT_INVALID",
                message="The approval path returned no Workspace state.",
            )
        return _workspace_response(workspace_id, result.workspace)


@app.delete("/live-workspaces/{workspace_id}/decisions/pending")
def cancel_live_workspace_pending_decision(
    workspace_id: str,
) -> dict[str, object]:
    try:
        workspace = workspace_orchestrator.cancel_pending_decision(workspace_id)
    except (LiveWorkspaceNotFound, LiveWorkspaceStateConflict) as exc:
        raise _workspace_api_error(exc) from exc
    return _workspace_response(workspace_id, workspace)


@app.post("/live-workspaces/{workspace_id}/grants/initial/verify")
def verify_live_workspace_initial_grant(
    workspace_id: str,
    _request: WorkspaceEmptyRequest,
) -> dict[str, object]:
    try:
        workspace = workspace_orchestrator.verify_initial_grant(workspace_id)
    except (LiveWorkspaceNotFound, LiveWorkspaceStateConflict) as exc:
        raise _workspace_api_error(exc) from exc
    return _workspace_response(workspace_id, workspace)


@app.put("/live-workspaces/{workspace_id}/plan")
def update_live_workspace_plan(
    workspace_id: str,
    request: WorkspacePlanUpdateRequest,
) -> dict[str, object]:
    try:
        workspace = workspace_orchestrator.update_plan(workspace_id, request)
    except (LiveWorkspaceNotFound, LiveWorkspaceStateConflict) as exc:
        raise _workspace_api_error(exc) from exc
    return _workspace_response(workspace_id, workspace)


@app.post("/live-workspaces/{workspace_id}/reauthorize")
def reauthorize_live_workspace(
    workspace_id: str,
    _request: WorkspaceEmptyRequest,
) -> dict[str, object]:
    try:
        workspace = workspace_orchestrator.reauthorize(workspace_id)
    except (LiveWorkspaceNotFound, LiveWorkspaceStateConflict) as exc:
        raise _workspace_api_error(exc) from exc
    return _workspace_response(workspace_id, workspace)


@app.post("/live-workspaces/{workspace_id}/grants/replacement/verify")
def verify_live_workspace_replacement_grant(
    workspace_id: str,
    _request: WorkspaceEmptyRequest,
) -> dict[str, object]:
    try:
        workspace = workspace_orchestrator.verify_replacement_grant(workspace_id)
    except (LiveWorkspaceNotFound, LiveWorkspaceStateConflict) as exc:
        raise _workspace_api_error(exc) from exc
    return _workspace_response(workspace_id, workspace)


@app.get("/scenario-lab/runs/{run_id}")
def scenario_run(run_id: str) -> dict[str, object]:
    try:
        run = scenario_runner.get(run_id)
    except ScenarioRunNotFound as exc:
        raise _scenario_api_error(exc) from exc
    return correlated_payload(run)


@app.post("/scenario-lab/runs/{run_id}/advance")
def scenario_advance(
    run_id: str,
    request: ScenarioAdvanceRequest | None = None,
) -> dict[str, object]:
    try:
        run = scenario_runner.advance(
            run_id,
            expected_stage=request.expected_stage if request else None,
        )
    except (ScenarioRunNotFound, ScenarioRunConflict) as exc:
        raise _scenario_api_error(exc) from exc
    return correlated_payload(run)


@app.post("/scenario-lab/scenarios/{scenario_id}/reset")
def scenario_reset(scenario_id: str) -> dict[str, object]:
    try:
        scenario_runner.definition(scenario_id)
        scenario_runner.reset(scenario_id)
    except (KeyError, ScenarioRunConflict) as exc:
        raise _scenario_api_error(exc) from exc
    return correlated_payload({"reset": True, "scenario_id": scenario_id})


@app.get("/scenario-lab/results")
def scenario_results() -> dict[str, object]:
    return correlated_payload({"runs": scenario_runner.latest_runs()})


@app.post("/scenario-lab/run-all")
def scenario_run_all(request: ScenarioRunAllRequest) -> dict[str, object]:
    try:
        report = scenario_runner.run_all(request.scenario_ids)
    except (KeyError, ScenarioRunConflict) as exc:
        raise _scenario_api_error(exc) from exc
    return correlated_payload(report)
