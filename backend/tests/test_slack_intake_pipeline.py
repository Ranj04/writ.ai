from __future__ import annotations

from datetime import UTC, datetime

from dragback.domain import ApprovalStatus, Artifact, ArtifactKind, DecisionMutation
from dragback.intake.gate import DeterministicIntakeGate
from dragback.intake.slack import SlackDecisionIntake, VerifiedSlackMessage
from dragback.llm.extractor import FixtureDecisionExtractor

TEXT = "Approved: exports must be admin-only, effective immediately."


class Verifier:
    def parse_message(self, *, body, headers):
        del body, headers
        return VerifiedSlackMessage(
            event_id="evt-1",
            connection_user_id="dragback-user-1",
            author_user_id="U-COMPLIANCE",
            team_id="T1",
            channel_id="C1",
            message_ts="1784952300.000001",
            delivered_at=datetime(2026, 7, 25, 4, 5, tzinfo=UTC),
            text=TEXT,
            source_ref="slack://T1/C1/1784952300.000001",
        )


class Checker:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    def has_permission(self, *, user_id: str, permission_id: str) -> bool:
        assert user_id == "hex-user-compliance"
        assert permission_id == "approve_compliance"
        return self.allowed


def _baseline() -> Artifact:
    return Artifact(
        id="DEC-004",
        kind=ArtifactKind.DECISION,
        title="Exports available",
        scopes={"export.authorization"},
        approval_status=ApprovalStatus.APPROVED,
        authority_role="approve_compliance",
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        attributes={
            "requirements": {
                "export.authorization": {"audience": "all_users"}
            }
        },
    )


def _extractor() -> FixtureDecisionExtractor:
    return FixtureDecisionExtractor(
        DecisionMutation(
            decision=Artifact(
                id="DEC-018",
                kind=ArtifactKind.DECISION,
                title="Admin-only exports",
                text=TEXT,
                scopes={"export.authorization"},
                approval_status=ApprovalStatus.APPROVED,
                authority_role="model-role",
                confidence=0.96,
                attributes={
                    "requirements": {
                        "export.authorization": {
                            "audience": "admin_only"
                        }
                    }
                },
            ),
            supersedes_id="model-target",
            affected_scopes={"export.authorization"},
        )
    )


def _gate(allowed: bool) -> DeterministicIntakeGate:
    return DeterministicIntakeGate(
        known_scopes={"export.authorization"},
        authority_policy={
            "export.authorization": {"approve_compliance"}
        },
        permission_checker=Checker(allowed),
        confidence_threshold=0.75,
    )


def test_authoritative_slack_message_becomes_untrusted_workspace_proposal() -> None:
    outcome = SlackDecisionIntake(
        verifier=Verifier(),
        extractor=_extractor(),
        gate=_gate(True),
    ).ingest(
        workspace_id="csv-exports",
        authority_user_id="hex-user-compliance",
        body=b"signed",
        headers={},
        current_decisions=[_baseline()],
        supersession_root_id="DEC-004",
    )

    assert outcome.proposal is not None
    assert outcome.proposal.supersedes_id == "DEC-004"
    assert outcome.proposal.decision.approval_status is ApprovalStatus.PROPOSAL
    assert outcome.proposal.decision.authority_role == "approve_compliance"
    assert outcome.proposal.decision.attributes["extraction"]["human_reviewed"] is False
    assert not hasattr(outcome.gate, "verdict")


def test_non_authoritative_slack_author_is_parked_without_proposal() -> None:
    outcome = SlackDecisionIntake(
        verifier=Verifier(),
        extractor=_extractor(),
        gate=_gate(False),
    ).ingest(
        workspace_id="csv-exports",
        authority_user_id="hex-user-compliance",
        body=b"signed",
        headers={},
        current_decisions=[_baseline()],
        supersession_root_id="DEC-004",
    )

    assert outcome.proposal is None
    assert outcome.gate.eligible_for_confirmation is False


def test_later_slack_change_still_supersedes_seeded_graph_root() -> None:
    baseline = _baseline()
    baseline.invalidated_scopes = {"export.authorization"}
    previous_change = _baseline().model_copy(
        update={
            "id": "DEC-017",
            "scopes": {"export.authorization"},
            "effective_at": datetime(2026, 7, 24, tzinfo=UTC),
            "attributes": {
                "requirements": {
                    "export.authorization": {"audience": "employees"}
                }
            },
        },
        deep=True,
    )

    outcome = SlackDecisionIntake(
        verifier=Verifier(),
        extractor=_extractor(),
        gate=_gate(True),
    ).ingest(
        workspace_id="csv-exports",
        authority_user_id="hex-user-compliance",
        body=b"signed",
        headers={},
        current_decisions=[baseline, previous_change],
        supersession_root_id="DEC-004",
    )

    assert outcome.proposal is not None
    assert outcome.proposal.supersedes_id == "DEC-004"
