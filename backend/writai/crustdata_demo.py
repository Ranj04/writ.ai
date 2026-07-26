"""Deterministic CrustData fallback rehearsal.

This module deliberately exercises only the documentation-reconstructed replay
path. It never calls CrustData, accepts a callback, or writes product state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from writai.intake.approval import ApprovalChannel, ApprovalEvidence
from writai.intake.crustdata import (
    CrustDataObservationResult,
    CrustDataPersonObservationService,
    CrustDataReplayRequest,
)
from writai.intake.replay import JsonCrustDataDeliveryReplayStore

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = (
    REPO_ROOT
    / "fixtures"
    / "crustdata_person_role_change_documentation_reconstructed.json"
)
EXPECTED_SOURCE_LABEL = (
    "documentation-reconstructed payload, replayed (not captured from CrustData)"
)


def _documentation_reconstructed_approval_fixture() -> ApprovalEvidence:
    """Load fixture evidence; this is not an approval channel or graph write."""

    return ApprovalEvidence.model_validate(
        {
            "workspace_id": "workspace-alpha",
            "decision_id": "DEC-ALPHA",
            "approver_user_id": "6324687",
            "permission_id": "approve_compliance",
            "channel": ApprovalChannel.CLI,
            "evidence_ref": "fixture://crustdata-demo/approval-alpha",
            "approved_at": datetime(2026, 7, 15, 18, 30, tzinfo=UTC),
            "confirmed_proposal_fingerprint": "sha256:" + "a" * 64,
            "confirmed_proposal_instance_id": (
                "fixture-crustdata-demo-proposal-alpha"
            ),
        }
    )


def run_rehearsal() -> CrustDataObservationResult:
    """Run the real review-only service against an isolated replay ledger."""

    request = CrustDataReplayRequest.model_validate_json(
        FIXTURE_PATH.read_text(encoding="utf-8")
    )
    with TemporaryDirectory(prefix="writai-crustdata-rehearsal-") as directory:
        result = CrustDataPersonObservationService(
            replay_store=JsonCrustDataDeliveryReplayStore(
                Path(directory) / "deliveries.json"
            ),
            expected_api_version="2025-11-01",
        ).process(
            request,
            approval_evidence=(_documentation_reconstructed_approval_fixture(),),
        )
    _validate_rehearsal(result)
    return result


def _validate_rehearsal(result: CrustDataObservationResult) -> None:
    if result.fixture_provenance.kind != "documentation-reconstructed":
        raise RuntimeError("The fallback rehearsal no longer has reconstructed provenance.")
    if result.source_label != EXPECTED_SOURCE_LABEL:
        raise RuntimeError("The fallback rehearsal source label changed.")
    if result.duplicate or result.graph_mutated or not result.human_review_required:
        raise RuntimeError("The fallback rehearsal violated its review-only contract.")
    if len(result.flags) != 1:
        raise RuntimeError("The fallback rehearsal must produce exactly one review flag.")
    flag = result.flags[0]
    if (
        flag.review_status != "pending-human-review"
        or not flag.human_confirmation_required
        or flag.graph_mutated
    ):
        raise RuntimeError("The fallback review flag violated its human-review contract.")


def render_rehearsal(result: CrustDataObservationResult) -> str:
    """Render only stable facts so repeated rehearsals are byte-for-byte equal."""

    flag = result.flags[0]
    change = flag.changes[0]
    return "\n".join(
        (
            "CRUSTDATA REHEARSAL — DOCUMENTATION-RECONSTRUCTED REPLAY, NOT LIVE",
            f"Source: {result.source_label}",
            f"Person change: {change.old_value} -> {change.new_value}",
            f"Matched approval: {flag.workspace_id}/{flag.decision_id}",
            f"Review status: {flag.review_status}",
            "Human confirmation required: yes",
            "Graph mutated: no",
            "CrustData API called: no",
            "Callback captured: no",
            "Server-owned Hexclave identity binding: not exercised",
            "Sponsor evidence: no — this is not live sponsor-usage evidence",
        )
    )


def main() -> int:
    print(render_rehearsal(run_rehearsal()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
