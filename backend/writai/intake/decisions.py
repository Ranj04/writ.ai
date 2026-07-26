from __future__ import annotations

from datetime import datetime

from writai.domain import (
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    ValidityStatus,
)
from writai.llm.extractor import (
    DecisionExtractionCandidate,
    evidence_span_error,
    extracted_requirements_error,
)
from writai.workspaces.models import WorkspaceProposalRequest


class DecisionDraftError(ValueError):
    """An extracted candidate cannot become a reviewable Workspace proposal."""


def select_supersession_target(
    *,
    decisions: list[Artifact],
    affected_scopes: set[str],
) -> Artifact:
    """Select the newest non-invalidated Decision governing every scope."""

    candidates = [
        decision
        for decision in decisions
        if decision.kind is ArtifactKind.DECISION
        and decision.approval_status is ApprovalStatus.APPROVED
        and affected_scopes <= decision.scopes
        and not (affected_scopes & decision.invalidated_scopes)
    ]
    if not candidates:
        raise DecisionDraftError(
            "No current approved Decision covers every affected scope."
        )
    candidates.sort(
        key=lambda item: (
            item.effective_at is not None,
            item.effective_at.isoformat() if item.effective_at else "",
            item.id,
        ),
        reverse=True,
    )
    return candidates[0]


def select_seeded_supersession_root(
    *,
    decisions: list[Artifact],
    affected_scopes: set[str],
    root_decision_id: str,
) -> Artifact:
    """Resolve the seeded Decision whose downstream graph carries invalidation.

    Approved changes intentionally continue to supersede this provenance root.
    A previously applied change has no copied downstream edges, so selecting it
    as the next supersession target would stop graph traversal at that leaf.
    The root may already be invalidated by an earlier change; the engine's
    monotonic invalidation model expects that and still traverses its edges.
    """

    normalized_id = root_decision_id.strip()
    candidates = [
        decision
        for decision in decisions
        if decision.id == normalized_id
        and decision.kind is ArtifactKind.DECISION
        and decision.approval_status is ApprovalStatus.APPROVED
        and affected_scopes <= decision.scopes
    ]
    if len(candidates) != 1:
        raise DecisionDraftError(
            "The seeded supersession Decision does not cover every affected scope."
        )
    return candidates[0]


def build_workspace_proposal(
    *,
    candidate: DecisionExtractionCandidate,
    raw_text: str,
    source_ref: str,
    author_user_id: str,
    authority_user_id: str,
    delivered_at: datetime,
    superseded_decision: Artifact,
    authority_permission_id: str,
) -> WorkspaceProposalRequest:
    """Turn untrusted extracted structure into a non-authoritative proposal.

    Governance fields are supplied by the signed-delivery context and the
    existing Workspace baseline, never trusted from the extractor. The result
    remains a proposal until a separate human approval passes Hexclave and the
    deterministic authority engine.
    """

    if superseded_decision.kind is not ArtifactKind.DECISION:
        raise DecisionDraftError("The supersession target must be a Decision.")
    if delivered_at.tzinfo is None or delivered_at.utcoffset() is None:
        raise DecisionDraftError("Slack delivery timestamps must be timezone-aware.")
    if not source_ref.strip():
        raise DecisionDraftError("A signed Slack source reference is required.")
    if not author_user_id.strip():
        raise DecisionDraftError("The Slack author user ID is required.")
    if not authority_user_id.strip():
        raise DecisionDraftError(
            "A bound Hexclave authority user ID is required."
        )
    if not authority_permission_id.strip():
        raise DecisionDraftError("An authority permission ID is required.")

    span_error = evidence_span_error(raw_text, candidate.evidence_spans)
    if span_error is not None:
        raise DecisionDraftError(span_error)

    extracted = candidate.mutation
    requirements_error = extracted_requirements_error(candidate)
    if requirements_error is not None:
        raise DecisionDraftError(requirements_error)
    affected_scopes = set(extracted.affected_scopes)
    if not affected_scopes <= superseded_decision.scopes:
        raise DecisionDraftError(
            "The extracted change includes scopes absent from the superseded Decision."
        )

    requirements = extracted.decision.attributes.get("requirements")
    assert isinstance(requirements, dict)

    decision = extracted.decision.model_copy(deep=True)
    decision.kind = ArtifactKind.DECISION
    decision.approval_status = ApprovalStatus.PROPOSAL
    decision.authority_role = authority_permission_id
    decision.effective_at = delivered_at
    decision.source_ref = source_ref
    decision.scopes = affected_scopes
    decision.validity = ValidityStatus.VALID
    decision.invalidated_scopes = set()
    decision.attributes = {
        **decision.attributes,
        "requirements": {
            scope: dict(requirements[scope])
            for scope in sorted(affected_scopes)
        },
        "extraction": {
            "source": "composio-slack",
            "author_user_id": author_user_id,
            "authority_user_id": authority_user_id,
            "delivered_at": delivered_at.isoformat(),
            "extraction_confidence": decision.confidence,
            "human_reviewed": False,
            "review_required": True,
            "validated_evidence_spans": [
                {
                    "source_ref": source_ref,
                    **span.model_dump(mode="json"),
                }
                for span in candidate.evidence_spans
            ],
        },
    }
    return WorkspaceProposalRequest(
        decision=decision,
        supersedes_id=superseded_decision.id,
        affected_scopes=affected_scopes,
    )
