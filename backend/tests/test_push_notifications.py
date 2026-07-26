from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from dragback.intake.approval import (
    ApprovalChannel,
    ApprovalCoordinator,
    ApprovalDisposition,
    ApprovalEvidence,
    ApprovalResult,
    PendingApproval,
)
from dragback.notify.email import (
    ApprovalImpact,
    ApprovalLinkClaims,
    ApprovalLinkRedeemer,
    ApprovalLinkSigner,
    InMemoryApprovalLinkUseStore,
    NotificationDeliveryMode,
    ReplayedApprovalLink,
)
from dragback.notify.push import (
    NtfyPushSender,
    PushApprovalMessage,
    PushApprovalNotifier,
    PushDeliveryReceipt,
    PushoverPushSender,
)
from pydantic import SecretStr

NOW = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
SECRET = "push-link-test-secret-that-is-over-32-bytes"
PRIVATE_TOPIC = "dragback-7F3k9Q2mR8xP4vN6cL1s"


class Checker:
    def __init__(self, allowed: bool) -> None:
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
        pending: PendingApproval,
        coordinator: ApprovalCoordinator,
    ) -> None:
        self.pending = pending
        self.coordinator = coordinator

    def approve_bound(
        self,
        *,
        claims: ApprovalLinkClaims,
        evidence_ref: str,
    ) -> ApprovalResult:
        if (
            claims.proposal_fingerprint
            != self.pending.proposal_fingerprint
            or claims.proposal_instance_id
            != self.pending.proposal_instance_id
        ):
            return ApprovalResult(
                disposition=ApprovalDisposition.STALE_CONFIRMATION
            )
        return self.coordinator.approve(
            pending=self.pending,
            approver_user_id=claims.approver_user_id,
            channel=claims.channel,
            evidence_ref=evidence_ref,
        )


class AcceptingRecipientVerifier:
    def verify(self, claims: ApprovalLinkClaims) -> None:
        del claims


class RecordingPushSender:
    provider = "ntfy"
    delivery_mode = NotificationDeliveryMode.SIMULATED
    recipient_binding_ref = "fixture://ntfy/private-topic"

    def __init__(self) -> None:
        self.messages: list[PushApprovalMessage] = []

    def send(self, message: PushApprovalMessage) -> PushDeliveryReceipt:
        self.messages.append(message)
        return PushDeliveryReceipt(
            provider="ntfy",
            message_id="PUSH-FIXTURE-1",
            delivery_mode=self.delivery_mode,
        )


def _pending() -> PendingApproval:
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
        proposal_fingerprint="sha256:" + ("a" * 64),
        proposal_instance_id="proposal-instance-1",
        evidence_refs=("slack://T1/C1/1",),
    )


def _signer() -> ApprovalLinkSigner:
    return ApprovalLinkSigner(
        secret=SECRET,
        ttl_seconds=900,
        clock=lambda: NOW,
        token_id_factory=lambda: "push-token-id-000000001",
    )


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        (True, ApprovalDisposition.APPROVED),
        (False, ApprovalDisposition.IGNORED_NOT_AUTHORIZED),
    ],
)
def test_push_action_uses_shared_permission_checked_coordinator(
    allowed: bool,
    expected: ApprovalDisposition,
) -> None:
    signer = _signer()
    sender = RecordingPushSender()
    checker = Checker(allowed)
    workspace = WorkspacePort()
    coordinator = ApprovalCoordinator(
        permission_checker=checker,
        workspace_port=workspace,
        clock=lambda: NOW,
    )
    notifier = PushApprovalNotifier(
        signer=signer,
        public_base_url="https://approval.example",
        sender=sender,
    )

    notifier.notify(
        pending=_pending(),
        approver_user_id="hex-compliance",
        impact=ApprovalImpact(
            interrupted_assignment_ids=("ASSIGN-2",),
            preserved_assignment_ids=("ASSIGN-1",),
        ),
    )

    assert checker.calls == []
    assert workspace.calls == []
    assert len(sender.messages) == 1
    message = sender.messages[0]
    assert "1 of 2 active assignments" in message.text
    confirmation = urlsplit(
        message.confirmation_url.get_secret_value()
    )
    assert confirmation.query == ""
    token = parse_qs(confirmation.fragment)["token"][0]
    redemption_parts = urlsplit(message.redemption_url.get_secret_value())
    assert redemption_parts.query == ""
    assert redemption_parts.fragment == ""
    redeemer = ApprovalLinkRedeemer(
        signer=signer,
        use_store=InMemoryApprovalLinkUseStore(),
        approval_port=CoordinatorBoundPort(
            pending=_pending(),
            coordinator=coordinator,
        ),
        recipient_verifier=AcceptingRecipientVerifier(),
        clock=lambda: NOW,
    )

    redemption = redeemer.redeem(
        token=token,
        expected_channel=ApprovalChannel.PUSH,
    )

    assert redemption.result.disposition is expected
    assert checker.calls == [
        ("hex-compliance", "approve_compliance")
    ]
    assert len(workspace.calls) == (1 if allowed else 0)
    if allowed:
        evidence = workspace.calls[0][1]
        assert evidence.channel is ApprovalChannel.PUSH
        assert evidence.evidence_ref.startswith("push://approval/")
    with pytest.raises(ReplayedApprovalLink):
        redeemer.redeem(
            token=token,
            expected_channel=ApprovalChannel.PUSH,
        )


def test_ntfy_sender_posts_explicit_approval_action_and_review_link() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": "ntfy-message-1", "topic": PRIVATE_TOPIC},
        )

    sender = NtfyPushSender(
        server_url="https://ntfy.example",
        topic=PRIVATE_TOPIC,
        access_token="tk_ntfy_private_access",
        private_topic_confirmed=True,
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler)
        ),
    )
    message = PushApprovalMessage(
        title="Dragback approval",
        text="Review one exact proposal.",
        decision_id="DEC-018",
        confirmation_url=SecretStr(
            "https://approval.example/confirm#token=secret"
        ),
        redemption_url=SecretStr(
            "https://approval.example/redeem"
        ),
        redemption_body=SecretStr("token=secret"),
        delivery_mode=NotificationDeliveryMode.LIVE,
    )

    receipt = sender.send(message)

    assert receipt.message_id == "ntfy-message-1"
    assert len(requests) == 1
    request = requests[0]
    assert request.url == httpx.URL(
        f"https://ntfy.example/{PRIVATE_TOPIC}"
    )
    assert request.headers["authorization"] == (
        "Bearer tk_ntfy_private_access"
    )
    actions = request.headers["actions"]
    assert (
        "http, Approve exact proposal, "
        "https://approval.example/redeem, method=POST, "
        "body=token%3Dsecret, "
        "headers.Content-Type=application/x-www-form-urlencoded"
    ) in actions
    assert (
        "view, Review details, "
        "https://approval.example/confirm#token=secret"
    ) in actions
    action_urls = [
        action.split(",")[2].strip()
        for action in actions.split(";")
    ]
    assert all(
        "token=" not in url.split("#", 1)[0]
        for url in action_urls
    )
    assert "token=" not in str(request.url)
    assert request.content == b"Review one exact proposal."


def test_pushover_sender_opens_non_mutating_confirmation_surface() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"status": 1, "request": "pushover-request-1"},
        )

    sender = PushoverPushSender(
        app_token="app-secret",
        user_key="user-secret",
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler)
        ),
    )
    message = PushApprovalMessage(
        title="Dragback approval",
        text="Review one exact proposal.",
        decision_id="DEC-018",
        confirmation_url=SecretStr(
            "https://approval.example/confirm#token=secret"
        ),
        redemption_url=SecretStr(
            "https://approval.example/redeem"
        ),
        redemption_body=SecretStr("token=secret"),
        delivery_mode=NotificationDeliveryMode.LIVE,
    )

    receipt = sender.send(message)

    assert receipt.message_id == "pushover-request-1"
    assert len(requests) == 1
    form = dict(
        item.split("=", 1)
        for item in requests[0].content.decode().split("&")
    )
    assert form["url"] == (
        "https%3A%2F%2Fapproval.example%2Fconfirm%23token%3Dsecret"
    )
    assert "redeem" not in requests[0].content.decode()
    assert requests[0].url == httpx.URL(
        "https://api.pushover.net/1/messages.json"
    )
