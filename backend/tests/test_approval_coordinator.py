from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from writai.intake.approval import (
    ApprovalChannel,
    ApprovalCoordinator,
    ApprovalDisposition,
    ApprovalEvidence,
    PendingApproval,
)
from writai.supervisor_contract import InterruptResult


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
    ):
        self.calls.append((pending, evidence))
        return {
            "id": pending.workspace_id,
            "invalidation_report": {
                "paths": [
                    {
                        "artifact_id": "TASK-102",
                        "node_ids": [
                            "DEC-018",
                            "DEC-004",
                            "SPEC-009",
                            "TICKET-100",
                            "TASK-102",
                        ],
                    }
                ]
            },
            "supervisor": {
                "assignments": [
                    {
                        "id": "ASSIGNMENT-TASK-102",
                        "state": "interrupted",
                    },
                    {
                        "id": "ASSIGNMENT-TASK-101",
                        "state": "running",
                    },
                ]
            },
        }


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
        effective_at=datetime(2026, 7, 25, 4, 0, tzinfo=UTC),
        requirements={
            "export.authorization": {"audience": "admin_only"}
        },
        proposal_fingerprint="sha256:" + ("a" * 64),
        proposal_instance_id="csv-exports:proposal:1",
        evidence_refs=("slack://T1/C1/1",),
    )


def _coordinator(
    *,
    allowed: bool,
) -> tuple[ApprovalCoordinator, Checker, WorkspacePort]:
    checker = Checker(allowed)
    workspace = WorkspacePort()
    return (
        ApprovalCoordinator(
            permission_checker=checker,
            workspace_port=workspace,
            clock=lambda: datetime(2026, 7, 25, 5, 0, tzinfo=UTC),
        ),
        checker,
        workspace,
    )


def test_unauthorized_reaction_is_ignored_without_apply_or_interrupt() -> None:
    coordinator, checker, workspace = _coordinator(allowed=False)
    result = coordinator.approve(
        pending=_pending(),
        approver_user_id="U-NOT-AUTHORIZED",
        channel=ApprovalChannel.SLACK_REACTION,
        evidence_ref="slack://T1/C1/1#reaction-2",
    )

    assert result.disposition is ApprovalDisposition.IGNORED_NOT_AUTHORIZED
    assert checker.calls == [
        ("U-NOT-AUTHORIZED", "approve_compliance")
    ]
    assert workspace.calls == []


@pytest.mark.parametrize("channel", tuple(ApprovalChannel))
def test_every_approval_channel_uses_the_shared_permission_check(
    channel: ApprovalChannel,
) -> None:
    coordinator, checker, workspace = _coordinator(allowed=False)

    result = coordinator.approve(
        pending=_pending(),
        approver_user_id="U-NOT-AUTHORIZED",
        channel=channel,
        evidence_ref=f"{channel.value}://approval/DEC-018",
    )

    assert result.disposition is ApprovalDisposition.IGNORED_NOT_AUTHORIZED
    assert checker.calls == [
        ("U-NOT-AUTHORIZED", "approve_compliance")
    ]
    assert workspace.calls == []


def test_authorized_approval_persists_evidence_with_exact_partition() -> None:
    coordinator, checker, workspace = _coordinator(allowed=True)
    result = coordinator.approve(
        pending=_pending(),
        approver_user_id="U-COMPLIANCE",
        channel=ApprovalChannel.CLI,
        evidence_ref="cli://approval/DEC-018",
    )

    assert result.disposition is ApprovalDisposition.APPROVED
    assert checker.calls == [("U-COMPLIANCE", "approve_compliance")]
    assert workspace.calls[0][0] == _pending()
    evidence = workspace.calls[0][1]
    assert evidence.approver_user_id == "U-COMPLIANCE"
    assert evidence.permission_id == "approve_compliance"
    assert evidence.confirmed_proposal_fingerprint == _pending().proposal_fingerprint
    assert result.interrupt_result == InterruptResult(
        ("ASSIGNMENT-TASK-102",),
        ("ASSIGNMENT-TASK-101",),
    )
    assert result.evidence == evidence


def test_evidence_is_constructed_before_the_atomic_workspace_call() -> None:
    class FailingWorkspacePort(WorkspacePort):
        def approve_decision(
            self,
            *,
            pending: PendingApproval,
            evidence: ApprovalEvidence,
        ):
            self.calls.append((pending, evidence))
            raise RuntimeError("repository unavailable")

    checker = Checker(True)
    workspace = FailingWorkspacePort()
    coordinator = ApprovalCoordinator(
        permission_checker=checker,
        workspace_port=workspace,
        clock=lambda: datetime(2026, 7, 25, 5, 0, tzinfo=UTC),
    )

    try:
        coordinator.approve(
            pending=_pending(),
            approver_user_id="U-COMPLIANCE",
            channel=ApprovalChannel.CLI,
            evidence_ref="cli://approval/DEC-018",
        )
    except RuntimeError:
        pass

    assert workspace.calls[0][1].confirmed_proposal_fingerprint == (
        _pending().proposal_fingerprint
    )


def test_no_new_channel_can_bypass_the_shared_permission_check() -> None:
    """Pin the set of places an approval can be minted.

    `test_every_approval_channel_uses_the_shared_permission_check` proves the
    coordinator checks permission for every channel. It cannot prove that a
    channel *uses* the coordinator — a sixth route that built its own
    `ApprovalEvidence` and called the workspace port directly would pass that
    test and skip the check entirely.

    So the construction sites are enumerated. Adding one is not forbidden; it is
    made deliberate, because it means someone has to come here and say why the
    shared path did not fit.
    """

    repo_root = Path(__file__).resolve().parents[2]
    sites = {
        path.relative_to(repo_root).as_posix()
        for path in (*(repo_root / "backend" / "writai").rglob("*.py"),
                     *(repo_root / "scripts").rglob("*.py"))
        if "ApprovalEvidence(" in path.read_text(encoding="utf-8")
    }

    assert sites == {
        # The shared path itself: ApprovalCoordinator.approve, which is the
        # ONLY one that runs a permission check.
        "backend/writai/intake/approval.py",
        # Builds the envelope handed to the coordinator; it does not approve.
        "backend/writai/services/agent_api.py",
        # Demo-only, and both refuse without
        # WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1. They bypass the CHANNEL
        # authentication and no authority check, and they are disclosed on the
        # real-vs-simulated panel.
        "scripts/demo/seed.py",
        "scripts/demo/approve_in_process.py",
    }, "an approval is being minted somewhere new — does it check permission?"
