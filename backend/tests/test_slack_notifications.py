from __future__ import annotations

from datetime import UTC, datetime

import pytest
from writai.intake.approval import (
    ApprovalDisposition,
    ApprovalResult,
    PendingApproval,
)
from writai.intake.slack import VerifiedSlackReaction
from writai.notify.slack import (
    ComposioSlackThreadSender,
    InMemorySlackApprovalThreads,
    JsonSlackApprovalThreads,
    SlackBlastRadiusNotifier,
    SlackDecisionNotification,
    SlackDeliveryMode,
    SlackDeliveryReceipt,
    SlackNotificationError,
    SlackReactionApprovalHandler,
    SlackRequirementDelta,
    SlackThreadMessage,
    SlackThreadReference,
)
from writai.supervisor_contract import InterruptRequest, InterruptResult
from pydantic import ValidationError


class PreviewOnlyPort:
    def __init__(self, result: InterruptResult) -> None:
        self.result = result
        self.preview_requests: list[InterruptRequest] = []
        self.interrupt_calls = 0

    def preview(self, request: InterruptRequest) -> InterruptResult:
        self.preview_requests.append(request)
        return self.result

    def interrupt(self, request: InterruptRequest) -> InterruptResult:
        del request
        self.interrupt_calls += 1
        raise AssertionError("notification must not interrupt assignments")


class RecordingSender:
    def __init__(self, mode: SlackDeliveryMode) -> None:
        self._mode = mode
        self.messages: list[SlackThreadMessage] = []

    @property
    def delivery_mode(self) -> SlackDeliveryMode:
        return self._mode

    def send_thread_message(
        self,
        message: SlackThreadMessage,
    ) -> SlackDeliveryReceipt:
        self.messages.append(message)
        return SlackDeliveryReceipt(
            thread=message.thread,
            message_id=f"{self._mode.value}-message-1",
            delivery_mode=self._mode,
        )


class RecordingComposioTools:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def execute(self, slug: str, **kwargs: object):
        self.calls.append({"slug": slug, **kwargs})
        return self.result


def _interrupt_request() -> InterruptRequest:
    return InterruptRequest(
        workspace_id="csv-exports",
        decision_id="DEC-018",
        affected_scopes=frozenset({"export.authorization"}),
        provenance_path=(
            "DEC-018",
            "DEC-004",
            "SPEC-009",
            "TICKET-100",
            "TASK-102",
        ),
        interrupt_reason="Exports must require an administrator.",
        redirect_instruction="Add the administrator authorization check.",
    )


def _notification() -> SlackDecisionNotification:
    pending = _pending()
    return SlackDecisionNotification(
        team_id="T-WRITAI",
        thread=SlackThreadReference(
            channel_id="C-APPROVALS",
            parent_message_id="message-1700000000",
        ),
        pending=pending,
        interrupt_request=_interrupt_request(),
        source_author="Priya",
        source_excerpt="Approved: exports must be admin-only, effective immediately.",
        effective_at=pending.effective_at,
        requirement_deltas=(
            SlackRequirementDelta(
                scope="export.authorization",
                previous_summary="all authenticated users",
                proposed_summary="administrators only",
            ),
        ),
        assignment_labels={
            "ASSIGNMENT-TASK-101": "Marcus · Keep CSV calculations",
            "ASSIGNMENT-TASK-102": "Priya · Restrict exports",
            "ASSIGNMENT-TASK-103": "Avery · Update access tests",
        },
        evidence_refs=("slack://C-APPROVALS/message-1700000000",),
    )


def test_live_notification_uses_preview_only_and_posts_exact_blast_radius() -> None:
    port = PreviewOnlyPort(
        InterruptResult(
            interrupted_assignment_ids=(
                "ASSIGNMENT-TASK-103",
                "ASSIGNMENT-TASK-102",
            ),
            preserved_assignment_ids=("ASSIGNMENT-TASK-101",),
        )
    )
    sender = RecordingSender(SlackDeliveryMode.LIVE)

    result = SlackBlastRadiusNotifier(
        interrupt_port=port,
        sender=sender,
    ).notify(_notification())

    assert port.preview_requests == [_interrupt_request()]
    assert port.interrupt_calls == 0
    assert result.preview.interrupted_assignment_ids == (
        "ASSIGNMENT-TASK-102",
        "ASSIGNMENT-TASK-103",
    )
    assert result.preview.preserved_assignment_ids == ("ASSIGNMENT-TASK-101",)
    assert result.preview.interrupted_count == 2
    assert result.preview.total_assignment_count == 3
    assert sender.messages == [result.message]
    assert result.receipt.delivery_mode is SlackDeliveryMode.LIVE

    text = result.message.text
    assert "Delivery mode: LIVE — external Slack thread" in text
    assert "2 of 3 active assignments would be interrupted" in text
    assert (
        "Would interrupt (2): Priya · Restrict exports "
        "(ASSIGNMENT-TASK-102), Avery · Update access tests "
        "(ASSIGNMENT-TASK-103)"
    ) in text
    assert (
        "Would preserve (1): Marcus · Keep CSV calculations "
        "(ASSIGNMENT-TASK-101)"
    ) in text
    assert "export.authorization: all authenticated users => administrators only" in text
    assert "No assignment was interrupted and no decision was approved" in text
    assert "verify permission at reaction time" in text
    assert result.message.mentions_enabled is False


def test_live_composio_sender_pins_connection_tool_version_and_thread() -> None:
    tools = RecordingComposioTools(
        {
            "successful": True,
            "error": None,
            "data": {
                "ok": True,
                "channel": "C-APPROVALS",
                "ts": "1784952301.000002",
            },
        }
    )
    sender = ComposioSlackThreadSender(
        tools=tools,
        composio_user_id="writai-user-emanuel",
        connected_account_id="ca_slack_emanuel",
    )
    message = SlackThreadMessage(
        thread=SlackThreadReference(
            channel_id="C-APPROVALS",
            parent_message_id="1784952300.000001",
        ),
        decision_id="DEC-018",
        delivery_mode=SlackDeliveryMode.LIVE,
        text="Bound preview.",
    )

    receipt = sender.send_thread_message(message)

    assert receipt.message_id == "1784952301.000002"
    assert tools.calls == [
        {
            "slug": "SLACK_SEND_MESSAGE",
            "arguments": {
                "channel": "C-APPROVALS",
                "markdown_text": "Bound preview.",
                "thread_ts": "1784952300.000001",
                "reply_broadcast": False,
            },
            "user_id": "writai-user-emanuel",
            "connected_account_id": "ca_slack_emanuel",
            "version": "20260702_00",
        }
    ]
    assert receipt.thread == message.thread


@pytest.mark.parametrize(
    "result",
    [
        {"successful": False, "error": "failed", "data": {}},
        {"successful": True, "error": None, "data": {"ok": False}},
        {
            "successful": True,
            "error": None,
            "data": {"ok": True, "channel": "C-WRONG", "ts": "1"},
        },
        {
            "successful": True,
            "error": None,
            "data": {"ok": True, "channel": "C-APPROVALS"},
        },
    ],
)
def test_live_composio_sender_fails_closed_on_malformed_receipt(
    result: dict[str, object],
) -> None:
    sender = ComposioSlackThreadSender(
        tools=RecordingComposioTools(result),
        composio_user_id="writai-user-emanuel",
        connected_account_id="ca_slack_emanuel",
    )

    with pytest.raises(SlackNotificationError):
        sender.send_thread_message(
            SlackThreadMessage(
                thread=SlackThreadReference(
                    channel_id="C-APPROVALS",
                    parent_message_id="1784952300.000001",
                ),
                decision_id="DEC-018",
                delivery_mode=SlackDeliveryMode.LIVE,
                text="Bound preview.",
            )
        )


def test_simulated_delivery_is_unambiguously_labeled() -> None:
    port = PreviewOnlyPort(
        InterruptResult(
            interrupted_assignment_ids=(),
            preserved_assignment_ids=("ASSIGNMENT-TASK-101",),
        )
    )
    sender = RecordingSender(SlackDeliveryMode.SIMULATED)

    result = SlackBlastRadiusNotifier(
        interrupt_port=port,
        sender=sender,
    ).notify(_notification())

    assert result.message.delivery_mode is SlackDeliveryMode.SIMULATED
    assert result.receipt.delivery_mode is SlackDeliveryMode.SIMULATED
    assert "SIMULATED — no external Slack post" in result.message.text
    assert "Blast radius: 0 of 1" in result.message.text


def test_user_controlled_slack_markup_is_neutralized() -> None:
    notification = _notification().model_copy(
        update={
            "source_author": "<!channel>",
            "source_excerpt": "<@U-123> requested A & B",
            "requirement_deltas": (
                SlackRequirementDelta(
                    scope="export.authorization",
                    proposed_summary="<@U-456> must approve",
                ),
            ),
        }
    )
    sender = RecordingSender(SlackDeliveryMode.SIMULATED)

    result = SlackBlastRadiusNotifier(
        interrupt_port=PreviewOnlyPort(InterruptResult((), ())),
        sender=sender,
    ).notify(notification)

    assert "<!channel>" not in result.message.text
    assert "<@U-123>" not in result.message.text
    assert "<@U-456>" not in result.message.text
    assert "&lt;!channel&gt;" in result.message.text
    assert "&lt;@U-123&gt; requested A &amp; B" in result.message.text
    assert result.message.mentions_enabled is False


def test_notification_rejects_scope_drift_before_preview() -> None:
    with pytest.raises(
        ValidationError,
        match="requirement delta scopes must exactly match affected scopes",
    ):
        SlackDecisionNotification(
            team_id="T-WRITAI",
            thread=SlackThreadReference(
                channel_id="C-APPROVALS",
                parent_message_id="message-1700000000",
            ),
            pending=_pending(),
            interrupt_request=_interrupt_request(),
            source_author="Priya",
            source_excerpt="Approved change.",
            effective_at=_pending().effective_at,
            requirement_deltas=(
                SlackRequirementDelta(
                    scope="export.generation",
                    proposed_summary="CSV only",
                ),
            ),
        )


def test_notification_boundary_rejects_raw_provider_payload_fields() -> None:
    payload = _notification().model_dump()
    payload["raw_composio_payload"] = {"data": {"unknown": "shape"}}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SlackDecisionNotification.model_validate(payload)


def test_invalid_preview_partition_is_not_sent() -> None:
    port = PreviewOnlyPort(
        InterruptResult(
            interrupted_assignment_ids=("ASSIGNMENT-TASK-102",),
            preserved_assignment_ids=("ASSIGNMENT-TASK-102",),
        )
    )
    sender = RecordingSender(SlackDeliveryMode.LIVE)

    with pytest.raises(
        ValidationError,
        match="assignments in both partitions",
    ):
        SlackBlastRadiusNotifier(
            interrupt_port=port,
            sender=sender,
        ).notify(_notification())

    assert port.interrupt_calls == 0
    assert sender.messages == []


def test_sender_cannot_misreport_live_delivery_as_simulated() -> None:
    class MismatchedSender(RecordingSender):
        def send_thread_message(
            self,
            message: SlackThreadMessage,
        ) -> SlackDeliveryReceipt:
            self.messages.append(message)
            return SlackDeliveryReceipt(
                thread=message.thread,
                message_id="fixture-message",
                delivery_mode=SlackDeliveryMode.SIMULATED,
            )

    sender = MismatchedSender(SlackDeliveryMode.LIVE)
    port = PreviewOnlyPort(InterruptResult((), ()))

    with pytest.raises(
        SlackNotificationError,
        match="different delivery mode",
    ):
        SlackBlastRadiusNotifier(
            interrupt_port=port,
            sender=sender,
        ).notify(_notification())

    assert port.interrupt_calls == 0


def _pending() -> PendingApproval:
    return PendingApproval(
        workspace_id="csv-exports",
        decision_id="DEC-018",
        supersedes_id="DEC-004",
        affected_scopes=frozenset({"export.authorization"}),
        permission_id="approve_compliance",
        source_ref="slack://T/C/1",
        title="Admin-only exports",
        text="Exports must be admin-only.",
        effective_at=datetime(
            2026, 7, 25, tzinfo=UTC
        ),
        requirements={
            "export.authorization": {"audience": "admin_only"}
        },
        proposal_fingerprint="sha256:" + ("a" * 64),
        proposal_instance_id="csv-exports:proposal:1",
        evidence_refs=("slack://T/C/1",),
    )


def _reaction(*, user: str = "U-REACTING") -> VerifiedSlackReaction:
    return VerifiedSlackReaction(
        event_id="evt-reaction-1",
        connection_user_id="writai-connection-owner",
        reacting_user_id=user,
        team_id="T-WRITAI",
        channel_id="C-APPROVALS",
        message_ts="1784952300.000001",
        reaction="white_check_mark",
        delivered_at=datetime(2026, 7, 25, tzinfo=UTC),
        evidence_ref="slack://T-WRITAI/C-APPROVALS/1#reaction-2",
    )


def test_reaction_uses_reacting_user_and_shared_approval_path() -> None:
    registry = InMemorySlackApprovalThreads()
    registry.register(
        team_id="T-WRITAI",
        channel_id="C-APPROVALS",
        message_ts="1784952300.000001",
        pending=_pending(),
    )
    calls: list[dict[str, object]] = []

    class Coordinator:
        def approve(self, **kwargs):
            calls.append(kwargs)
            return ApprovalResult(
                disposition=ApprovalDisposition.APPROVED
            )

    class IdentityResolver:
        def resolve_hexclave_user_id(self, **kwargs):
            assert kwargs["reaction"].reacting_user_id == "U-REACTING"
            return "hex-user-compliance"

    result = SlackReactionApprovalHandler(
        pending_resolver=registry,
        identity_resolver=IdentityResolver(),
        coordinator=Coordinator(),
    ).handle(_reaction())

    assert result.handled is True
    assert calls[0]["approver_user_id"] == "hex-user-compliance"
    assert calls[0]["pending"] == _pending()
    assert registry.resolve(
        team_id="T-WRITAI",
        channel_id="C-APPROVALS",
        message_ts="1784952300.000001",
    ) is None


def test_notification_registers_the_exact_posted_pending_instance() -> None:
    registry = InMemorySlackApprovalThreads()
    result = SlackBlastRadiusNotifier(
        interrupt_port=PreviewOnlyPort(InterruptResult((), ())),
        sender=RecordingSender(SlackDeliveryMode.LIVE),
        approval_threads=registry,
    ).notify(_notification())

    assert registry.resolve(
        team_id="T-WRITAI",
        channel_id="C-APPROVALS",
        message_ts=result.receipt.message_id,
    ) == _pending()


def test_json_thread_binding_survives_restart_and_consumption_is_idempotent(
    tmp_path,
) -> None:
    path = tmp_path / "slack-approval-threads.json"
    first = JsonSlackApprovalThreads(path)
    first.register(
        team_id="T-WRITAI",
        channel_id="C-APPROVALS",
        message_ts="1784952300.000001",
        pending=_pending(),
    )

    restarted = JsonSlackApprovalThreads(path)
    assert restarted.resolve(
        team_id="T-WRITAI",
        channel_id="C-APPROVALS",
        message_ts="1784952300.000001",
    ) == _pending()
    assert restarted.consume(
        team_id="T-WRITAI",
        channel_id="C-APPROVALS",
        message_ts="1784952300.000001",
        event_id="msg-reaction-1",
        pending=_pending(),
    )
    assert JsonSlackApprovalThreads(path).resolve(
        team_id="T-WRITAI",
        channel_id="C-APPROVALS",
        message_ts="1784952300.000001",
    ) is None
    assert JsonSlackApprovalThreads(path).consume(
        team_id="T-WRITAI",
        channel_id="C-APPROVALS",
        message_ts="1784952300.000001",
        event_id="msg-reaction-1",
        pending=_pending(),
    )
    assert not JsonSlackApprovalThreads(path).consume(
        team_id="T-WRITAI",
        channel_id="C-APPROVALS",
        message_ts="1784952300.000001",
        event_id="msg-reaction-2",
        pending=_pending(),
    )


def test_unrelated_or_removed_reactions_cannot_unapprove() -> None:
    registry = InMemorySlackApprovalThreads()

    class Coordinator:
        def approve(self, **kwargs):
            raise AssertionError("unbound reactions must not reach approval")

    class IdentityResolver:
        def resolve_hexclave_user_id(self, **_kwargs):
            raise AssertionError("unbound reactions must not resolve identity")

    reaction = _reaction().model_copy(update={"reaction": "x"})
    result = SlackReactionApprovalHandler(
        pending_resolver=registry,
        identity_resolver=IdentityResolver(),
        coordinator=Coordinator(),
    ).handle(reaction)

    assert result.handled is False
    assert result.approval is None


def test_unmapped_slack_user_is_ignored_before_permission_check() -> None:
    registry = InMemorySlackApprovalThreads()
    registry.register(
        team_id="T-WRITAI",
        channel_id="C-APPROVALS",
        message_ts="1784952300.000001",
        pending=_pending(),
    )

    class IdentityResolver:
        def resolve_hexclave_user_id(self, **_kwargs):
            return None

    class Coordinator:
        def approve(self, **_kwargs):
            raise AssertionError("unmapped Slack user must not reach Hexclave")

    result = SlackReactionApprovalHandler(
        pending_resolver=registry,
        identity_resolver=IdentityResolver(),
        coordinator=Coordinator(),
    ).handle(_reaction(user="U-UNMAPPED"))

    assert result.handled is True
    assert result.approval is None
