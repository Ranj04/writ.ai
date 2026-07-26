from __future__ import annotations

from datetime import UTC, datetime

from dragback.authority.engine import IntentAuthority
from dragback.domain import ApprovalStatus, EdgeKind, ValidityStatus, Verdict
from dragback.fixtures import load_decision_v18, load_graph_fixture
from dragback.grants import GrantSigner
from dragback.graph.memory import MemoryGraphStore
from dragback.llm import (
    DecisionExtractionCandidate,
    EvidenceSpan,
    FixtureDecisionExtractor,
    TrustedDecisionContext,
    apply_extracted_decision,
)

SOURCE_TEXT = (
    "Compliance approved this change: exports must be admin-only for every account."
)
EVIDENCE_TEXT = "exports must be admin-only"
SOURCE_REF = "slack://compliance/thread-018"
TRUSTED_EFFECTIVE_AT = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
MODEL_EFFECTIVE_AT = datetime(2099, 1, 1, tzinfo=UTC)


def trusted_context(*, confidence: float = 0.97) -> TrustedDecisionContext:
    return TrustedDecisionContext(
        source_ref=SOURCE_REF,
        approval_status=ApprovalStatus.APPROVED,
        authority_role="compliance",
        confidence=confidence,
        effective_at=TRUSTED_EFFECTIVE_AT,
        supersedes_id="DEC-004",
        affected_scopes={"export.authorization"},
    )


def make_authority() -> IntentAuthority:
    version, artifacts, edges, _ = load_graph_fixture()
    graph = MemoryGraphStore()
    graph.reset(version=version, artifacts=artifacts, edges=edges)
    return IntentAuthority(graph=graph, signer=GrantSigner("test-secret"))


def candidate_for(mutation=None, *, evidence_text: str = EVIDENCE_TEXT):
    start = SOURCE_TEXT.index(EVIDENCE_TEXT)
    return DecisionExtractionCandidate(
        mutation=mutation or load_decision_v18(),
        evidence_spans=[
            EvidenceSpan(
                start=start,
                end=start + len(EVIDENCE_TEXT),
                text=evidence_text,
            )
        ],
    )


def assert_graph_was_not_mutated(authority: IntentAuthority) -> None:
    assert authority.graph.version_label == "graph-v17"
    assert all(artifact.id != "DEC-018" for artifact in authority.graph.list_artifacts())
    assert all(edge.source_id != "DEC-018" for edge in authority.graph.list_edges())


def test_exact_evidence_span_is_persisted_before_authority_applies_candidate() -> None:
    authority = make_authority()

    result = apply_extracted_decision(
        authority=authority,
        raw_text=SOURCE_TEXT,
        context=trusted_context(),
        candidate=candidate_for(),
    )

    assert result.applied is True
    assert result.graph_version == "graph-v18"
    stored = authority.graph.get_artifact("DEC-018")
    assert stored.source_ref == SOURCE_REF
    assert stored.attributes["validated_evidence_spans"] == [
        {
            "source_ref": SOURCE_REF,
            "start": SOURCE_TEXT.index(EVIDENCE_TEXT),
            "end": SOURCE_TEXT.index(EVIDENCE_TEXT) + len(EVIDENCE_TEXT),
            "text": EVIDENCE_TEXT,
        }
    ]
    supersession = next(
        edge
        for edge in authority.graph.list_edges()
        if edge.source_id == "DEC-018" and edge.kind is EdgeKind.SUPERSEDES
    )
    assert supersession.evidence_ref == SOURCE_REF


def test_nonexistent_evidence_routes_to_review_without_graph_mutation() -> None:
    authority = make_authority()

    result = apply_extracted_decision(
        authority=authority,
        raw_text=SOURCE_TEXT,
        context=trusted_context(),
        candidate=candidate_for(evidence_text="exports may be public"),
    )

    assert result.applied is False
    assert result.verdict is Verdict.HUMAN_REVIEW
    assert "does not exactly match" in result.reason
    assert_graph_was_not_mutated(authority)


def test_out_of_bounds_evidence_routes_to_review_without_graph_mutation() -> None:
    authority = make_authority()
    candidate = candidate_for()
    candidate.evidence_spans[0].end = len(SOURCE_TEXT) + 1

    result = apply_extracted_decision(
        authority=authority,
        raw_text=SOURCE_TEXT,
        context=trusted_context(),
        candidate=candidate,
    )

    assert result.applied is False
    assert result.verdict is Verdict.HUMAN_REVIEW
    assert "extends beyond" in result.reason
    assert_graph_was_not_mutated(authority)


def test_low_confidence_extraction_uses_authority_review_without_graph_mutation() -> None:
    authority = make_authority()
    mutation = load_decision_v18()

    result = apply_extracted_decision(
        authority=authority,
        raw_text=SOURCE_TEXT,
        context=trusted_context(confidence=0.5),
        candidate=candidate_for(mutation),
    )

    assert result.applied is False
    assert result.verdict is Verdict.HUMAN_REVIEW
    assert "confidence" in result.reason.lower()
    assert_graph_was_not_mutated(authority)


def test_model_cannot_fabricate_governance_metadata() -> None:
    authority = make_authority()
    mutation = load_decision_v18()
    mutation.decision.approval_status = ApprovalStatus.PROPOSAL
    mutation.decision.authority_role = "untrusted-model-role"
    mutation.decision.confidence = 1
    mutation.decision.effective_at = MODEL_EFFECTIVE_AT
    mutation.decision.scopes = {"export.generation"}
    mutation.decision.validity = ValidityStatus.INVALIDATED
    mutation.decision.invalidated_scopes = {"export.generation"}
    mutation.supersedes_id = "TASK-101"
    mutation.affected_scopes = {"export.generation"}

    result = apply_extracted_decision(
        authority=authority,
        raw_text=SOURCE_TEXT,
        context=trusted_context(),
        candidate=candidate_for(mutation),
    )

    assert result.applied is True
    stored = authority.graph.get_artifact("DEC-018")
    assert stored.approval_status is ApprovalStatus.APPROVED
    assert stored.authority_role == "compliance"
    assert stored.confidence == 0.97
    assert stored.effective_at == TRUSTED_EFFECTIVE_AT
    assert stored.scopes == {"export.authorization"}
    assert stored.validity is ValidityStatus.VALID
    assert stored.invalidated_scopes == set()
    supersession = next(
        edge
        for edge in authority.graph.list_edges()
        if edge.source_id == "DEC-018" and edge.kind is EdgeKind.SUPERSEDES
    )
    assert supersession.target_id == "DEC-004"
    assert supersession.scopes == {"export.authorization"}


def test_fixture_extractor_emits_an_exact_candidate_without_optional_llm_dependency() -> None:
    extractor = FixtureDecisionExtractor(
        load_decision_v18(),
        evidence_text=EVIDENCE_TEXT,
    )

    candidate = extractor.extract(SOURCE_TEXT)

    assert candidate.evidence_spans == [
        EvidenceSpan(
            start=SOURCE_TEXT.index(EVIDENCE_TEXT),
            end=SOURCE_TEXT.index(EVIDENCE_TEXT) + len(EVIDENCE_TEXT),
            text=EVIDENCE_TEXT,
        )
    ]
