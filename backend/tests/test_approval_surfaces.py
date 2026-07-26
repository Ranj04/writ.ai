"""Per surface: CLI, web and Slack all reach the SAME permission check.

The construction sites are already pinned elsewhere (`test_approval_coordinator`
enumerates every place an `ApprovalEvidence` can be minted). That proves nobody
can build an approval somewhere new. It does **not** prove that the surfaces we
ship actually route through the check — a surface could reach the coordinator
and still be wrong if it were allowed to *assert it had already checked*.

So each surface is pinned twice:

1. it reaches `ApprovalCoordinator.approve`, and the permission check runs;
2. it cannot approve by claiming authority — no caller-supplied role, user id,
   or "already verified" flag is accepted in place of the check.

Three surfaces, one boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from writai.intake.approval import (
    ApprovalAttemptRequest,
    ApprovalChannel,
    ApprovalCoordinator,
    ApprovalDisposition,
    ApprovalEvidence,
    PendingApproval,
)

PERMISSION = "approve_compliance"


def _pending() -> PendingApproval:
    return PendingApproval(
        workspace_id="csv-exports",
        decision_id="DEC-018",
        supersedes_id="DEC-004",
        affected_scopes=frozenset({"export.authorization"}),
        permission_id=PERMISSION,
        source_ref="slack://T1/C1/1",
        title="Admin-only exports",
        text="Exports must be admin-only.",
        effective_at=datetime(2026, 7, 25, 4, 0, tzinfo=UTC),
        requirements={"export.authorization": {"audience": "admin_only"}},
        proposal_fingerprint="sha256:" + ("a" * 64),
        proposal_instance_id="csv-exports:proposal:1",
        evidence_refs=("slack://T1/C1/1",),
    )


class _RecordingChecker:
    """The one permission check. Records every question it is asked."""

    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.questions: list[tuple[str, str]] = []

    def has_permission(self, *, user_id: str, permission_id: str) -> bool:
        self.questions.append((user_id, permission_id))
        return self.allowed


class _WorkspacePort:
    def __init__(self) -> None:
        self.applied: list[ApprovalEvidence] = []

    def approve_decision(
        self,
        *,
        pending: PendingApproval,
        evidence: ApprovalEvidence,
    ) -> dict[str, Any]:
        self.applied.append(evidence)
        return {"id": pending.workspace_id, "supervisor": {"assignments": []}}


def _coordinator(checker: _RecordingChecker) -> tuple[ApprovalCoordinator, _WorkspacePort]:
    port = _WorkspacePort()
    return (
        ApprovalCoordinator(
            permission_checker=checker,
            workspace_port=port,
            clock=lambda: datetime(2026, 7, 25, 5, 0, tzinfo=UTC),
        ),
        port,
    )


# --------------------------------------------------------------------------------------
# Every surface reaches the check
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "channel",
    [ApprovalChannel.CLI, ApprovalChannel.WORKSPACE_UI, ApprovalChannel.SLACK_REACTION],
)
def test_each_surface_reaches_the_one_permission_check(
    channel: ApprovalChannel,
) -> None:
    """Same pending change, same coordinator, three channels, one question asked."""

    checker = _RecordingChecker(allowed=True)
    coordinator, port = _coordinator(checker)

    result = coordinator.approve(
        pending=_pending(),
        approver_user_id="H-COMPLIANCE",
        channel=channel,
        evidence_ref=f"{channel.value}://approval/DEC-018",
    )

    assert result.disposition is ApprovalDisposition.APPROVED
    # The check ran, with the pending change's OWN permission id — not one the
    # surface chose.
    assert checker.questions == [("H-COMPLIANCE", PERMISSION)]
    # And the evidence records which surface it came through, so an audit can
    # tell a CLI approval from a Slack one.
    (evidence,) = port.applied
    assert evidence.channel is channel
    assert evidence.approver_user_id == "H-COMPLIANCE"


@pytest.mark.parametrize(
    "channel",
    [ApprovalChannel.CLI, ApprovalChannel.WORKSPACE_UI, ApprovalChannel.SLACK_REACTION],
)
def test_no_surface_can_approve_when_the_check_says_no(
    channel: ApprovalChannel,
) -> None:
    """A refusal is a refusal on every surface, and nothing is applied."""

    checker = _RecordingChecker(allowed=False)
    coordinator, port = _coordinator(checker)

    result = coordinator.approve(
        pending=_pending(),
        approver_user_id="H-NOT-AUTHORIZED",
        channel=channel,
        evidence_ref=f"{channel.value}://approval/DEC-018",
    )

    assert result.disposition is ApprovalDisposition.IGNORED_NOT_AUTHORIZED
    assert checker.questions == [("H-NOT-AUTHORIZED", PERMISSION)]
    assert port.applied == []


# --------------------------------------------------------------------------------------
# No surface can approve by asserting it already checked
# --------------------------------------------------------------------------------------


def test_the_web_envelope_refuses_a_caller_supplied_identity_or_role() -> None:
    """The public envelope carries a TOKEN, never an answer.

    A surface that could post `approver_user_id` or `actor_role` would be
    asserting the outcome of the check instead of submitting to it. The model is
    `extra="forbid"`, so each of these is rejected at parse time rather than
    quietly ignored.
    """

    valid = {
        "approval_token": "tok-live",
        "channel": "workspace-ui",
        "evidence_ref": "workspace-ui://approvals/csv-exports/DEC-018",
        "confirmed_proposal_fingerprint": "sha256:" + ("a" * 64),
        "confirmed_proposal_instance_id": "csv-exports:proposal:1",
    }
    assert ApprovalAttemptRequest.model_validate(valid)

    for smuggled in (
        {"approver_user_id": "H-ME"},
        {"actor_role": PERMISSION},
        {"permission_id": PERMISSION},
        {"already_verified": True},
        {"has_permission": True},
    ):
        with pytest.raises(Exception) as raised:
            ApprovalAttemptRequest.model_validate({**valid, **smuggled})
        assert "extra" in str(raised.value).lower(), smuggled


def test_the_coordinator_ignores_everything_except_the_checked_identity() -> None:
    """There is no argument that can stand in for the permission check.

    `approve` takes an approver id and a channel. It does not take a role, a
    verdict, or a "trusted" flag, so no caller can express "I already checked".
    """

    import inspect

    signature = inspect.signature(ApprovalCoordinator.approve)
    assert set(signature.parameters) == {
        "self",
        "pending",
        "approver_user_id",
        "channel",
        "evidence_ref",
    }


def test_the_legacy_cli_role_command_is_disabled() -> None:
    """`workspace approve-change --role X` took the role on trust. It is gone.

    This is the concrete shape of "a surface asserting it already checked", and
    it shipped once. It now refuses rather than being quietly rerouted.
    """

    import argparse

    from writai.cli import CliError, _request_for_command

    for command in ("approve-baseline", "approve-change"):
        args = argparse.Namespace(
            command=command,
            workspace_id="csv-exports",
            decision_id="DEC-018",
            role=PERMISSION,
        )
        with pytest.raises(CliError) as raised:
            _request_for_command(client=None, args=args)  # type: ignore[arg-type]
        assert raised.value.code == "COMMAND_DEPRECATED"
        assert "verified" in str(raised.value)


def test_the_slack_surface_cannot_supply_its_own_approver(tmp_path: Path) -> None:
    """A signed Slack delivery authenticates Composio, not a human.

    The reacting user id is Slack's, and it only becomes an approver by being
    resolved through the identity binding. An unresolved reactor never reaches
    the check — the surface cannot promote its own caller.
    """

    from writai.intake.slack import VerifiedSlackReaction
    from writai.notify.slack import (
        JsonSlackApprovalThreads,
        SlackReactionApprovalHandler,
    )

    store = JsonSlackApprovalThreads(tmp_path / "threads.json")
    store.register(
        team_id="T1", channel_id="C1", message_ts="1700000000.000100", pending=_pending()
    )
    checker = _RecordingChecker(allowed=True)
    coordinator, port = _coordinator(checker)

    class _NoIdentity:
        def resolve_hexclave_user_id(
            self, *, pending: PendingApproval, reaction: VerifiedSlackReaction
        ) -> str | None:
            return None

    reaction = VerifiedSlackReaction(
        event_id="Ev-1",
        connection_user_id="U-CONNECTION",
        reacting_user_id="U-SLACK-STRANGER",
        team_id="T1",
        channel_id="C1",
        message_ts="1700000000.000100",
        reaction=SlackReactionApprovalHandler.APPROVE_REACTION,
        delivered_at=datetime(2026, 7, 25, 5, 0, tzinfo=UTC),
        evidence_ref="slack://T1/C1/1#reaction-1",
    )
    handler = SlackReactionApprovalHandler(
        pending_resolver=store,
        identity_resolver=_NoIdentity(),
        coordinator=coordinator,
    )

    result = handler.handle(reaction)

    # No identity, so the check was never asked and nothing was applied. The raw
    # Slack user id never becomes an approver on its own.
    assert checker.questions == []
    assert port.applied == []
    assert result.approval is None


def test_an_approval_records_the_identity_that_was_checked_not_the_one_claimed(
    tmp_path: Path,
) -> None:
    """The evidence must name the user the check ran against.

    If the recorded approver could differ from the checked one, an audit would
    be describing a decision nobody actually authorised.
    """

    checker = _RecordingChecker(allowed=True)
    coordinator, port = _coordinator(checker)

    coordinator.approve(
        pending=_pending(),
        approver_user_id="  H-COMPLIANCE  ",
        channel=ApprovalChannel.SLACK_REACTION,
        evidence_ref="slack://T1/C1/1#reaction-1",
    )

    (checked_user, _permission) = checker.questions[0]
    (evidence,) = port.applied
    assert evidence.approver_user_id == checked_user == "H-COMPLIANCE"
    assert evidence.permission_id == PERMISSION
    # The proposal binding travels with it, so the approval cannot land on a
    # proposal that moved after the approver saw it.
    assert evidence.confirmed_proposal_fingerprint == _pending().proposal_fingerprint
    assert evidence.confirmed_proposal_instance_id == _pending().proposal_instance_id
