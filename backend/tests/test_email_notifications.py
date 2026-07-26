from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from urllib.parse import parse_qs, urlsplit

import httpx
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
    ApprovalImpact,
    ApprovalLinkClaims,
    ApprovalLinkRedeemer,
    ApprovalLinkSigner,
    ApprovalRecipientBinding,
    ApprovalRecipientDirectory,
    ChannelApprovalAssertionSigner,
    EmailApprovalNotifier,
    EmailDeliveryReceipt,
    ExpiredApprovalLink,
    InMemoryApprovalLinkUseStore,
    InvalidApprovalLink,
    JsonApprovalLinkUseStore,
    NotificationDeliveryMode,
    ReplayedApprovalLink,
    ResendEmailSender,
    SqliteApprovalLinkUseStore,
    SqliteChannelApprovalAssertionUseStore,
    approval_confirmation_html,
)

NOW = datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
SECRET = "email-link-test-secret-that-is-over-32-bytes"


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
    """Test seam matching the agent service's reload-and-coordinate behavior."""

    def __init__(
        self,
        *,
        current: PendingApproval,
        coordinator: ApprovalCoordinator,
    ) -> None:
        self.current = current
        self.coordinator = coordinator
        self.calls: list[tuple[ApprovalLinkClaims, str]] = []

    def approve_bound(
        self,
        *,
        claims: ApprovalLinkClaims,
        evidence_ref: str,
    ) -> ApprovalResult:
        self.calls.append((claims, evidence_ref))
        if (
            claims.workspace_id != self.current.workspace_id
            or claims.decision_id != self.current.decision_id
            or claims.proposal_fingerprint
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
            message_id="EMAIL-FIXTURE-1",
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


def _signer(*, now: datetime = NOW) -> ApprovalLinkSigner:
    return ApprovalLinkSigner(
        secret=SECRET,
        ttl_seconds=900,
        clock=lambda: now,
        token_id_factory=lambda: "email-token-id-00000001",
    )


def _coordinator_port(
    *,
    allowed: bool,
    current: PendingApproval | None = None,
) -> tuple[CoordinatorBoundPort, Checker, WorkspacePort]:
    checker = Checker(allowed)
    workspace = WorkspacePort()
    coordinator = ApprovalCoordinator(
        permission_checker=checker,
        workspace_port=workspace,
        clock=lambda: NOW,
    )
    return (
        CoordinatorBoundPort(
            current=current or _pending(),
            coordinator=coordinator,
        ),
        checker,
        workspace,
    )


def _email_token(message: ApprovalEmail) -> str:
    url = next(
        line for line in message.text.splitlines() if line.startswith("http")
    )
    split = urlsplit(url)
    assert split.query == ""
    values = parse_qs(split.fragment)
    return values["token"][0]


def test_email_get_is_read_only_and_explicit_post_uses_shared_coordinator() -> None:
    signer = _signer()
    sender = RecordingEmailSender()
    bound_port, checker, workspace = _coordinator_port(allowed=True)
    notifier = EmailApprovalNotifier(
        signer=signer,
        public_base_url="https://approval.example",
        sender_email="approvals@example.com",
        sender=sender,
    )

    result = notifier.notify(
        pending=_pending(),
        approver_user_id="hex-compliance",
        recipient_email="lead@example.com",
        impact=ApprovalImpact(
            interrupted_assignment_ids=("ASSIGN-2", "ASSIGN-3"),
            preserved_assignment_ids=("ASSIGN-1",),
        ),
    )

    assert result.receipt.delivery_mode is NotificationDeliveryMode.SIMULATED
    assert checker.calls == []
    assert workspace.calls == []
    assert len(sender.messages) == 1
    message = sender.messages[0]
    assert "2 of 3 active assignments would be interrupted" in message.text
    assert "Opening it does not approve anything" in message.text
    token = _email_token(message)

    redeemer = ApprovalLinkRedeemer(
        signer=signer,
        use_store=InMemoryApprovalLinkUseStore(),
        approval_port=bound_port,
        recipient_verifier=AcceptingRecipientVerifier(),
        clock=lambda: NOW,
    )
    inspected = redeemer.inspect(
        token=token,
        expected_channel=ApprovalChannel.EMAIL,
    )
    page = approval_confirmation_html(channel=inspected.channel)

    assert 'method="post"' in page
    assert "Approve this exact proposal" in page
    assert token not in page
    assert "window.location.hash" in page
    assert checker.calls == []
    assert workspace.calls == []

    redemption = redeemer.redeem(
        token=token,
        expected_channel=ApprovalChannel.EMAIL,
    )

    assert redemption.result.disposition is ApprovalDisposition.APPROVED
    assert checker.calls == [
        ("hex-compliance", "approve_compliance")
    ]
    assert len(workspace.calls) == 1
    evidence = workspace.calls[0][1]
    assert evidence.channel is ApprovalChannel.EMAIL
    assert evidence.approver_user_id == "hex-compliance"
    assert evidence.confirmed_proposal_fingerprint == (
        _pending().proposal_fingerprint
    )
    assert evidence.confirmed_proposal_instance_id == (
        _pending().proposal_instance_id
    )
    assert evidence.evidence_ref.startswith("email://approval/")

    with pytest.raises(ReplayedApprovalLink):
        redeemer.redeem(
            token=token,
            expected_channel=ApprovalChannel.EMAIL,
        )
    assert len(checker.calls) == 1
    assert len(workspace.calls) == 1


def test_unauthorized_email_link_cannot_apply_and_is_single_use() -> None:
    signer = _signer()
    token = signer.issue(
        pending=_pending(),
        approver_user_id="hex-denied",
        channel=ApprovalChannel.EMAIL,
        delivery_provider=ApprovalDeliveryProvider.EMAIL,
        recipient_ref="denied@example.com",
    )
    bound_port, checker, workspace = _coordinator_port(allowed=False)
    redeemer = ApprovalLinkRedeemer(
        signer=signer,
        use_store=InMemoryApprovalLinkUseStore(),
        approval_port=bound_port,
        recipient_verifier=AcceptingRecipientVerifier(),
        clock=lambda: NOW,
    )

    first = redeemer.redeem(
        token=token,
        expected_channel=ApprovalChannel.EMAIL,
    )

    assert first.result.disposition is (
        ApprovalDisposition.IGNORED_NOT_AUTHORIZED
    )
    assert checker.calls == [("hex-denied", "approve_compliance")]
    assert workspace.calls == []
    with pytest.raises(ReplayedApprovalLink):
        redeemer.redeem(
            token=token,
            expected_channel=ApprovalChannel.EMAIL,
        )


def test_stale_email_link_never_reaches_permission_check_or_apply() -> None:
    signer = _signer()
    token = signer.issue(
        pending=_pending(),
        approver_user_id="hex-compliance",
        channel=ApprovalChannel.EMAIL,
        delivery_provider=ApprovalDeliveryProvider.EMAIL,
        recipient_ref="lead@example.com",
    )
    current = _pending(
        fingerprint_character="b",
        instance_id="proposal-instance-2",
    )
    bound_port, checker, workspace = _coordinator_port(
        allowed=True,
        current=current,
    )
    redeemer = ApprovalLinkRedeemer(
        signer=signer,
        use_store=InMemoryApprovalLinkUseStore(),
        approval_port=bound_port,
        recipient_verifier=AcceptingRecipientVerifier(),
        clock=lambda: NOW,
    )

    result = redeemer.redeem(
        token=token,
        expected_channel=ApprovalChannel.EMAIL,
    )

    assert result.result.disposition is (
        ApprovalDisposition.STALE_CONFIRMATION
    )
    assert checker.calls == []
    assert workspace.calls == []


def test_tampered_cross_channel_and_expired_links_fail_before_approval() -> None:
    signer = _signer()
    token = signer.issue(
        pending=_pending(),
        approver_user_id="hex-compliance",
        channel=ApprovalChannel.EMAIL,
        delivery_provider=ApprovalDeliveryProvider.EMAIL,
        recipient_ref="lead@example.com",
    )
    bound_port, checker, workspace = _coordinator_port(allowed=True)
    redeemer = ApprovalLinkRedeemer(
        signer=signer,
        use_store=InMemoryApprovalLinkUseStore(),
        approval_port=bound_port,
        recipient_verifier=AcceptingRecipientVerifier(),
        clock=lambda: NOW,
    )

    with pytest.raises(InvalidApprovalLink):
        redeemer.redeem(
            token=token[:-1] + ("A" if token[-1] != "A" else "B"),
            expected_channel=ApprovalChannel.EMAIL,
        )
    with pytest.raises(InvalidApprovalLink):
        redeemer.redeem(
            token=token,
            expected_channel=ApprovalChannel.PUSH,
        )

    expired_signer = _signer(
        now=datetime(2026, 7, 25, 8, 16, tzinfo=UTC)
    )
    with pytest.raises(ExpiredApprovalLink):
        expired_signer.decode(
            token,
            expected_channel=ApprovalChannel.EMAIL,
        )
    assert checker.calls == []
    assert workspace.calls == []


def test_non_ascii_link_is_rejected_as_malformed() -> None:
    with pytest.raises(InvalidApprovalLink):
        _signer().decode(
            "é.invalid",
            expected_channel=ApprovalChannel.EMAIL,
        )


def test_changed_recipient_binding_fails_before_consume_or_forward() -> None:
    signer = _signer()
    token = signer.issue(
        pending=_pending(),
        approver_user_id="hex-compliance",
        channel=ApprovalChannel.EMAIL,
        delivery_provider=ApprovalDeliveryProvider.EMAIL,
        recipient_ref="lead@example.com",
    )
    bound_port, checker, workspace = _coordinator_port(allowed=True)
    changed_directory = ApprovalRecipientDirectory(
        (
            ApprovalRecipientBinding(
                workspace_id="csv-exports",
                approver_user_id="hex-compliance",
                email="replacement@example.com",
            ),
        ),
        ntfy_server="https://ntfy.sh",
    )
    redeemer = ApprovalLinkRedeemer(
        signer=signer,
        use_store=InMemoryApprovalLinkUseStore(),
        approval_port=bound_port,
        recipient_verifier=changed_directory,
        clock=lambda: NOW,
    )

    with pytest.raises(InvalidApprovalLink):
        redeemer.redeem(
            token=token,
            expected_channel=ApprovalChannel.EMAIL,
        )

    assert bound_port.calls == []
    assert checker.calls == []
    assert workspace.calls == []


def test_json_use_store_blocks_replay_across_restart(tmp_path: Path) -> None:
    signer = _signer()
    token = signer.issue(
        pending=_pending(),
        approver_user_id="hex-compliance",
        channel=ApprovalChannel.EMAIL,
        delivery_provider=ApprovalDeliveryProvider.EMAIL,
        recipient_ref="lead@example.com",
    )
    bound_port, checker, workspace = _coordinator_port(allowed=True)
    path = tmp_path / "approval-link-uses.json"

    first = ApprovalLinkRedeemer(
        signer=signer,
        use_store=JsonApprovalLinkUseStore(path),
        approval_port=bound_port,
        recipient_verifier=AcceptingRecipientVerifier(),
        clock=lambda: NOW,
    )
    first.redeem(
        token=token,
        expected_channel=ApprovalChannel.EMAIL,
    )

    restarted = ApprovalLinkRedeemer(
        signer=signer,
        use_store=JsonApprovalLinkUseStore(path),
        approval_port=bound_port,
        recipient_verifier=AcceptingRecipientVerifier(),
        clock=lambda: NOW,
    )
    with pytest.raises(ReplayedApprovalLink):
        restarted.redeem(
            token=token,
            expected_channel=ApprovalChannel.EMAIL,
        )
    assert len(checker.calls) == 1
    assert len(workspace.calls) == 1
    assert path.stat().st_mode & 0o777 == 0o600


def test_sqlite_link_ledger_is_atomic_across_two_instances(
    tmp_path: Path,
) -> None:
    signer = _signer()
    claims = signer.decode(
        signer.issue(
            pending=_pending(),
            approver_user_id="hex-compliance",
            channel=ApprovalChannel.EMAIL,
            delivery_provider=ApprovalDeliveryProvider.EMAIL,
            recipient_ref="lead@example.com",
        ),
        expected_channel=ApprovalChannel.EMAIL,
    )
    path = tmp_path / "link-replays.sqlite3"
    stores = (
        SqliteApprovalLinkUseStore(path),
        SqliteApprovalLinkUseStore(path),
    )
    barrier = Barrier(2)

    def consume(store: SqliteApprovalLinkUseStore) -> bool:
        barrier.wait()
        return store.consume(claims, consumed_at=NOW)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(consume, stores))

    assert sorted(results) == [False, True]
    assert path.stat().st_mode & 0o777 == 0o600


def test_sqlite_assertion_ledger_is_atomic_across_two_instances(
    tmp_path: Path,
) -> None:
    link_signer = _signer()
    link_claims = link_signer.decode(
        link_signer.issue(
            pending=_pending(),
            approver_user_id="hex-compliance",
            channel=ApprovalChannel.EMAIL,
            delivery_provider=ApprovalDeliveryProvider.EMAIL,
            recipient_ref="lead@example.com",
        ),
        expected_channel=ApprovalChannel.EMAIL,
    )
    assertion_signer = ChannelApprovalAssertionSigner(
        secret=SECRET,
        clock=lambda: NOW,
        assertion_id_factory=lambda: "assertion-token-id-00001",
    )
    assertion = assertion_signer.decode(
        assertion_signer.issue_notification(
            link_claims
        ).get_secret_value(),
        expected_workspace_id=link_claims.workspace_id,
        expected_decision_id=link_claims.decision_id,
    )
    path = tmp_path / "assertion-replays.sqlite3"
    stores = (
        SqliteChannelApprovalAssertionUseStore(path),
        SqliteChannelApprovalAssertionUseStore(path),
    )
    barrier = Barrier(2)

    def consume(store: SqliteChannelApprovalAssertionUseStore) -> bool:
        barrier.wait()
        return store.consume(assertion, consumed_at=NOW)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(consume, stores))

    assert sorted(results) == [False, True]
    assert path.stat().st_mode & 0o777 == 0o600


def test_resend_sender_uses_one_pinned_https_submission() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            "https://api.resend.com/emails"
        )
        assert request.headers["authorization"] == "Bearer resend-secret"
        payload = __import__("json").loads(request.content)
        assert payload["to"] == ["lead@example.com"]
        assert payload["from"] == "approvals@example.com"
        return httpx.Response(200, json={"id": "email-resend-1"})

    sender = ResendEmailSender(
        api_key="resend-secret",
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler)
        ),
    )

    receipt = sender.send(
        ApprovalEmail(
            recipient_email="lead@example.com",
            sender_email="approvals@example.com",
            subject="Approval",
            text="Review this exact proposal.",
            html="<p>Review this exact proposal.</p>",
            delivery_mode=NotificationDeliveryMode.LIVE,
        )
    )

    assert receipt.provider == "resend"
    assert receipt.message_id == "email-resend-1"
    assert receipt.delivery_mode is NotificationDeliveryMode.LIVE
