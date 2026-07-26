from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import writai.services.authority_api as authority_api
import pytest
from writai.intake.approval import (
    ApprovalChannel,
    ApprovalCoordinator,
    ApprovalDisposition,
    ApprovalEvidence,
    ApprovalResult,
    PendingApproval,
)
from writai.notify.email import (
    ApprovalDeliveryProvider,
    ApprovalEmail,
    ApprovalLinkClaims,
    ApprovalLinkRedeemer,
    ApprovalLinkSigner,
    EmailDeliveryReceipt,
    InMemoryApprovalLinkUseStore,
    NotificationDeliveryMode,
)
from writai.notify.push import (
    PushApprovalMessage,
    PushDeliveryReceipt,
)
from writai.services.support import (
    INTERNAL_SERVICE_AUTH_HEADER,
    internal_service_token,
)
from writai.workspaces.models import WorkspaceApprovalPreview
from fastapi.testclient import TestClient

NOW = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
SECRET = "route-link-test-secret-that-is-over-32-bytes"


class Checker:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, str]] = []

    def has_permission(self, *, user_id: str, permission_id: str) -> bool:
        self.calls.append((user_id, permission_id))
        return self.allowed


class WorkspacePort:
    def __init__(self) -> None:
        self.calls: list[tuple[PendingApproval, ApprovalEvidence]] = []

    def approve_decision(
        self,
        *,
        pending: PendingApproval,
        evidence: ApprovalEvidence,
    ) -> dict[str, object]:
        self.calls.append((pending, evidence))
        return {
            "id": pending.workspace_id,
            "supervisor": {"assignments": []},
        }


class CoordinatorBoundPort:
    def __init__(
        self,
        *,
        current: PendingApproval,
        coordinator: ApprovalCoordinator,
    ) -> None:
        self.current = current
        self.coordinator = coordinator

    def approve_bound(
        self,
        *,
        claims: ApprovalLinkClaims,
        evidence_ref: str,
    ) -> ApprovalResult:
        if (
            claims.proposal_fingerprint
            != self.current.proposal_fingerprint
            or claims.proposal_instance_id
            != self.current.proposal_instance_id
        ):
            return ApprovalResult(
                disposition=ApprovalDisposition.STALE_CONFIRMATION
            )
        return self.coordinator.approve(
            pending=self.current,
            approver_user_id=claims.approver_user_id,
            channel=claims.channel,
            evidence_ref=evidence_ref,
        )


class AcceptingRecipientVerifier:
    def verify(self, claims: ApprovalLinkClaims) -> None:
        del claims


class RecordingEmailSender:
    delivery_mode = NotificationDeliveryMode.SIMULATED

    def __init__(self) -> None:
        self.messages: list[ApprovalEmail] = []

    def send(self, message: ApprovalEmail) -> EmailDeliveryReceipt:
        self.messages.append(message)
        return EmailDeliveryReceipt(
            provider="fixture",
            message_id="EMAIL-ROUTE-1",
            delivery_mode=self.delivery_mode,
        )


class RecordingPushSender:
    provider = "ntfy"
    delivery_mode = NotificationDeliveryMode.SIMULATED
    recipient_binding_ref = "fixture://private-topic"

    def __init__(self) -> None:
        self.messages: list[PushApprovalMessage] = []

    def send(self, message: PushApprovalMessage) -> PushDeliveryReceipt:
        self.messages.append(message)
        return PushDeliveryReceipt(
            provider="ntfy",
            message_id="PUSH-ROUTE-1",
            delivery_mode=self.delivery_mode,
        )


def _pending(
    *,
    fingerprint_character: str = "a",
    instance_id: str = "proposal-instance-1",
) -> PendingApproval:
    return PendingApproval(
        workspace_id="csv-exports",
        decision_id="DEC-018",
        supersedes_id="DEC-004",
        affected_scopes=frozenset({"export.authorization"}),
        permission_id="approve_compliance",
        source_ref="slack://T1/C1/1",
        title="Admin-only exports",
        text="Exports must be admin-only.",
        effective_at=datetime(2026, 7, 25, 7, 30, tzinfo=UTC),
        requirements={
            "export.authorization": {"audience": "admin_only"}
        },
        proposal_fingerprint=(
            "sha256:" + (fingerprint_character * 64)
        ),
        proposal_instance_id=instance_id,
        evidence_refs=("slack://T1/C1/1",),
    )


def _signer(channel_id: str) -> ApprovalLinkSigner:
    return ApprovalLinkSigner(
        secret=SECRET,
        ttl_seconds=900,
        clock=lambda: NOW,
        token_id_factory=lambda: f"{channel_id}-token-id-0000001",
    )


def _redeemer(
    *,
    signer: ApprovalLinkSigner,
    current: PendingApproval,
    allowed: bool = True,
) -> tuple[ApprovalLinkRedeemer, Checker, WorkspacePort]:
    checker = Checker(allowed)
    workspace = WorkspacePort()
    coordinator = ApprovalCoordinator(
        permission_checker=checker,
        workspace_port=workspace,
        clock=lambda: NOW,
    )
    return (
        ApprovalLinkRedeemer(
            signer=signer,
            use_store=InMemoryApprovalLinkUseStore(),
            approval_port=CoordinatorBoundPort(
                current=current,
                coordinator=coordinator,
            ),
            recipient_verifier=AcceptingRecipientVerifier(),
            clock=lambda: NOW,
        ),
        checker,
        workspace,
    )


def test_email_confirmation_get_is_scanner_safe_and_post_is_single_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = _signer("email")
    token = signer.issue(
        pending=_pending(),
        approver_user_id="hex-compliance",
        channel=ApprovalChannel.EMAIL,
        delivery_provider=ApprovalDeliveryProvider.EMAIL,
        recipient_ref="lead@example.com",
    )
    redeemer, checker, workspace = _redeemer(
        signer=signer,
        current=_pending(),
    )
    monkeypatch.setattr(
        authority_api,
        "_approval_link_redeemer",
        lambda: redeemer,
    )
    client = TestClient(authority_api.app)

    preview = client.get(
        "/approvals/email/confirm",
        params={"token": token},
    )
    scanner_retry = client.get(
        "/approvals/email/confirm",
        params={"token": token},
    )

    assert preview.status_code == 200
    assert scanner_retry.status_code == 200
    assert 'method="post"' in preview.text
    assert preview.headers["cache-control"] == "no-store"
    assert preview.headers["referrer-policy"] == "no-referrer"
    assert checker.calls == []
    assert workspace.calls == []

    approved = client.post(
        "/approvals/email/redeem",
        data={"token": token},
    )
    replay = client.post(
        "/approvals/email/redeem",
        data={"token": token},
    )

    assert approved.status_code == 200
    assert approved.json()["disposition"] == "APPROVED"
    assert approved.json()["channel"] == "email"
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == (
        "APPROVAL_LINK_ALREADY_USED"
    )
    assert checker.calls == [
        ("hex-compliance", "approve_compliance")
    ]
    assert len(workspace.calls) == 1


@pytest.mark.parametrize(
    ("allowed", "current", "expected_status", "expected_code"),
    [
        (
            False,
            _pending(),
            403,
            "APPROVER_NOT_AUTHORIZED",
        ),
        (
            True,
            _pending(
                fingerprint_character="b",
                instance_id="proposal-instance-2",
            ),
            409,
            "STALE_APPROVAL_LINK",
        ),
    ],
)
def test_push_redemption_maps_unauthorized_and_stale_without_bypass(
    monkeypatch: pytest.MonkeyPatch,
    allowed: bool,
    current: PendingApproval,
    expected_status: int,
    expected_code: str,
) -> None:
    signer = _signer("push")
    token = signer.issue(
        pending=_pending(),
        approver_user_id="hex-recipient",
        channel=ApprovalChannel.PUSH,
        delivery_provider=ApprovalDeliveryProvider.NTFY,
        recipient_ref="ntfy:private-topic",
    )
    redeemer, checker, workspace = _redeemer(
        signer=signer,
        current=current,
        allowed=allowed,
    )
    monkeypatch.setattr(
        authority_api,
        "_approval_link_redeemer",
        lambda: redeemer,
    )
    client = TestClient(authority_api.app)

    response = client.post(
        "/approvals/push/redeem",
        data={"token": token},
    )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert workspace.calls == []
    if expected_code == "STALE_APPROVAL_LINK":
        assert checker.calls == []
    else:
        assert checker.calls == [
            ("hex-recipient", "approve_compliance")
        ]


def test_internal_dispatch_routes_require_hmac_and_never_return_link_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    preview = WorkspaceApprovalPreview(
        pending=_pending(),
        interrupted_assignment_ids=("ASSIGN-2",),
        preserved_assignment_ids=("ASSIGN-1",),
        interrupted_count=1,
        preserved_count=1,
        total_assignment_count=2,
        assignment_provenance_paths={
            "ASSIGN-2": ("DEC-018", "DEC-004", "TASK-2")
        },
    )
    email_sender = RecordingEmailSender()
    push_sender = RecordingPushSender()
    test_secret = "internal-service-test-secret"
    monkeypatch.setattr(
        authority_api,
        "settings",
        replace(
            authority_api.settings,
            grant_secret=test_secret,
            approval_link_secret=SECRET,
            approval_link_store=str(tmp_path / "link-uses.json"),
            public_base_url="https://approval.example",
            email_from="approvals@example.com",
            approval_recipient_bindings=(
                '{"schema_version":1,"recipients":[{'
                '"workspace_id":"csv-exports",'
                '"approver_user_id":"hex-compliance",'
                '"email":"lead@example.com",'
                '"ntfy_topic":"writai-7F3k9Q2mR8xP4vN6cL1s"}]}'
            ),
        ),
    )
    monkeypatch.setattr(
        authority_api,
        "_agent_decision_preview",
        lambda **_kwargs: preview,
    )
    monkeypatch.setattr(
        authority_api,
        "ResendEmailSender",
        lambda **_kwargs: email_sender,
    )
    monkeypatch.setattr(
        authority_api,
        "NtfyPushSender",
        lambda **_kwargs: push_sender,
    )
    client = TestClient(authority_api.app)
    email_path = (
        "/internal/live-workspaces/csv-exports/decisions/"
        "DEC-018/notifications/email"
    )
    push_path = (
        "/internal/live-workspaces/csv-exports/decisions/"
        "DEC-018/notifications/push"
    )

    rejected = client.post(
        email_path,
        json={
            "approver_user_id": "hex-compliance",
        },
    )
    headers = {
        INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(test_secret)
    }
    injected_destination = client.post(
        email_path,
        json={
            "approver_user_id": "hex-compliance",
            "recipient_email": "attacker@example.com",
        },
        headers=headers,
    )
    assert injected_destination.status_code == 422
    assert email_sender.messages == []
    email = client.post(
        email_path,
        json={
            "approver_user_id": "hex-compliance",
        },
        headers=headers,
    )
    push = client.post(
        push_path,
        json={
            "approver_user_id": "hex-compliance",
            "provider": "ntfy",
        },
        headers=headers,
    )

    assert rejected.status_code == 403
    assert email.status_code == 200
    assert push.status_code == 200
    assert email.json()["graph_mutated"] is False
    assert push.json()["graph_mutated"] is False
    assert "token" not in email.text
    assert "token" not in push.text
    assert len(email_sender.messages) == 1
    assert len(push_sender.messages) == 1
    assert "1 of 2 active assignments" in email_sender.messages[0].text
    assert "1 of 2 active assignments" in push_sender.messages[0].text
