"""The four reaction semantics, pinned. "Appears correct" is not the standard here.

A permission system's subtle bugs are invisible: nothing throws, nothing logs,
the demo looks fine, and an approval either happened when it should not have or
silently came apart after three agents were already interrupted.

Each test below is one claim, in the order they matter:

A. An unpermitted user reacting ✅ does nothing, **silently**.
B. Un-reacting cannot un-approve — and not merely because nobody wired it up.
C. The first qualifying reaction binds; a second is a no-op.
D. A non-✅ emoji is ignored.

These exercise `SlackReactionApprovalHandler` against the real
`JsonSlackApprovalThreads` store, so the idempotency claim is tested through the
persistence that actually enforces it rather than a stub that agrees with us.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from writai.intake.approval import (
    ApprovalChannel,
    ApprovalDisposition,
    ApprovalResult,
    PendingApproval,
)
from writai.intake.slack import (
    REACTION_ADDED_SLUG,
    SlackWebhookError,
    VerifiedSlackReaction,
)
from writai.notify.slack import (
    JsonSlackApprovalThreads,
    SlackReactionApprovalHandler,
)

TEAM = "T-WRITAI"
CHANNEL = "C-COMPLIANCE"
MESSAGE_TS = "1700000000.000100"


def _pending() -> PendingApproval:
    return PendingApproval(
        workspace_id="csv-exports",
        decision_id="DEC-018",
        supersedes_id="DEC-004",
        affected_scopes=frozenset({"export.authorization"}),
        permission_id="approve_compliance",
        source_ref=f"slack://{TEAM}/{CHANNEL}/{MESSAGE_TS}",
        title="Admin-only exports",
        text="Exports must be admin-only.",
        effective_at=datetime(2026, 7, 25, 4, 0, tzinfo=UTC),
        requirements={"export.authorization": {"audience": "admin_only"}},
        proposal_fingerprint="sha256:" + ("a" * 64),
        proposal_instance_id="csv-exports:proposal:1",
        evidence_refs=(f"slack://{TEAM}/{CHANNEL}/{MESSAGE_TS}",),
    )


def _reaction(
    *,
    reaction: str = SlackReactionApprovalHandler.APPROVE_REACTION,
    event_id: str = "Ev-1",
    user: str = "U-COMPLIANCE",
) -> VerifiedSlackReaction:
    return VerifiedSlackReaction(
        event_id=event_id,
        connection_user_id="U-CONNECTION",
        reacting_user_id=user,
        team_id=TEAM,
        channel_id=CHANNEL,
        message_ts=MESSAGE_TS,
        reaction=reaction,
        delivered_at=datetime(2026, 7, 25, 5, 0, tzinfo=UTC),
        evidence_ref=f"slack://{TEAM}/{CHANNEL}/{MESSAGE_TS}#reaction-1",
    )


class _Identity:
    """Maps a Slack reactor to a Hexclave user, or refuses to."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = mapping if mapping is not None else {"U-COMPLIANCE": "H-COMPLIANCE"}
        self.calls: list[str] = []

    def resolve_hexclave_user_id(
        self,
        *,
        pending: PendingApproval,
        reaction: VerifiedSlackReaction,
    ) -> str | None:
        self.calls.append(reaction.reacting_user_id)
        return self.mapping.get(reaction.reacting_user_id)


class _Coordinator:
    """The shared permission-checked approver. Records every attempt."""

    def __init__(self, *, permitted: bool = True) -> None:
        self.permitted = permitted
        self.attempts: list[tuple[str, ApprovalChannel]] = []

    def approve(
        self,
        *,
        pending: PendingApproval,
        approver_user_id: str,
        channel: ApprovalChannel,
        evidence_ref: str,
    ) -> ApprovalResult:
        self.attempts.append((approver_user_id, channel))
        if not self.permitted:
            # Exactly what ApprovalCoordinator returns for an unauthorized user:
            # a disposition, not an exception.
            return ApprovalResult(disposition=ApprovalDisposition.IGNORED_NOT_AUTHORIZED)
        return ApprovalResult(disposition=ApprovalDisposition.APPROVED)


def _store(tmp_path: Path) -> JsonSlackApprovalThreads:
    store = JsonSlackApprovalThreads(tmp_path / "threads.json")
    store.register(
        team_id=TEAM,
        channel_id=CHANNEL,
        message_ts=MESSAGE_TS,
        pending=_pending(),
    )
    return store


def _handler(
    store: JsonSlackApprovalThreads,
    coordinator: _Coordinator,
    identity: _Identity | None = None,
) -> SlackReactionApprovalHandler:
    return SlackReactionApprovalHandler(
        pending_resolver=store,
        identity_resolver=identity or _Identity(),
        coordinator=coordinator,
    )


# --------------------------------------------------------------------------------------
# A. An unpermitted user reacting ✅ does nothing, silently.
# --------------------------------------------------------------------------------------


def test_an_unpermitted_user_reacting_does_nothing_and_says_nothing(
    tmp_path: Path,
) -> None:
    """The entire claim. It must be silent: not an error, not an escalation."""

    store = _store(tmp_path)
    coordinator = _Coordinator(permitted=False)
    handler = _handler(store, coordinator)

    result = handler.handle(_reaction(user="U-COMPLIANCE"))

    # The permission check RAN — this is not "ignored because unmapped".
    assert coordinator.attempts == [("H-COMPLIANCE", ApprovalChannel.SLACK_REACTION)]
    # ...and it refused, as a disposition rather than an exception.
    assert result.approval is not None
    assert result.approval.disposition is ApprovalDisposition.IGNORED_NOT_AUTHORIZED
    assert not result.approval.approved

    # Silent: the binding is untouched, so this is not consumed, not escalated,
    # and the real approver can still approve afterwards.
    assert store.resolve(
        team_id=TEAM, channel_id=CHANNEL, message_ts=MESSAGE_TS
    ) is not None


def test_an_unmapped_slack_user_never_reaches_the_permission_check(
    tmp_path: Path,
) -> None:
    """A signed delivery authenticates Composio, not a human. No identity, no attempt."""

    store = _store(tmp_path)
    coordinator = _Coordinator(permitted=True)
    handler = _handler(store, coordinator, _Identity(mapping={}))

    result = handler.handle(_reaction(user="U-STRANGER"))

    assert coordinator.attempts == []
    assert result.approval is None
    assert store.resolve(
        team_id=TEAM, channel_id=CHANNEL, message_ts=MESSAGE_TS
    ) is not None


# --------------------------------------------------------------------------------------
# B. Un-reacting cannot un-approve.
# --------------------------------------------------------------------------------------


class _Triggers:
    """A stand-in for Composio's verified-parse, returning a chosen envelope."""

    def __init__(self, trigger_slug: str) -> None:
        self.trigger_slug = trigger_slug
        # The constructor validates BOTH trigger schemas up front, so this has
        # to satisfy the message fields as well as the reaction fields.
        self.schema = {
            "properties": {
                name: {"type": "string"}
                for name in (
                    "event_id",
                    "user",
                    "team_id",
                    "channel",
                    "channel_id",
                    "text",
                    "ts",
                    "message_channel",
                    "message_ts",
                    "reaction",
                    "event_ts",
                )
            }
        }

    def get_type(self, slug: str) -> object:
        return type("TriggerType", (), {"payload": self.schema, "slug": slug})()

    def parse(self, *, body: object, headers: object, verify_secret: str) -> dict:
        return {
            "version": "V3",
            "payload": {
                "trigger_slug": self.trigger_slug,
                "toolkit_slug": "slack",
                "user_id": "U-CONNECTION",
                "connection_nano_id": "conn-1",
                "payload": {
                    "event_id": "Ev-removed",
                    "user": "U-COMPLIANCE",
                    "team_id": TEAM,
                    "message_channel": CHANNEL,
                    "message_ts": MESSAGE_TS,
                    "reaction": "white_check_mark",
                    "event_ts": "1700000000.000200",
                },
            },
        }


def test_a_reaction_removed_delivery_is_rejected_by_the_parser(
    tmp_path: Path,
) -> None:
    """Structural, not incidental — the worst outcome in the whole system.

    An approval silently withdrawn after three agents have already been
    interrupted is the failure to design against. Proving "nobody wired removal
    up" would be weak, because a future wiring would silently acquire the
    behaviour. So this drives a genuine REACTION_REMOVED envelope through the
    real verifier and asserts it never becomes a VerifiedSlackReaction at all.

    The gate is `_parse`'s own slug check, so every caller inherits it rather
    than each one having to remember.
    """

    from writai.intake.slack import ComposioSlackWebhookVerifier

    added = ComposioSlackWebhookVerifier(
        triggers=cast("Any", _Triggers(REACTION_ADDED_SLUG)),
        webhook_secret="shhh",
    )
    # The added trigger parses, so the harness itself is known-good.
    parsed = added.parse_reaction(body=b"{}", headers={"webhook-id": "d-1"})
    assert parsed.reaction == "white_check_mark"

    # The removal does not. Same shape, same signature, same channel — only the
    # trigger differs, and that is enough.
    removed = ComposioSlackWebhookVerifier(
        triggers=cast("Any", _Triggers("SLACK_MESSAGE_REACTION_REMOVED")),
        webhook_secret="shhh",
    )
    with pytest.raises(SlackWebhookError, match="wrong trigger type"):
        removed.parse_reaction(body=b"{}", headers={"webhook-id": "d-2"})

    # Nor can it sneak in as a message ingest.
    with pytest.raises(SlackWebhookError, match="wrong trigger type"):
        removed.parse_message(body=b"{}", headers={"webhook-id": "d-3"})


def test_no_removal_trigger_is_registered_anywhere(tmp_path: Path) -> None:
    """There is no removed-slug constant to accidentally start honouring."""

    from writai.intake import slack as slack_intake

    slugs = {
        name: value
        for name, value in vars(slack_intake).items()
        if name.endswith("_SLUG") and isinstance(value, str)
    }
    assert slugs == {
        "CHANNEL_MESSAGE_SLUG": "SLACK_CHANNEL_MESSAGE_RECEIVED",
        "REACTION_ADDED_SLUG": "SLACK_MESSAGE_REACTION_ADDED",
    }
    assert not any("REMOV" in value.upper() for value in slugs.values())


def test_removing_a_reaction_after_approval_leaves_the_approval_standing(
    tmp_path: Path,
) -> None:
    """Even if a removal were somehow delivered, it has nothing to undo.

    Approval is applied by the coordinator and recorded by the workspace. The
    thread binding is CONSUMED on approval, so a later delivery of any kind
    finds nothing pending and cannot reverse anything.
    """

    store = _store(tmp_path)
    coordinator = _Coordinator(permitted=True)
    handler = _handler(store, coordinator)

    approved = handler.handle(_reaction(event_id="Ev-approve"))
    assert approved.approval is not None and approved.approval.approved

    # The binding is gone: there is no pending approval left to withdraw.
    assert (
        store.resolve(team_id=TEAM, channel_id=CHANNEL, message_ts=MESSAGE_TS) is None
    )

    # A subsequent delivery on the same card is inert, whatever it carries.
    after = handler.handle(_reaction(event_id="Ev-later"))
    assert after.approval is None
    # And crucially it did NOT reach the approver a second time.
    assert len(coordinator.attempts) == 1


# --------------------------------------------------------------------------------------
# C. First qualifying reaction binds; a second is a no-op.
# --------------------------------------------------------------------------------------


def test_the_first_qualifying_reaction_binds_and_the_second_is_a_no_op(
    tmp_path: Path,
) -> None:
    """`consume()` is what makes this idempotent, so consume through the real store."""

    store = _store(tmp_path)
    coordinator = _Coordinator(permitted=True)
    handler = _handler(store, coordinator)

    first = handler.handle(_reaction(event_id="Ev-1", user="U-COMPLIANCE"))
    second = handler.handle(_reaction(event_id="Ev-2", user="U-COMPLIANCE"))

    assert first.approval is not None and first.approval.approved
    assert second.approval is None
    # One approval attempt, not two: the second never reached the coordinator.
    assert coordinator.attempts == [("H-COMPLIANCE", ApprovalChannel.SLACK_REACTION)]


def test_idempotency_survives_a_fresh_store_over_the_same_file(
    tmp_path: Path,
) -> None:
    """A restart must not re-open an approval that already landed."""

    store = _store(tmp_path)
    coordinator = _Coordinator(permitted=True)
    assert _handler(store, coordinator).handle(_reaction()).approval is not None

    reopened = JsonSlackApprovalThreads(tmp_path / "threads.json")
    later = _Coordinator(permitted=True)
    result = _handler(reopened, later).handle(_reaction(event_id="Ev-after-restart"))

    assert result.approval is None
    assert later.attempts == []


def test_a_second_reactor_cannot_approve_a_card_that_is_already_consumed(
    tmp_path: Path,
) -> None:
    """Two people react at once; exactly one approval happens."""

    store = _store(tmp_path)
    coordinator = _Coordinator(permitted=True)
    identity = _Identity({"U-COMPLIANCE": "H-COMPLIANCE", "U-SECOND": "H-SECOND"})
    handler = _handler(store, coordinator, identity)

    first = handler.handle(_reaction(event_id="Ev-1", user="U-COMPLIANCE"))
    second = handler.handle(_reaction(event_id="Ev-2", user="U-SECOND"))

    assert first.approval is not None and first.approval.approved
    assert second.approval is None
    assert [user for user, _channel in coordinator.attempts] == ["H-COMPLIANCE"]


# --------------------------------------------------------------------------------------
# D. Non-✅ emoji are ignored.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "emoji",
    ["thumbsup", "eyes", "x", "heavy_check_mark", "ballot_box_with_check", "+1"],
)
def test_a_non_approve_emoji_is_ignored_entirely(tmp_path: Path, emoji: str) -> None:
    """Only ✅ approves. Look-alikes are not close enough."""

    store = _store(tmp_path)
    coordinator = _Coordinator(permitted=True)
    identity = _Identity()
    handler = _handler(store, coordinator, identity)

    result = handler.handle(_reaction(reaction=emoji))

    assert result.handled is False
    assert result.approval is None
    # Not even an identity lookup: a wrong emoji is dropped before anything else.
    assert identity.calls == []
    assert coordinator.attempts == []
    # And the card is still approvable by the right emoji afterwards.
    assert (
        store.resolve(team_id=TEAM, channel_id=CHANNEL, message_ts=MESSAGE_TS)
        is not None
    )
    assert _handler(store, coordinator, identity).handle(_reaction()).approval is not None


def test_the_approve_emoji_is_exactly_one_value() -> None:
    """Widening this is a security change, so it should have to be deliberate."""

    assert SlackReactionApprovalHandler.APPROVE_REACTION == "white_check_mark"


def test_a_reaction_on_an_unknown_message_is_ignored(tmp_path: Path) -> None:
    """A ✅ on some other message in the channel must not approve anything."""

    store = _store(tmp_path)
    coordinator = _Coordinator(permitted=True)
    handler = _handler(store, coordinator)

    stray = _reaction().model_copy(update={"message_ts": "1700000000.999999"})
    result = handler.handle(stray)

    assert result.handled is False
    assert coordinator.attempts == []
