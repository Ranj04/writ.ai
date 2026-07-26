from __future__ import annotations

import base64
import hashlib
import json
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from svix.webhooks import Webhook
from writai.auth.hexclave import (
    HexclavePermissionChecker,
    TeamPermissionRequest,
    TeamPermissionResponse,
)
from writai.auth.hexclave_webhooks import (
    HEXCLAVE_WEBHOOK_MAX_BODY_BYTES,
    HexclaveWebhookConfigurationError,
    HexclaveWebhookPayloadError,
    HexclaveWebhookVerification,
    HexclaveWebhookVerificationError,
    JsonHexclaveWebhookEvidenceStore,
    SvixHexclaveWebhookVerifier,
    VerifiedHexclaveAuthorityEvent,
)
from writai.services import agent_api, authority_api
from writai.services.support import (
    INTERNAL_SERVICE_AUTH_HEADER,
    ApiError,
    internal_service_token,
)

SECRET_BYTES = b"writai-hexclave-webhook-test-secret"
WEBHOOK_SECRET = "whsec_" + base64.b64encode(SECRET_BYTES).decode()


@pytest.mark.parametrize("secret", [None, "", "whsec_", "whsec_not-base64***"])
def test_webhook_verifier_rejects_invalid_secret_configuration(
    secret: str | None,
) -> None:
    with pytest.raises(HexclaveWebhookConfigurationError):
        SvixHexclaveWebhookVerifier(secret=secret)


def _signed_delivery(
    event_type: str,
    data: dict[str, object],
    *,
    delivery_id: str = "evt-hexclave-001",
    delivered_at: datetime | None = None,
) -> tuple[bytes, dict[str, str]]:
    timestamp = delivered_at or datetime.now(UTC)
    body = json.dumps(
        {"type": event_type, "data": data},
        separators=(",", ":"),
    ).encode()
    return body, {
        "svix-id": delivery_id,
        "svix-timestamp": str(int(timestamp.timestamp())),
        "svix-signature": Webhook(WEBHOOK_SECRET).sign(
            delivery_id,
            timestamp,
            body.decode(),
        ),
    }


@pytest.mark.parametrize(
    ("event_type", "data", "permission_id"),
    [
        (
            "team_permission.created",
            {
                "id": "approve_compliance",
                "team_id": "team-001",
                "user_id": "user-001",
            },
            "approve_compliance",
        ),
        (
            "team_permission.deleted",
            {
                "id": "approve_compliance",
                "team_id": "team-001",
                "user_id": "user-001",
            },
            "approve_compliance",
        ),
        (
            "team_membership.created",
            {"team_id": "team-001", "user_id": "user-001"},
            None,
        ),
        (
            "team_membership.deleted",
            {"team_id": "team-001", "user_id": "user-001"},
            None,
        ),
    ],
)
def test_svix_verifier_accepts_each_documented_authority_event(
    event_type: str,
    data: dict[str, object],
    permission_id: str | None,
) -> None:
    body, headers = _signed_delivery(event_type, data)

    verified = SvixHexclaveWebhookVerifier(secret=WEBHOOK_SECRET).verify(
        body=body,
        headers=headers,
    )

    assert verified.event is not None
    assert verified.event.event_type == event_type
    assert verified.event.team_id == "team-001"
    assert verified.event.user_id == "user-001"
    assert verified.event.permission_id == permission_id
    assert verified.event.payload_sha256 == hashlib.sha256(body).hexdigest()


def test_svix_verifier_rejects_tampered_and_stale_deliveries() -> None:
    body, headers = _signed_delivery(
        "team_membership.deleted",
        {"team_id": "team-001", "user_id": "user-001"},
    )
    verifier = SvixHexclaveWebhookVerifier(secret=WEBHOOK_SECRET)

    with pytest.raises(HexclaveWebhookVerificationError):
        verifier.verify(body=body + b" ", headers=headers)

    stale_body, stale_headers = _signed_delivery(
        "team_membership.deleted",
        {"team_id": "team-001", "user_id": "user-001"},
        delivered_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    with pytest.raises(HexclaveWebhookVerificationError):
        verifier.verify(body=stale_body, headers=stale_headers)


def test_verified_unsupported_event_has_no_authority_event() -> None:
    body, headers = _signed_delivery(
        "user.updated",
        {"team_id": "team-001", "user_id": "user-001"},
    )

    verified = SvixHexclaveWebhookVerifier(secret=WEBHOOK_SECRET).verify(
        body=body,
        headers=headers,
    )

    assert verified.event_type == "user.updated"
    assert verified.event is None


def test_verified_supported_event_requires_documented_identifiers() -> None:
    body, headers = _signed_delivery(
        "team_permission.deleted",
        {"team_id": "team-001", "user_id": "user-001"},
    )

    with pytest.raises(HexclaveWebhookPayloadError):
        SvixHexclaveWebhookVerifier(secret=WEBHOOK_SECRET).verify(
            body=body,
            headers=headers,
        )


def test_verifier_accepts_legacy_headers_but_rejects_conflicting_aliases() -> None:
    body, svix_headers = _signed_delivery(
        "team_membership.deleted",
        {"team_id": "team-001", "user_id": "user-001"},
    )
    legacy_headers = {
        key.replace("svix-", "webhook-"): value
        for key, value in svix_headers.items()
    }
    verifier = SvixHexclaveWebhookVerifier(secret=WEBHOOK_SECRET)

    assert verifier.verify(body=body, headers=legacy_headers).event is not None

    with pytest.raises(HexclaveWebhookVerificationError):
        verifier.verify(
            body=body,
            headers={
                **svix_headers,
                "webhook-id": "conflicting-delivery-id",
            },
        )


def _verified_event(
    *,
    delivery_id: str = "evt-hexclave-route-001",
) -> VerifiedHexclaveAuthorityEvent:
    return VerifiedHexclaveAuthorityEvent(
        delivery_id=delivery_id,
        event_type="team_permission.deleted",
        delivered_at=datetime(2026, 7, 26, 3, 0, tzinfo=UTC),
        payload_sha256=hashlib.sha256(b"signed-redacted-body").hexdigest(),
        team_id="team-001",
        user_id="user-001",
        permission_id="approve_compliance",
    )


def test_evidence_store_is_idempotent_atomic_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "hexclave-webhook-events.json"
    store = JsonHexclaveWebhookEvidenceStore(path)
    event = _verified_event()
    initial_temporary_modes: list[int] = []
    original_replace = Path.replace

    def replace_with_mode_check(source: Path, target: Path) -> Path:
        initial_temporary_modes.append(
            stat.S_IMODE(source.stat().st_mode)
        )
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace_with_mode_check)

    assert store.reserve(event) == "reserved"
    completed = store.complete(
        event.delivery_id,
        authority_caches_cleared=2,
        agent_caches_cleared=3,
    )
    assert completed.status == "completed"
    assert store.reserve(event) == "completed"
    assert initial_temporary_modes == [0o600, 0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    persisted = path.read_text(encoding="utf-8")
    assert "signed-redacted-body" not in persisted
    assert WEBHOOK_SECRET not in persisted
    assert "verdict" not in persisted
    assert "approve_compliance" in persisted


class _StaticVerifier:
    def __init__(self, verification: HexclaveWebhookVerification) -> None:
        self.verification = verification

    def verify(self, **_kwargs: object) -> HexclaveWebhookVerification:
        return self.verification


def test_public_webhook_clears_both_services_and_completed_replay_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    delivery_id = "evt-hexclave-route-001"
    body, headers = _signed_delivery(
        "team_permission.deleted",
        {
            "id": "approve_compliance",
            "team_id": "team-001",
            "user_id": "user-001",
        },
        delivery_id=delivery_id,
    )
    store = JsonHexclaveWebhookEvidenceStore(tmp_path / "events.json")
    calls: list[str] = []

    def clear_authority_caches() -> int:
        calls.append("authority")
        return 2

    def clear_agent_caches(_request: object) -> int:
        calls.append("agent")
        return 3

    monkeypatch.setattr(
        authority_api,
        "_live_hexclave_webhook_verifier",
        lambda: SvixHexclaveWebhookVerifier(secret=WEBHOOK_SECRET),
    )
    monkeypatch.setattr(
        authority_api,
        "_hexclave_webhook_evidence_store",
        lambda: store,
    )
    monkeypatch.setattr(
        authority_api,
        "_clear_hexclave_permission_caches",
        clear_authority_caches,
    )
    monkeypatch.setattr(
        authority_api,
        "_invalidate_agent_hexclave_permission_cache",
        clear_agent_caches,
    )
    client = TestClient(authority_api.app)

    first = client.post("/webhooks/hexclave", content=body, headers=headers)
    second = client.post("/webhooks/hexclave", content=body, headers=headers)

    assert first.status_code == 200
    assert first.json()["graph_mutated"] is False
    assert first.json()["duplicate"] is False
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert calls == ["authority", "agent"]
    assert store.get(delivery_id).status == "completed"  # type: ignore[union-attr]


def test_unsupported_signed_event_has_no_cache_or_evidence_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verification = HexclaveWebhookVerification(
        delivery_id="evt-unsupported",
        event_type="user.updated",
    )
    monkeypatch.setattr(
        authority_api,
        "_live_hexclave_webhook_verifier",
        lambda: _StaticVerifier(verification),
    )
    monkeypatch.setattr(
        authority_api,
        "_hexclave_webhook_evidence_store",
        lambda: (_ for _ in ()).throw(AssertionError("must not store")),
    )
    monkeypatch.setattr(
        authority_api,
        "_clear_hexclave_permission_caches",
        lambda: (_ for _ in ()).throw(AssertionError("must not clear")),
    )

    response = TestClient(authority_api.app).post(
        "/webhooks/hexclave",
        content=b"signed",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["graph_mutated"] is False


def test_invalid_signature_has_no_cache_or_evidence_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body, headers = _signed_delivery(
        "team_membership.deleted",
        {"team_id": "team-001", "user_id": "user-001"},
    )
    headers["svix-signature"] = "v1,invalid"
    monkeypatch.setattr(
        authority_api,
        "_live_hexclave_webhook_verifier",
        lambda: SvixHexclaveWebhookVerifier(secret=WEBHOOK_SECRET),
    )
    monkeypatch.setattr(
        authority_api,
        "_hexclave_webhook_evidence_store",
        lambda: (_ for _ in ()).throw(AssertionError("must not store")),
    )
    monkeypatch.setattr(
        authority_api,
        "_clear_hexclave_permission_caches",
        lambda: (_ for _ in ()).throw(AssertionError("must not clear")),
    )

    response = TestClient(authority_api.app).post(
        "/webhooks/hexclave",
        content=body,
        headers=headers,
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_HEXCLAVE_WEBHOOK"


@pytest.mark.parametrize("streamed", [False, True])
def test_oversize_unsigned_body_stops_before_all_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    streamed: bool,
) -> None:
    monkeypatch.setattr(
        authority_api,
        "_live_hexclave_webhook_verifier",
        lambda: (_ for _ in ()).throw(AssertionError("must not verify")),
    )
    monkeypatch.setattr(
        authority_api,
        "_hexclave_webhook_evidence_store",
        lambda: (_ for _ in ()).throw(AssertionError("must not store")),
    )
    monkeypatch.setattr(
        authority_api,
        "_clear_hexclave_permission_caches",
        lambda: (_ for _ in ()).throw(AssertionError("must not clear")),
    )
    client = TestClient(authority_api.app)

    if streamed:
        def chunks():
            yield b"a" * HEXCLAVE_WEBHOOK_MAX_BODY_BYTES
            yield b"b"

        response = client.post(
            "/webhooks/hexclave",
            content=chunks(),
            headers={"transfer-encoding": "chunked"},
        )
    else:
        response = client.post(
            "/webhooks/hexclave",
            content=b"x",
            headers={
                "content-length": str(
                    HEXCLAVE_WEBHOOK_MAX_BODY_BYTES + 1
                )
            },
        )

    assert response.status_code == 400
    assert (
        response.json()["error"]["code"]
        == "INVALID_HEXCLAVE_WEBHOOK_PAYLOAD"
    )


def test_fanout_failure_is_retryable_and_later_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    event = _verified_event(delivery_id="evt-fanout-failure")
    store = JsonHexclaveWebhookEvidenceStore(tmp_path / "events.json")
    monkeypatch.setattr(
        authority_api,
        "_live_hexclave_webhook_verifier",
        lambda: _StaticVerifier(
            HexclaveWebhookVerification(
                delivery_id=event.delivery_id,
                event_type=event.event_type,
                event=event,
            )
        ),
    )
    monkeypatch.setattr(
        authority_api,
        "_hexclave_webhook_evidence_store",
        lambda: store,
    )
    monkeypatch.setattr(
        authority_api,
        "_clear_hexclave_permission_caches",
        lambda: 1,
    )
    fanout_attempts = 0

    def retrying_fanout(_request: object) -> int:
        nonlocal fanout_attempts
        fanout_attempts += 1
        if fanout_attempts == 1:
            raise ApiError(
                status_code=503,
                code="AGENT_PERMISSION_CACHE_UNAVAILABLE",
                message="unavailable",
                retryable=True,
            )
        return 2

    monkeypatch.setattr(
        authority_api,
        "_invalidate_agent_hexclave_permission_cache",
        retrying_fanout,
    )
    client = TestClient(authority_api.app)

    first = client.post(
        "/webhooks/hexclave",
        content=b"signed",
    )
    assert first.status_code == 503
    assert first.json()["error"]["retryable"] is True
    assert store.get(event.delivery_id).status == "reserved"  # type: ignore[union-attr]

    second = client.post(
        "/webhooks/hexclave",
        content=b"signed",
    )
    assert second.status_code == 200
    assert second.json()["duplicate"] is False
    completed = store.get(event.delivery_id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.authority_caches_cleared == 1
    assert completed.agent_caches_cleared == 2
    assert fanout_attempts == 2


class _RecordingChecker:
    def __init__(self) -> None:
        self.clear_calls = 0

    def clear_cache(self) -> None:
        self.clear_calls += 1


def _invalidation_body() -> dict[str, str]:
    return {
        "delivery_id": "evt-internal-001",
        "event_type": "team_membership.deleted",
        "evidence_ref": "hexclave-webhook://evt-internal-001",
    }


def test_internal_agent_cache_clear_requires_service_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _RecordingChecker()
    monkeypatch.setattr(
        agent_api,
        "hexclave_permission_checkers",
        {("cache",): checker},
    )
    client = TestClient(agent_api.app)

    rejected = client.post(
        "/internal/hexclave/permission-cache/invalidate",
        json=_invalidation_body(),
    )
    accepted = client.post(
        "/internal/hexclave/permission-cache/invalidate",
        json=_invalidation_body(),
        headers={
            INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
                agent_api.settings.grant_secret
            )
        },
    )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["caches_cleared"] == 1
    assert accepted.json()["graph_mutated"] is False
    assert checker.clear_calls == 1


@dataclass
class _MutablePermissionTransport:
    allowed: bool = True
    calls: int = 0

    def get_team_permission(
        self,
        request: TeamPermissionRequest,
        *,
        secret_key: str,
        timeout_seconds: float,
    ) -> TeamPermissionResponse:
        del request, secret_key, timeout_seconds
        self.calls += 1
        return TeamPermissionResponse(allowed=self.allowed)


def test_internal_invalidation_turns_cached_allow_into_fresh_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _MutablePermissionTransport()
    checker = HexclavePermissionChecker(
        project_id="project-001",
        secret_key="secret-server-key",
        team_id="team-001",
        transport=transport,
    )
    assert checker.has_permission(
        user_id="user-001",
        permission_id="approve_compliance",
    )
    transport.allowed = False
    monkeypatch.setattr(
        agent_api,
        "hexclave_permission_checkers",
        {("cache",): checker},
    )

    response = TestClient(agent_api.app).post(
        "/internal/hexclave/permission-cache/invalidate",
        json=_invalidation_body(),
        headers={
            INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
                agent_api.settings.grant_secret
            )
        },
    )

    assert response.status_code == 200
    assert not checker.has_permission(
        user_id="user-001",
        permission_id="approve_compliance",
    )
    assert transport.calls == 2
