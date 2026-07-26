from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import parse_qs, quote

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

from writai.auth.hexclave import (
    HexclaveConfigurationError,
    HexclavePermissionChecker,
)
from writai.auth.hexclave_webhooks import (
    HEXCLAVE_WEBHOOK_MAX_BODY_BYTES,
    HexclaveCacheInvalidationRequest,
    HexclaveWebhookConfigurationError,
    HexclaveWebhookEvidenceStoreError,
    HexclaveWebhookPayloadError,
    HexclaveWebhookVerificationError,
    JsonHexclaveWebhookEvidenceStore,
    SvixHexclaveWebhookVerifier,
)
from writai.config import settings
from writai.domain import (
    ArtifactKind,
    AuthorizationRequest,
    DecisionMutation,
    GrantVerificationRequest,
)
from writai.intake.approval import (
    ApprovalChannel,
    ApprovalDisposition,
    ApprovalResult,
    PendingApproval,
)
from writai.intake.decisions import DecisionDraftError
from writai.intake.gate import DeterministicIntakeGate
from writai.intake.replay import (
    JsonSlackDeliveryReplayStore,
    SlackDeliveryKey,
)
from writai.intake.slack import (
    CHANNEL_MESSAGE_SLUG,
    REACTION_ADDED_SLUG,
    ComposioSlackWebhookVerifier,
    SlackDecisionIntake,
    SlackIntakeOutcome,
    SlackWebhookError,
    VerifiedSlackMessage,
    VerifiedSlackReaction,
)
from writai.llm.provider import (
    LLMProviderConfigurationError,
    build_decision_extractor,
)
from writai.notify.email import (
    ApprovalDeliveryProvider,
    ApprovalImpact,
    ApprovalLinkClaims,
    ApprovalLinkConfigurationError,
    ApprovalLinkError,
    ApprovalLinkRedeemer,
    ApprovalLinkSigner,
    ApprovalRecipientDirectory,
    ChannelApprovalAssertionSigner,
    EmailApprovalDispatchRequest,
    EmailApprovalNotifier,
    EmailNotificationError,
    ExpiredApprovalLink,
    InvalidApprovalLink,
    ReplayedApprovalLink,
    ResendEmailSender,
    SqliteApprovalLinkUseStore,
    approval_confirmation_html,
)
from writai.notify.push import (
    ApprovalPushSender,
    NtfyPushSender,
    PushApprovalDispatchRequest,
    PushApprovalNotifier,
    PushNotificationError,
    PushoverPushSender,
)
from writai.notify.slack import (
    ComposioSlackThreadSender,
    JsonSlackApprovalThreads,
    SlackBlastRadiusNotifier,
    SlackDecisionNotification,
    SlackNotificationError,
    SlackNotificationResult,
    SlackReactionApprovalHandler,
    SlackReactionResult,
    SlackRequirementDelta,
    SlackThreadReference,
    SlackThreadSender,
)
from writai.runtime import create_authority_runtime
from writai.scenarios.authority_contexts import (
    ScenarioAuthorityContextConflict,
    ScenarioAuthorityContextCreateRequest,
    ScenarioAuthorityContextNotFound,
    ScenarioAuthorityContextRegistry,
    ScenarioDefinitionNotFound,
)
from writai.services.events import EventBroker, snapshot_event, stream_events
from writai.services.support import (
    CORRELATION_ID_HEADER,
    DEMO_FRONTEND_ORIGINS,
    INTERNAL_SERVICE_AUTH_HEADER,
    ApiError,
    correlated_payload,
    current_correlation_id,
    install_api_support,
    internal_service_token,
    post_model,
    require_internal_service,
)
from writai.supervisor_contract import InterruptRequest, InterruptResult
from writai.workspaces.authority_contexts import (
    DynamicAuthorityContextConflict,
    DynamicAuthorityContextCreateRequest,
    DynamicAuthorityContextNotFound,
    DynamicAuthorityContextRegistry,
    DynamicMutationApprovalRequest,
)
from writai.workspaces.models import (
    LiveWorkspaceView,
    WorkspaceApprovalPreview,
    WorkspaceApprovalRequest,
    WorkspaceSlackBinding,
)

runtime = create_authority_runtime()
scenario_contexts = ScenarioAuthorityContextRegistry(
    grant_secret=settings.grant_secret,
    grant_ttl_seconds=settings.grant_ttl_seconds,
    authority_threshold=settings.authority_threshold,
)
workspace_contexts = DynamicAuthorityContextRegistry(
    grant_secret=settings.grant_secret,
    grant_ttl_seconds=settings.grant_ttl_seconds,
    authority_threshold=settings.authority_threshold,
)
event_broker = EventBroker()
runtime_lock = RLock()
slack_intake_lock = RLock()
hexclave_webhook_lock = RLock()
hexclave_webhook_store_lock = RLock()
# Process-local clients make the checker's bounded TTL effective across requests.
hexclave_client_lock = RLock()
slack_store_lock = RLock()
approval_link_store_lock = RLock()
hexclave_permission_checkers: dict[
    tuple[str, str, bytes, float, str], HexclavePermissionChecker
] = {}
slack_approval_thread_stores: dict[Path, JsonSlackApprovalThreads] = {}
slack_delivery_replay_stores: dict[Path, JsonSlackDeliveryReplayStore] = {}
approval_link_use_stores: dict[Path, SqliteApprovalLinkUseStore] = {}
_slack_verifier: ComposioSlackWebhookVerifier | None = None
_hexclave_webhook_verifier: SvixHexclaveWebhookVerifier | None = None
hexclave_webhook_evidence_stores: dict[
    Path, JsonHexclaveWebhookEvidenceStore
] = {}
app = FastAPI(title="writ.ai Intent Authority", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=DEMO_FRONTEND_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[CORRELATION_ID_HEADER],
)
install_api_support(app)


@app.get("/health")
def health() -> dict[str, str]:
    with runtime_lock:
        graph_version = runtime.graph.version_label
    return correlated_payload({"status": "ok", "graph_version": graph_version})


def _state_body() -> dict[str, object]:
    with runtime_lock:
        state = runtime.graph.snapshot()
        state["last_report"] = (
            runtime.authority.last_report.model_dump(mode="json")
            if runtime.authority.last_report
            else None
        )
        return state


def _require_demo_reset() -> None:
    if not settings.demo_reset_enabled:
        raise ApiError(
            status_code=403,
            code="DEMO_RESET_DISABLED",
            message="Demo graph reset is disabled in this environment.",
        )


@app.post("/graph/reset")
@app.post("/demo/reset")
def reset_demo() -> dict[str, object]:
    _require_demo_reset()
    with runtime_lock:
        runtime.reset()
        state = _state_body()
        event_broker.publish("graph.state.reset", state)
    return correlated_payload(state)


@app.get("/demo/state")
def demo_state() -> dict[str, object]:
    with runtime_lock:
        state = _state_body()
    return correlated_payload(state)


@app.get("/events")
def events(request: Request) -> StreamingResponse:
    with runtime_lock:
        initial = snapshot_event(
            sequence=event_broker.current_sequence,
            event_type="graph.state.snapshot",
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


@app.post("/decisions/ingest")
def ingest_decision(mutation: DecisionMutation):
    if not settings.demo_reset_enabled:
        raise ApiError(
            status_code=403,
            code="FIXTURE_INGEST_DISABLED",
            message="Direct approved-decision ingest is available only in demo mode.",
        )
    try:
        with runtime_lock:
            result = runtime.authority.apply_decision_change(mutation)
            event_broker.publish(
                "graph.state.changed" if result.applied else "graph.decision.reviewed",
                _state_body(),
            )
    except KeyError as exc:
        raise ApiError(
            status_code=404,
            code="ARTIFACT_NOT_FOUND",
            message="The superseded artifact does not exist.",
        ) from exc
    except ValueError as exc:
        raise ApiError(
            status_code=409,
            code="ARTIFACT_CONFLICT",
            message="The decision artifact already exists.",
        ) from exc
    return correlated_payload(result)


def _live_slack_verifier() -> ComposioSlackWebhookVerifier:
    global _slack_verifier
    with slack_intake_lock:
        if _slack_verifier is None:
            try:
                _slack_verifier = ComposioSlackWebhookVerifier.live(
                    api_key=settings.composio_api_key or "",
                    webhook_secret=settings.composio_webhook_secret or "",
                )
            except SlackWebhookError as exc:
                raise ApiError(
                    status_code=503,
                    code="SLACK_INTAKE_NOT_CONFIGURED",
                    message=str(exc),
                ) from exc
        return _slack_verifier


def _hexclave_permission_checker(team_id: str) -> HexclavePermissionChecker:
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


def _live_hexclave_webhook_verifier() -> SvixHexclaveWebhookVerifier:
    global _hexclave_webhook_verifier
    with hexclave_webhook_lock:
        if _hexclave_webhook_verifier is None:
            try:
                _hexclave_webhook_verifier = SvixHexclaveWebhookVerifier(
                    secret=settings.hexclave_webhook_secret
                )
            except HexclaveWebhookConfigurationError as exc:
                raise ApiError(
                    status_code=503,
                    code="HEXCLAVE_WEBHOOK_NOT_CONFIGURED",
                    message=str(exc),
                    retryable=True,
                ) from exc
        return _hexclave_webhook_verifier


def _hexclave_webhook_evidence_store() -> JsonHexclaveWebhookEvidenceStore:
    path = Path(settings.hexclave_webhook_evidence_store).expanduser()
    with hexclave_webhook_store_lock:
        store = hexclave_webhook_evidence_stores.get(path)
        if store is None:
            store = JsonHexclaveWebhookEvidenceStore(path)
            hexclave_webhook_evidence_stores[path] = store
        return store


def _clear_hexclave_permission_caches() -> int:
    """Clear every process-local permission result without producing a verdict."""

    with hexclave_client_lock:
        checkers = tuple(hexclave_permission_checkers.values())
        for checker in checkers:
            checker.clear_cache()
        return len(checkers)


def _invalidate_agent_hexclave_permission_cache(
    invalidation: HexclaveCacheInvalidationRequest,
) -> int:
    try:
        response = httpx.post(
            f"{settings.agent_url}/internal/hexclave/permission-cache/invalidate",
            json=invalidation.model_dump(mode="json"),
            headers={
                CORRELATION_ID_HEADER: current_correlation_id(),
                INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
                    settings.grant_secret
                ),
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(settings.service_timeout_seconds),
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        raise ApiError(
            status_code=503,
            code="AGENT_PERMISSION_CACHE_UNAVAILABLE",
            message="The agent permission-cache service is unavailable.",
            retryable=True,
        ) from exc
    if response.is_error:
        raise ApiError(
            status_code=503,
            code="AGENT_PERMISSION_CACHE_UNAVAILABLE",
            message="The agent permission-cache service is unavailable.",
            retryable=True,
        )
    try:
        body = response.json()
        caches_cleared = body["caches_cleared"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(
            status_code=503,
            code="AGENT_PERMISSION_CACHE_UNAVAILABLE",
            message="The agent permission-cache service returned an invalid response.",
            retryable=True,
        ) from exc
    if (
        not isinstance(caches_cleared, int)
        or isinstance(caches_cleared, bool)
        or caches_cleared < 0
    ):
        raise ApiError(
            status_code=503,
            code="AGENT_PERMISSION_CACHE_UNAVAILABLE",
            message="The agent permission-cache service returned an invalid response.",
            retryable=True,
        )
    return caches_cleared


async def _read_hexclave_webhook_body(request: Request) -> bytes:
    """Read an unsigned delivery without ever buffering more than the hard cap."""

    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            parsed_length = int(declared_length)
        except ValueError as exc:
            raise HexclaveWebhookPayloadError(
                "The Hexclave webhook payload length is invalid."
            ) from exc
        if (
            parsed_length < 0
            or parsed_length > HEXCLAVE_WEBHOOK_MAX_BODY_BYTES
        ):
            raise HexclaveWebhookPayloadError(
                "The Hexclave webhook payload is too large."
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > HEXCLAVE_WEBHOOK_MAX_BODY_BYTES:
            raise HexclaveWebhookPayloadError(
                "The Hexclave webhook payload is too large."
            )
        body.extend(chunk)
    return bytes(body)


@app.post("/webhooks/hexclave")
async def receive_hexclave_webhook(request: Request) -> dict[str, object]:
    """Verify an authority event, clear caches, and record no authorization verdict."""

    try:
        body = await _read_hexclave_webhook_body(request)
        verified = _live_hexclave_webhook_verifier().verify(
            body=body,
            headers=request.headers,
        )
    except HexclaveWebhookVerificationError as exc:
        raise ApiError(
            status_code=401,
            code="INVALID_HEXCLAVE_WEBHOOK",
            message=str(exc),
        ) from exc
    except HexclaveWebhookPayloadError as exc:
        raise ApiError(
            status_code=400,
            code="INVALID_HEXCLAVE_WEBHOOK_PAYLOAD",
            message=str(exc),
        ) from exc

    event = verified.event
    if event is None:
        return correlated_payload(
            {
                "status": "ignored",
                "event_type": verified.event_type,
                "graph_mutated": False,
            }
        )

    store = _hexclave_webhook_evidence_store()
    try:
        reservation = store.reserve(event)
    except HexclaveWebhookEvidenceStoreError as exc:
        raise ApiError(
            status_code=503,
            code="HEXCLAVE_WEBHOOK_EVIDENCE_UNAVAILABLE",
            message=str(exc),
            retryable=True,
        ) from exc
    if reservation == "completed":
        return correlated_payload(
            {
                "status": "permission-cache-invalidated",
                "delivery_id": event.delivery_id,
                "event_type": event.event_type,
                "evidence_ref": event.evidence_ref,
                "duplicate": True,
                "graph_mutated": False,
            }
        )

    authority_caches_cleared = _clear_hexclave_permission_caches()
    agent_caches_cleared = _invalidate_agent_hexclave_permission_cache(
        HexclaveCacheInvalidationRequest(
            delivery_id=event.delivery_id,
            event_type=event.event_type,
            evidence_ref=event.evidence_ref,
        )
    )
    try:
        store.complete(
            event.delivery_id,
            authority_caches_cleared=authority_caches_cleared,
            agent_caches_cleared=agent_caches_cleared,
        )
    except HexclaveWebhookEvidenceStoreError as exc:
        raise ApiError(
            status_code=503,
            code="HEXCLAVE_WEBHOOK_EVIDENCE_UNAVAILABLE",
            message=str(exc),
            retryable=True,
        ) from exc
    return correlated_payload(
        {
            "status": "permission-cache-invalidated",
            "delivery_id": event.delivery_id,
            "event_type": event.event_type,
            "evidence_ref": event.evidence_ref,
            "authority_caches_cleared": authority_caches_cleared,
            "agent_caches_cleared": agent_caches_cleared,
            "duplicate": False,
            "graph_mutated": False,
        }
    )


class _SlackReplayDuplicate(RuntimeError):
    def __init__(self, result: Mapping[str, Any] | None) -> None:
        super().__init__("The signed Slack delivery was already reserved.")
        self.result = dict(result) if result is not None else None


class _PreviewSnapshotPort:
    """Expose one agent-computed preview through the notifier's frozen seam."""

    def __init__(
        self,
        *,
        request: InterruptRequest,
        preview: WorkspaceApprovalPreview,
    ) -> None:
        self._request = request
        self._result = InterruptResult(
            interrupted_assignment_ids=preview.interrupted_assignment_ids,
            preserved_assignment_ids=preview.preserved_assignment_ids,
        )

    def preview(self, request: InterruptRequest) -> InterruptResult:
        if request != self._request:
            raise SlackNotificationError(
                "The Slack notification does not match the agent preview."
            )
        return self._result

    def interrupt(self, request: InterruptRequest) -> InterruptResult:
        del request
        raise SlackNotificationError(
            "The notification preview port may not interrupt assignments."
        )


class _BoundSlackReactionIdentityResolver:
    def __init__(self, binding: WorkspaceSlackBinding) -> None:
        self._binding = binding

    def resolve_hexclave_user_id(
        self,
        *,
        pending: PendingApproval,
        reaction: VerifiedSlackReaction,
    ) -> str | None:
        if (
            pending.workspace_id != self._binding.workspace_id
            or reaction.team_id != self._binding.slack_team_id
            or reaction.connection_user_id
            != self._binding.composio_connection_user_id
        ):
            return None
        return self._binding.hexclave_user_for(reaction.reacting_user_id)


class _AgentChannelApprovalPort:
    """Forward only channel-authenticated approval capabilities to the agent."""

    def approve(
        self,
        *,
        pending: PendingApproval,
        approver_user_id: str,
        channel: ApprovalChannel,
        evidence_ref: str,
    ) -> ApprovalResult:
        if channel is not ApprovalChannel.SLACK_REACTION:
            raise SlackNotificationError(
                "The signed Slack path accepts only Slack reaction approvals."
            )
        assertion = _channel_approval_assertion_signer().issue_slack(
            pending=pending,
            approver_user_id=approver_user_id,
            evidence_ref=evidence_ref,
        )
        return self._forward(
            workspace_id=pending.workspace_id,
            decision_id=pending.decision_id,
            channel=channel,
            assertion=assertion.get_secret_value(),
        )

    def approve_bound(
        self,
        *,
        claims: ApprovalLinkClaims,
        evidence_ref: str,
    ) -> ApprovalResult:
        del evidence_ref
        if claims.channel not in {
            ApprovalChannel.PUSH,
            ApprovalChannel.EMAIL,
        }:
            raise ApprovalLinkError(
                "The signed-link path accepts push or email approvals only."
            )
        assertion = _channel_approval_assertion_signer().issue_notification(
            claims
        )
        return self._forward(
            workspace_id=claims.workspace_id,
            decision_id=claims.decision_id,
            channel=claims.channel,
            assertion=assertion.get_secret_value(),
        )

    def _forward(
        self,
        *,
        workspace_id: str,
        decision_id: str,
        channel: ApprovalChannel,
        assertion: str,
    ) -> ApprovalResult:
        is_slack = channel is ApprovalChannel.SLACK_REACTION
        route = (
            "approve-from-slack"
            if is_slack
            else "approve-from-notification"
        )
        url = (
            f"{settings.agent_url}/internal/live-workspaces/"
            f"{quote(workspace_id, safe='')}/decisions/"
            f"{quote(decision_id, safe='')}/{route}"
        )
        payload = {"assertion": assertion}
        try:
            response = httpx.post(
                url,
                json=payload,
                headers={
                    CORRELATION_ID_HEADER: current_correlation_id(),
                    INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
                        settings.grant_secret
                    ),
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(settings.service_timeout_seconds),
            )
        except httpx.TimeoutException as exc:
            raise ApiError(
                status_code=504,
                code="AGENT_APPROVAL_TIMEOUT",
                message="The agent approval service timed out.",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ApiError(
                status_code=503,
                code="AGENT_APPROVAL_UNAVAILABLE",
                message="The agent approval service is unavailable.",
                retryable=True,
            ) from exc

        if response.status_code == 409:
            # A stale immutable binding is an ignored reaction, never a mutation.
            return ApprovalResult(
                disposition=ApprovalDisposition.STALE_CONFIRMATION
            )
        if response.is_error:
            raise ApiError(
                status_code=502,
                code="AGENT_APPROVAL_REJECTED",
                message=(
                    f"The agent approval service returned HTTP "
                    f"{response.status_code}."
                ),
                retryable=response.status_code >= 500,
            )
        try:
            body = response.json()
            disposition = ApprovalDisposition(body["disposition"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError(
                status_code=502,
                code="AGENT_APPROVAL_INVALID_RESPONSE",
                message="The agent approval service returned an invalid response.",
            ) from exc
        workspace = body.get("workspace")
        if workspace is not None and not isinstance(workspace, Mapping):
            raise ApiError(
                status_code=502,
                code="AGENT_APPROVAL_INVALID_RESPONSE",
                message="The agent approval response contained invalid Workspace state.",
            )
        if disposition in {
            ApprovalDisposition.APPROVED,
            ApprovalDisposition.ALREADY_APPROVED,
        } and not isinstance(workspace, Mapping):
            raise ApiError(
                status_code=502,
                code="AGENT_APPROVAL_INVALID_RESPONSE",
                message="The successful approval response omitted Workspace state.",
            )
        return ApprovalResult(
            disposition=disposition,
            workspace=dict(workspace) if isinstance(workspace, Mapping) else None,
        )


def _workspace_store_sibling(label: str) -> Path:
    workspace_path = Path(settings.workspace_store).expanduser()
    suffix = workspace_path.suffix or ".json"
    return workspace_path.with_name(
        f"{workspace_path.stem}-{label}{suffix}"
    )


def _approval_link_secret() -> str:
    secret = settings.approval_link_secret or ""
    if not secret or secret == settings.grant_secret:
        raise ApprovalLinkConfigurationError(
            "WRITAI_APPROVAL_LINK_SECRET must be configured and distinct "
            "from WRITAI_GRANT_SECRET."
        )
    return secret


def _approval_link_signer() -> ApprovalLinkSigner:
    return ApprovalLinkSigner(
        secret=_approval_link_secret(),
        ttl_seconds=settings.approval_link_ttl_seconds,
    )


def _channel_approval_assertion_signer(
) -> ChannelApprovalAssertionSigner:
    return ChannelApprovalAssertionSigner(
        secret=_approval_link_secret(),
    )


def _approval_recipient_directory() -> ApprovalRecipientDirectory:
    return ApprovalRecipientDirectory.from_json(
        settings.approval_recipient_bindings,
        ntfy_server=settings.ntfy_server,
    )


def _approval_link_use_store() -> SqliteApprovalLinkUseStore:
    path = Path(settings.approval_link_store).expanduser()
    with approval_link_store_lock:
        store = approval_link_use_stores.get(path)
        if store is None:
            store = SqliteApprovalLinkUseStore(path)
            approval_link_use_stores[path] = store
        return store


def _approval_link_redeemer() -> ApprovalLinkRedeemer:
    return ApprovalLinkRedeemer(
        signer=_approval_link_signer(),
        use_store=_approval_link_use_store(),
        approval_port=_AgentChannelApprovalPort(),
        recipient_verifier=_approval_recipient_directory(),
    )


def _approval_impact(
    preview: WorkspaceApprovalPreview,
) -> ApprovalImpact:
    return ApprovalImpact(
        interrupted_assignment_ids=tuple(
            sorted(preview.interrupted_assignment_ids)
        ),
        preserved_assignment_ids=tuple(
            sorted(preview.preserved_assignment_ids)
        ),
    )


def _email_approval_notifier() -> EmailApprovalNotifier:
    return EmailApprovalNotifier(
        signer=_approval_link_signer(),
        public_base_url=settings.public_base_url,
        sender_email=settings.email_from or "",
        sender=ResendEmailSender(
            api_key=settings.resend_api_key or "",
            timeout_seconds=settings.service_timeout_seconds,
        ),
    )


def _push_approval_notifier(
    provider: str,
    *,
    recipient_address: str,
) -> PushApprovalNotifier:
    sender: ApprovalPushSender
    if provider == "ntfy":
        sender = NtfyPushSender(
            server_url=settings.ntfy_server,
            topic=recipient_address,
            access_token=settings.ntfy_access_token or "",
            private_topic_confirmed=settings.ntfy_private_topic_confirmed,
            timeout_seconds=settings.service_timeout_seconds,
        )
    elif provider == "pushover":
        sender = PushoverPushSender(
            app_token=settings.pushover_app_token or "",
            user_key=recipient_address,
            timeout_seconds=settings.service_timeout_seconds,
        )
    else:  # Pydantic validates this; keep the composition boundary closed.
        raise PushNotificationError("The push provider is unsupported.")
    return PushApprovalNotifier(
        signer=_approval_link_signer(),
        public_base_url=settings.public_base_url,
        sender=sender,
    )


def _slack_approval_threads() -> JsonSlackApprovalThreads:
    path = _workspace_store_sibling("slack-approval-threads")
    with slack_store_lock:
        store = slack_approval_thread_stores.get(path)
        if store is None:
            store = JsonSlackApprovalThreads(path)
            slack_approval_thread_stores[path] = store
        return store


def _slack_delivery_replays() -> JsonSlackDeliveryReplayStore:
    path = _workspace_store_sibling("slack-deliveries")
    with slack_store_lock:
        store = slack_delivery_replay_stores.get(path)
        if store is None:
            store = JsonSlackDeliveryReplayStore(path)
            slack_delivery_replay_stores[path] = store
        return store


def _delivery_key(
    *,
    workspace_id: str,
    connection_user_id: str,
    trigger_kind: str,
    event_id: str,
) -> SlackDeliveryKey:
    return SlackDeliveryKey(
        workspace_id=workspace_id,
        connection_user_id=connection_user_id,
        trigger_kind=trigger_kind,
        event_id=event_id,
    )


def _has_webhook_delivery_id(headers: Mapping[str, Any]) -> bool:
    return any(
        isinstance(key, str)
        and key.casefold() == "webhook-id"
        and isinstance(value, str)
        and bool(value.strip())
        for key, value in headers.items()
    )


def _reserve_slack_delivery(key: SlackDeliveryKey) -> None:
    store = _slack_delivery_replays()
    if store.reserve(key):
        return
    record = store.get(key)
    raise _SlackReplayDuplicate(record.result if record is not None else None)


def _complete_slack_delivery(
    key: SlackDeliveryKey,
    result: Mapping[str, Any],
) -> None:
    store = _slack_delivery_replays()
    # Focused route tests may inject an already-normalized outcome and bypass
    # reservation. Production signed deliveries always have a record here.
    if store.get(key) is not None:
        store.complete(key, result=dict(result))


def _duplicate_delivery_result(
    duplicate: _SlackReplayDuplicate,
) -> dict[str, object]:
    if duplicate.result is None:
        return correlated_payload(
            {
                "status": "delivery-reserved",
                "duplicate": True,
                "graph_mutated": False,
            }
        )
    result = dict(duplicate.result)
    result["duplicate"] = True
    return correlated_payload(result)


def _agent_decision_preview(
    *,
    workspace_id: str,
    decision_id: str,
) -> WorkspaceApprovalPreview:
    try:
        response = httpx.get(
            (
                f"{settings.agent_url}/live-workspaces/"
                f"{quote(workspace_id, safe='')}/decisions/"
                f"{quote(decision_id, safe='')}/preview"
            ),
            headers={
                CORRELATION_ID_HEADER: current_correlation_id(),
                INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
                    settings.grant_secret
                ),
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(settings.service_timeout_seconds),
        )
    except httpx.TimeoutException as exc:
        raise ApiError(
            status_code=504,
            code="AGENT_PREVIEW_TIMEOUT",
            message="The agent approval preview timed out.",
            retryable=True,
        ) from exc
    except httpx.RequestError as exc:
        raise ApiError(
            status_code=503,
            code="AGENT_PREVIEW_UNAVAILABLE",
            message="The agent approval preview is unavailable.",
            retryable=True,
        ) from exc
    if response.is_error:
        raise ApiError(
            status_code=502,
            code="AGENT_PREVIEW_REJECTED",
            message=f"The agent preview returned HTTP {response.status_code}.",
            retryable=response.status_code >= 500,
        )
    try:
        return WorkspaceApprovalPreview.model_validate(response.json())
    except (TypeError, ValueError) as exc:
        raise ApiError(
            status_code=502,
            code="AGENT_PREVIEW_INVALID_RESPONSE",
            message="The agent approval preview returned an invalid response.",
        ) from exc


def _verified_connected_account_id(body: bytes) -> str:
    try:
        envelope = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise SlackNotificationError(
            "The verified Composio delivery is not a JSON envelope."
        ) from exc
    metadata = envelope.get("metadata") if isinstance(envelope, Mapping) else None
    value = (
        metadata.get("connected_account_id")
        if isinstance(metadata, Mapping)
        else None
    )
    if not isinstance(value, str) or not value.strip():
        raise SlackNotificationError(
            "The verified Composio delivery has no connected-account ID."
        )
    return value.strip()


def _slack_notification_sender(
    *,
    message: VerifiedSlackMessage,
    body: bytes,
) -> SlackThreadSender:
    return ComposioSlackThreadSender.live(
        api_key=settings.composio_api_key or "",
        composio_user_id=message.connection_user_id,
        connected_account_id=_verified_connected_account_id(body),
    )


def _requirement_summary(requirement: Mapping[str, Any]) -> str:
    text = json.dumps(
        dict(requirement),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return text if len(text) <= 500 else f"{text[:497]}..."


def _notification_interrupt_request(
    preview: WorkspaceApprovalPreview,
) -> InterruptRequest:
    pending = preview.pending
    paths = sorted(
        preview.assignment_provenance_paths.values(),
        key=lambda path: (len(path), path),
    )
    return InterruptRequest(
        workspace_id=pending.workspace_id,
        decision_id=pending.decision_id,
        affected_scopes=pending.affected_scopes,
        provenance_path=paths[-1] if paths else (),
        interrupt_reason=(
            f"Approval of {pending.decision_id} changes "
            + ", ".join(sorted(pending.affected_scopes))
            + "."
        ),
        redirect_instruction=(
            f"Replan against the requirements approved by "
            f"{pending.decision_id}."
        ),
    )


def _notify_slack_pending(
    *,
    outcome: SlackIntakeOutcome,
    body: bytes,
) -> SlackNotificationResult:
    proposal = outcome.proposal
    if proposal is None:
        raise SlackNotificationError(
            "A parked Slack candidate cannot produce an approval notification."
        )
    preview = _agent_decision_preview(
        workspace_id=outcome.workspace_id,
        decision_id=proposal.decision.id,
    )
    request = _notification_interrupt_request(preview)
    pending = preview.pending
    notification = SlackDecisionNotification(
        team_id=outcome.message.team_id,
        thread=SlackThreadReference(
            channel_id=outcome.message.channel_id,
            parent_message_id=outcome.message.message_ts,
        ),
        pending=pending,
        interrupt_request=request,
        source_author=outcome.message.author_user_id,
        source_excerpt=outcome.message.text[:2_000],
        effective_at=pending.effective_at,
        requirement_deltas=tuple(
            SlackRequirementDelta(
                scope=scope,
                proposed_summary=_requirement_summary(
                    pending.requirements[scope]
                ),
            )
            for scope in sorted(pending.affected_scopes)
        ),
        evidence_refs=pending.evidence_refs,
    )
    return SlackBlastRadiusNotifier(
        interrupt_port=_PreviewSnapshotPort(
            request=request,
            preview=preview,
        ),
        sender=_slack_notification_sender(
            message=outcome.message,
            body=body,
        ),
        approval_threads=_slack_approval_threads(),
    ).notify(notification)


@app.post(
    "/internal/live-workspaces/{workspace_id}/decisions/"
    "{decision_id}/notifications/email"
)
def dispatch_email_approval(
    workspace_id: str,
    decision_id: str,
    dispatch: EmailApprovalDispatchRequest,
    request: Request,
) -> dict[str, object]:
    """Send a bound email; this route never approves the proposal."""

    require_internal_service(request, secret=settings.grant_secret)
    preview = _agent_decision_preview(
        workspace_id=workspace_id,
        decision_id=decision_id,
    )
    try:
        recipient_email = _approval_recipient_directory().delivery_address(
            workspace_id=workspace_id,
            approver_user_id=dispatch.approver_user_id,
            provider=ApprovalDeliveryProvider.EMAIL,
        )
        result = _email_approval_notifier().notify(
            pending=preview.pending,
            approver_user_id=dispatch.approver_user_id,
            recipient_email=recipient_email,
            impact=_approval_impact(preview),
        )
    except (
        ApprovalLinkConfigurationError,
        EmailNotificationError,
    ) as exc:
        raise ApiError(
            status_code=503,
            code="EMAIL_APPROVAL_UNAVAILABLE",
            message=str(exc),
            retryable=True,
        ) from exc
    return correlated_payload(
        {
            "status": "notification-sent",
            "channel": ApprovalChannel.EMAIL.value,
            "workspace_id": workspace_id,
            "decision_id": decision_id,
            "proposal_fingerprint": result.proposal_fingerprint,
            "proposal_instance_id": result.proposal_instance_id,
            "expires_at": result.expires_at.isoformat(),
            "provider": result.receipt.provider,
            "message_id": result.receipt.message_id,
            "delivery_mode": result.receipt.delivery_mode.value,
            "graph_mutated": False,
        }
    )


@app.post(
    "/internal/live-workspaces/{workspace_id}/decisions/"
    "{decision_id}/notifications/push"
)
def dispatch_push_approval(
    workspace_id: str,
    decision_id: str,
    dispatch: PushApprovalDispatchRequest,
    request: Request,
) -> dict[str, object]:
    """Send a bound ntfy/Pushover message; this route never approves."""

    require_internal_service(request, secret=settings.grant_secret)
    preview = _agent_decision_preview(
        workspace_id=workspace_id,
        decision_id=decision_id,
    )
    try:
        provider = ApprovalDeliveryProvider(dispatch.provider)
        recipient_address = (
            _approval_recipient_directory().delivery_address(
                workspace_id=workspace_id,
                approver_user_id=dispatch.approver_user_id,
                provider=provider,
            )
        )
        result = _push_approval_notifier(
            dispatch.provider,
            recipient_address=recipient_address,
        ).notify(
            pending=preview.pending,
            approver_user_id=dispatch.approver_user_id,
            impact=_approval_impact(preview),
        )
    except (
        ApprovalLinkConfigurationError,
        PushNotificationError,
    ) as exc:
        raise ApiError(
            status_code=503,
            code="PUSH_APPROVAL_UNAVAILABLE",
            message=str(exc),
            retryable=True,
        ) from exc
    return correlated_payload(
        {
            "status": "notification-sent",
            "channel": ApprovalChannel.PUSH.value,
            "workspace_id": workspace_id,
            "decision_id": decision_id,
            "proposal_fingerprint": result.proposal_fingerprint,
            "proposal_instance_id": result.proposal_instance_id,
            "expires_at": result.expires_at.isoformat(),
            "provider": result.receipt.provider,
            "message_id": result.receipt.message_id,
            "delivery_mode": result.receipt.delivery_mode.value,
            "graph_mutated": False,
        }
    )


@app.get("/approvals/email/confirm", response_class=HTMLResponse)
def confirm_email_approval() -> HTMLResponse:
    return _approval_confirmation_response(
        channel=ApprovalChannel.EMAIL,
    )


@app.get("/approvals/push/confirm", response_class=HTMLResponse)
def confirm_push_approval() -> HTMLResponse:
    return _approval_confirmation_response(
        channel=ApprovalChannel.PUSH,
    )


@app.post("/approvals/email/redeem")
async def redeem_email_approval(request: Request) -> dict[str, object]:
    return await _redeem_approval_link(
        request=request,
        channel=ApprovalChannel.EMAIL,
    )


@app.post("/approvals/push/redeem")
async def redeem_push_approval(request: Request) -> dict[str, object]:
    return await _redeem_approval_link(
        request=request,
        channel=ApprovalChannel.PUSH,
    )


def _approval_confirmation_response(
    *,
    channel: ApprovalChannel,
) -> HTMLResponse:
    return HTMLResponse(
        approval_confirmation_html(channel=channel),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; form-action 'self'; "
                "script-src 'unsafe-inline'; style-src 'unsafe-inline'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _redeem_approval_link(
    *,
    request: Request,
    channel: ApprovalChannel,
) -> dict[str, object]:
    token = await _approval_token_from_request(request)
    try:
        redemption = _approval_link_redeemer().redeem(
            token=token,
            expected_channel=channel,
        )
    except ExpiredApprovalLink as exc:
        raise ApiError(
            status_code=410,
            code="APPROVAL_LINK_EXPIRED",
            message=str(exc),
        ) from exc
    except ReplayedApprovalLink as exc:
        raise ApiError(
            status_code=409,
            code="APPROVAL_LINK_ALREADY_USED",
            message=str(exc),
        ) from exc
    except InvalidApprovalLink as exc:
        raise ApiError(
            status_code=400,
            code="INVALID_APPROVAL_LINK",
            message=str(exc),
        ) from exc
    except ApprovalLinkConfigurationError as exc:
        raise ApiError(
            status_code=503,
            code="APPROVAL_LINKS_NOT_CONFIGURED",
            message=str(exc),
        ) from exc
    disposition = redemption.result.disposition
    if disposition is ApprovalDisposition.STALE_CONFIRMATION:
        raise ApiError(
            status_code=409,
            code="STALE_APPROVAL_LINK",
            message=(
                "The approval link no longer matches the pending proposal."
            ),
        )
    if disposition is ApprovalDisposition.IGNORED_NOT_AUTHORIZED:
        raise ApiError(
            status_code=403,
            code="APPROVER_NOT_AUTHORIZED",
            message=(
                "The link recipient does not currently hold the required "
                "Hexclave permission."
            ),
        )
    if (
        disposition
        is ApprovalDisposition.PERMISSION_CHECK_UNAVAILABLE
    ):
        raise ApiError(
            status_code=503,
            code="PERMISSION_CHECK_UNAVAILABLE",
            message="Hexclave permission verification is unavailable.",
            retryable=True,
        )
    if not redemption.result.approved:
        raise ApiError(
            status_code=409,
            code="APPROVAL_LINK_REJECTED",
            message="The exact proposal could not be approved.",
        )
    return correlated_payload(
        {
            "status": "approved",
            "disposition": disposition.value,
            "workspace_id": redemption.claims.workspace_id,
            "decision_id": redemption.claims.decision_id,
            "channel": redemption.claims.channel.value,
        }
    )


async def _approval_token_from_request(request: Request) -> str:
    query_token = request.query_params.get("token")
    if query_token is not None:
        raise ApiError(
            status_code=400,
            code="APPROVAL_TOKEN_QUERY_FORBIDDEN",
            message="Approval tokens must be submitted in the request body.",
        )
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    body = await request.body()
    if len(body) > 10_000:
        raise ApiError(
            status_code=413,
            code="APPROVAL_LINK_REQUEST_TOO_LARGE",
            message="The approval-link request is too large.",
        )
    body_token: str | None = None
    if body:
        if content_type == "application/x-www-form-urlencoded":
            try:
                values = parse_qs(
                    body.decode("utf-8"),
                    keep_blank_values=True,
                    strict_parsing=True,
                )
            except (UnicodeDecodeError, ValueError) as exc:
                raise ApiError(
                    status_code=400,
                    code="INVALID_APPROVAL_LINK_REQUEST",
                    message="The approval-link request is malformed.",
                ) from exc
            candidates = values.get("token", [])
            if len(candidates) == 1:
                body_token = candidates[0]
        elif content_type == "application/json":
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, ValueError) as exc:
                raise ApiError(
                    status_code=400,
                    code="INVALID_APPROVAL_LINK_REQUEST",
                    message="The approval-link request is malformed.",
                ) from exc
            if isinstance(payload, Mapping):
                value = payload.get("token")
                if isinstance(value, str):
                    body_token = value
    token = body_token
    if not isinstance(token, str) or not token.strip():
        raise ApiError(
            status_code=400,
            code="APPROVAL_LINK_REQUIRED",
            message="An approval token is required.",
        )
    return token


def _process_slack_delivery(
    *,
    workspace_id: str,
    body: bytes,
    headers: Mapping[str, Any],
) -> SlackIntakeOutcome:
    verifier = _live_slack_verifier()
    try:
        message = verifier.parse_message(body=body, headers=headers)
    except SlackWebhookError as exc:
        raise ApiError(
            status_code=401,
            code="INVALID_COMPOSIO_WEBHOOK",
            message=str(exc),
        ) from exc

    context_id = f"live-{workspace_id}"
    try:
        state = workspace_contexts.state(context_id)
    except DynamicAuthorityContextNotFound as exc:
        raise ApiError(
            status_code=404,
            code="WORKSPACE_CONTEXT_NOT_FOUND",
            message="Import and authorize the Workspace before enabling Slack intake.",
        ) from exc
    binding = state.slack_binding
    if (
        binding is None
        or binding.workspace_id != workspace_id
        or message.team_id != binding.slack_team_id
        or message.connection_user_id
        != binding.composio_connection_user_id
    ):
        raise ApiError(
            status_code=403,
            code="SLACK_WORKSPACE_BINDING_MISMATCH",
            message="The signed Slack event is not bound to this Workspace.",
        )
    authority_user_id = binding.hexclave_user_for(message.author_user_id)
    if authority_user_id is None:
        raise ApiError(
            status_code=403,
            code="SLACK_IDENTITY_BINDING_NOT_FOUND",
            message=(
                "The signed Slack author has no provisioned Hexclave identity "
                "for this Workspace."
            ),
        )
    if _has_webhook_delivery_id(headers):
        _reserve_slack_delivery(
            _delivery_key(
                workspace_id=workspace_id,
                connection_user_id=message.connection_user_id,
                trigger_kind=CHANNEL_MESSAGE_SLUG,
                event_id=message.event_id,
            )
        )
    try:
        extractor = build_decision_extractor(settings)
    except LLMProviderConfigurationError as exc:
        raise ApiError(
            status_code=503,
            code="EXTRACTION_NOT_CONFIGURED",
            message=str(exc),
        ) from exc
    if extractor is None:
        raise ApiError(
            status_code=503,
            code="EXTRACTION_NOT_CONFIGURED",
            message=(
                "Live Slack intake requires LLM_PROVIDER=gemini or "
                "LLM_PROVIDER=venice."
            ),
        )
    try:
        checker = _hexclave_permission_checker(binding.hexclave_team_id)
    except HexclaveConfigurationError as exc:
        raise ApiError(
            status_code=503,
            code="HEXCLAVE_NOT_CONFIGURED",
            message=str(exc),
        ) from exc
    gate = DeterministicIntakeGate(
        known_scopes=set(state.authority_policy),
        authority_policy=state.authority_policy,
        permission_checker=checker,
        confidence_threshold=settings.authority_threshold,
    )
    decisions = [
        artifact
        for artifact in state.artifacts
        if artifact.kind is ArtifactKind.DECISION
    ]
    try:
        return SlackDecisionIntake(
            verifier=verifier,
            extractor=extractor,
            gate=gate,
        ).ingest_verified(
            workspace_id=workspace_id,
            message=message,
            current_decisions=decisions,
            authority_user_id=authority_user_id,
            supersession_root_id=state.baseline_decision_id,
        )
    except DecisionDraftError as exc:
        raise ApiError(
            status_code=422,
            code="INVALID_DECISION_DRAFT",
            message=str(exc),
        ) from exc


def _process_slack_reaction(
    *,
    workspace_id: str,
    body: bytes,
    headers: Mapping[str, Any],
) -> tuple[VerifiedSlackReaction, WorkspaceSlackBinding, SlackDeliveryKey]:
    verifier = _live_slack_verifier()
    try:
        reaction = verifier.parse_reaction(body=body, headers=headers)
    except SlackWebhookError as exc:
        raise ApiError(
            status_code=401,
            code="INVALID_COMPOSIO_WEBHOOK",
            message=str(exc),
        ) from exc

    try:
        state = workspace_contexts.state(f"live-{workspace_id}")
    except DynamicAuthorityContextNotFound as exc:
        raise ApiError(
            status_code=404,
            code="WORKSPACE_CONTEXT_NOT_FOUND",
            message="Import and authorize the Workspace before enabling Slack intake.",
        ) from exc
    binding = state.slack_binding
    if (
        binding is None
        or binding.workspace_id != workspace_id
        or reaction.team_id != binding.slack_team_id
        or reaction.connection_user_id
        != binding.composio_connection_user_id
    ):
        raise ApiError(
            status_code=403,
            code="SLACK_WORKSPACE_BINDING_MISMATCH",
            message="The signed Slack event is not bound to this Workspace.",
        )
    key = _delivery_key(
        workspace_id=workspace_id,
        connection_user_id=reaction.connection_user_id,
        trigger_kind=REACTION_ADDED_SLUG,
        event_id=reaction.event_id,
    )
    if _has_webhook_delivery_id(headers):
        _reserve_slack_delivery(key)
    return reaction, binding, key


@app.post("/intake/slack/{workspace_id}")
async def intake_slack(
    workspace_id: str,
    request: Request,
) -> dict[str, object]:
    """Verify, extract, gate, and store an unapproved Workspace proposal."""

    body = await request.body()
    try:
        outcome = _process_slack_delivery(
            workspace_id=workspace_id,
            body=body,
            headers=request.headers,
        )
    except _SlackReplayDuplicate as duplicate:
        return _duplicate_delivery_result(duplicate)
    delivery_key = _delivery_key(
        workspace_id=workspace_id,
        connection_user_id=outcome.message.connection_user_id,
        trigger_kind=CHANNEL_MESSAGE_SLUG,
        event_id=outcome.message.event_id,
    )
    if outcome.proposal is None:
        result: dict[str, object] = {
            "status": "parked",
            "workspace_id": workspace_id,
            "event_id": outcome.message.event_id,
            "reasons": [reason.value for reason in outcome.gate.reasons],
            "graph_mutated": False,
        }
        if _has_webhook_delivery_id(request.headers):
            _complete_slack_delivery(delivery_key, result)
        return correlated_payload(result)
    workspace = post_model(
        url=(
            f"{settings.agent_url}/live-workspaces/"
            f"{quote(workspace_id, safe='')}/decisions/propose"
        ),
        payload=outcome.proposal,
        response_model=LiveWorkspaceView,
        upstream_name="Agent service",
        upstream_code="AGENT",
        timeout_seconds=settings.service_timeout_seconds,
    )
    result = {
        "status": "pending-human-confirmation",
        "workspace_id": workspace_id,
        "event_id": outcome.message.event_id,
        "decision_id": outcome.proposal.decision.id,
        "affected_scopes": sorted(outcome.proposal.affected_scopes),
        "permission_id": outcome.gate.permission_id,
        "graph_version": workspace.graph_version,
        "graph_mutated": False,
        "human_reviewed": False,
    }
    # A real agent response always carries the persisted pending mutation. Some
    # focused boundary tests inject a minimal response and intentionally stop
    # before external notification composition.
    if getattr(workspace, "pending_mutation", None) is not None:
        notification = _notify_slack_pending(outcome=outcome, body=body)
        result.update(
            {
                "notification_status": "posted",
                "approval_message_ts": notification.receipt.message_id,
                "notification_delivery_mode": (
                    notification.receipt.delivery_mode.value
                ),
                "would_interrupt_assignment_ids": list(
                    notification.preview.interrupted_assignment_ids
                ),
                "would_preserve_assignment_ids": list(
                    notification.preview.preserved_assignment_ids
                ),
            }
        )
    if _has_webhook_delivery_id(request.headers):
        _complete_slack_delivery(delivery_key, result)
    return correlated_payload(result)


@app.post("/intake/slack/{workspace_id}/reaction")
async def intake_slack_reaction(
    workspace_id: str,
    request: Request,
) -> dict[str, object]:
    """Verify and route one reaction through the shared approval coordinator."""

    body = await request.body()
    try:
        reaction, binding, delivery_key = _process_slack_reaction(
            workspace_id=workspace_id,
            body=body,
            headers=request.headers,
        )
    except _SlackReplayDuplicate as duplicate:
        return _duplicate_delivery_result(duplicate)

    handled: SlackReactionResult = SlackReactionApprovalHandler(
        pending_resolver=_slack_approval_threads(),
        identity_resolver=_BoundSlackReactionIdentityResolver(binding),
        coordinator=_AgentChannelApprovalPort(),
    ).handle(reaction)
    if not handled.handled:
        result: dict[str, object] = {
            "status": "ignored",
            "reason": "no-qualifying-pending-approval",
            "workspace_id": workspace_id,
            "event_id": reaction.event_id,
            "graph_mutated": False,
        }
    elif handled.approval is None:
        result = {
            "status": "ignored",
            "reason": "unmapped-slack-identity",
            "workspace_id": workspace_id,
            "event_id": reaction.event_id,
            "graph_mutated": False,
        }
    else:
        disposition = handled.approval.disposition
        result = {
            "status": (
                "approved" if handled.approval.approved else "ignored"
            ),
            "reason": disposition.value,
            "workspace_id": workspace_id,
            "event_id": reaction.event_id,
            "decision_id": (
                handled.approval.evidence.decision_id
                if handled.approval.evidence is not None
                else None
            ),
            "graph_mutated": disposition is ApprovalDisposition.APPROVED,
        }
    if _has_webhook_delivery_id(request.headers):
        _complete_slack_delivery(delivery_key, result)
    return correlated_payload(result)


@app.post("/authorize")
def authorize(request: AuthorizationRequest):
    with runtime_lock:
        result = runtime.authority.evaluate_plan(
            run_id=request.run_id,
            task_id=request.task_id,
            plan=request.plan,
        )
    return correlated_payload(result)


@app.post("/grants/verify")
def verify_grant(request: GrantVerificationRequest):
    with runtime_lock:
        result = runtime.authority.verify_grant(
            token=request.token,
            run_id=request.run_id,
            task_id=request.task_id,
            plan=request.plan,
        )
    return correlated_payload(result)


@app.post("/scenario-lab/authority/contexts", status_code=201)
def create_scenario_authority_context(
    request: ScenarioAuthorityContextCreateRequest,
) -> dict[str, object]:
    try:
        state = scenario_contexts.create(request)
    except ScenarioDefinitionNotFound as exc:
        raise ApiError(
            status_code=404,
            code="SCENARIO_NOT_FOUND",
            message="The requested scenario does not exist.",
        ) from exc
    except ScenarioAuthorityContextConflict as exc:
        raise ApiError(
            status_code=409,
            code="SCENARIO_CONTEXT_CONFLICT",
            message=str(exc),
        ) from exc
    return correlated_payload(state)


@app.get("/scenario-lab/authority/contexts/{context_id}")
def scenario_authority_context_state(context_id: str) -> dict[str, object]:
    try:
        state = scenario_contexts.state(context_id)
    except ScenarioAuthorityContextNotFound as exc:
        raise ApiError(
            status_code=404,
            code="SCENARIO_CONTEXT_NOT_FOUND",
            message="The requested Scenario Lab authority context does not exist.",
        ) from exc
    return correlated_payload(state)


@app.delete("/scenario-lab/authority/contexts/{context_id}")
def delete_scenario_authority_context(context_id: str) -> dict[str, object]:
    try:
        scenario_contexts.delete(context_id)
    except ScenarioAuthorityContextNotFound as exc:
        raise ApiError(
            status_code=404,
            code="SCENARIO_CONTEXT_NOT_FOUND",
            message="The requested Scenario Lab authority context does not exist.",
        ) from exc
    return correlated_payload({"context_id": context_id, "deleted": True})


@app.post("/scenario-lab/authority/contexts/{context_id}/mutation")
def apply_scenario_authority_mutation(context_id: str) -> dict[str, object]:
    try:
        result = scenario_contexts.apply_mutation(context_id)
    except ScenarioAuthorityContextNotFound as exc:
        raise ApiError(
            status_code=404,
            code="SCENARIO_CONTEXT_NOT_FOUND",
            message="The requested Scenario Lab authority context does not exist.",
        ) from exc
    except ScenarioAuthorityContextConflict as exc:
        raise ApiError(
            status_code=409,
            code="SCENARIO_CONTEXT_CONFLICT",
            message=str(exc),
        ) from exc
    return correlated_payload(result)


@app.post("/scenario-lab/authority/contexts/{context_id}/authorize")
def authorize_in_scenario_context(
    context_id: str,
    request: AuthorizationRequest,
) -> dict[str, object]:
    try:
        result = scenario_contexts.authorize(context_id, request)
    except ScenarioAuthorityContextNotFound as exc:
        raise ApiError(
            status_code=404,
            code="SCENARIO_CONTEXT_NOT_FOUND",
            message="The requested Scenario Lab authority context does not exist.",
        ) from exc
    return correlated_payload(result)


@app.post("/scenario-lab/authority/contexts/{context_id}/grants/verify")
def verify_grant_in_scenario_context(
    context_id: str,
    request: GrantVerificationRequest,
) -> dict[str, object]:
    try:
        result = scenario_contexts.verify_grant(context_id, request)
    except ScenarioAuthorityContextNotFound as exc:
        raise ApiError(
            status_code=404,
            code="SCENARIO_CONTEXT_NOT_FOUND",
            message="The requested Scenario Lab authority context does not exist.",
        ) from exc
    return correlated_payload(result)


def _workspace_context_error(exc: Exception) -> ApiError:
    if isinstance(exc, DynamicAuthorityContextNotFound):
        return ApiError(
            status_code=404,
            code="WORKSPACE_CONTEXT_NOT_FOUND",
            message=str(exc),
        )
    return ApiError(
        status_code=409,
        code="WORKSPACE_CONTEXT_CONFLICT",
        message=str(exc),
    )


@app.post("/live-workspaces/authority/contexts", status_code=201)
def create_workspace_authority_context(
    context: DynamicAuthorityContextCreateRequest,
    request: Request,
) -> dict[str, object]:
    require_internal_service(request, secret=settings.grant_secret)
    try:
        state = workspace_contexts.create(context)
    except DynamicAuthorityContextConflict as exc:
        raise _workspace_context_error(exc) from exc
    return correlated_payload(state)


@app.get("/live-workspaces/authority/contexts/{context_id}")
def workspace_authority_context_state(
    context_id: str,
    request: Request,
) -> dict[str, object]:
    try:
        state = workspace_contexts.state(context_id)
    except DynamicAuthorityContextNotFound as exc:
        raise _workspace_context_error(exc) from exc
    try:
        require_internal_service(request, secret=settings.grant_secret)
    except ApiError:
        # Preserve the existing read-only graph diagnostics without disclosing
        # Slack↔Hexclave identity mappings or durable human evidence.
        public_state = state.model_dump(mode="json")
        public_state["slack_binding"] = None
        public_state["approval_evidence"] = {}
        return correlated_payload(public_state)
    return correlated_payload(state)


@app.delete("/live-workspaces/authority/contexts/{context_id}")
def delete_workspace_authority_context(
    context_id: str,
    request: Request,
) -> dict[str, object]:
    require_internal_service(request, secret=settings.grant_secret)
    try:
        workspace_contexts.delete(context_id)
    except DynamicAuthorityContextNotFound as exc:
        raise _workspace_context_error(exc) from exc
    return correlated_payload({"context_id": context_id, "deleted": True})


@app.post("/live-workspaces/authority/contexts/{context_id}/baseline/approve")
def approve_workspace_baseline(
    context_id: str,
    approval: WorkspaceApprovalRequest,
    request: Request,
) -> dict[str, object]:
    require_internal_service(request, secret=settings.grant_secret)
    try:
        state = workspace_contexts.approve_baseline(context_id, approval)
    except (DynamicAuthorityContextNotFound, DynamicAuthorityContextConflict) as exc:
        raise _workspace_context_error(exc) from exc
    return correlated_payload(state)


@app.post("/live-workspaces/authority/contexts/{context_id}/mutations/approve")
def approve_workspace_mutation(
    context_id: str,
    approval: DynamicMutationApprovalRequest,
    request: Request,
) -> dict[str, object]:
    require_internal_service(request, secret=settings.grant_secret)
    try:
        result = workspace_contexts.approve_mutation(context_id, approval)
    except (DynamicAuthorityContextNotFound, DynamicAuthorityContextConflict) as exc:
        raise _workspace_context_error(exc) from exc
    return correlated_payload(result)


@app.post("/live-workspaces/authority/contexts/{context_id}/authorize")
def authorize_in_workspace_context(
    context_id: str,
    request: AuthorizationRequest,
) -> dict[str, object]:
    try:
        result = workspace_contexts.authorize(context_id, request)
    except DynamicAuthorityContextNotFound as exc:
        raise _workspace_context_error(exc) from exc
    return correlated_payload(result)


@app.post("/live-workspaces/authority/contexts/{context_id}/grants/verify")
def verify_grant_in_workspace_context(
    context_id: str,
    request: GrantVerificationRequest,
) -> dict[str, object]:
    try:
        result = workspace_contexts.verify_grant(context_id, request)
    except DynamicAuthorityContextNotFound as exc:
        raise _workspace_context_error(exc) from exc
    return correlated_payload(result)
