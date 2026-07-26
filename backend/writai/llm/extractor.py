from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, Field

from writai.domain import (
    ApprovalStatus,
    DecisionMutation,
    MutationResult,
    ValidityStatus,
    Verdict,
)

if TYPE_CHECKING:
    from writai.authority.engine import IntentAuthority


class EvidenceSpan(BaseModel):
    """An exact, half-open character span in the supplied source text."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = Field(min_length=1)


class DecisionExtractionCandidate(BaseModel):
    """Untrusted structure proposed by an extractor, never an authority verdict."""

    mutation: DecisionMutation
    evidence_spans: list[EvidenceSpan] = Field(min_length=1)


class TrustedDecisionContext(BaseModel):
    """Governance metadata supplied by authenticated ingestion, never by the LLM."""

    source_ref: str = Field(min_length=1)
    approval_status: ApprovalStatus
    authority_role: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    effective_at: datetime | None
    supersedes_id: str = Field(min_length=1)
    affected_scopes: set[str] = Field(min_length=1)


class DecisionExtractor(Protocol):
    def extract(
        self,
        raw_text: str,
        *,
        scope_vocabulary: set[str] | None = None,
    ) -> DecisionExtractionCandidate: ...


def extracted_requirements_error(
    candidate: DecisionExtractionCandidate,
) -> str | None:
    """Validate the requirements shape needed for a reviewable proposal."""

    affected_scopes = set(candidate.mutation.affected_scopes)
    if not affected_scopes:
        return "`mutation.affected_scopes` must be non-empty."

    requirements = candidate.mutation.decision.attributes.get("requirements")
    if not isinstance(requirements, dict) or not requirements:
        return (
            "`mutation.decision.attributes.requirements` must be a non-empty object."
        )
    if set(requirements) != affected_scopes:
        return (
            "Requirement keys must exactly match `mutation.affected_scopes`."
        )
    if any(not isinstance(value, dict) or not value for value in requirements.values()):
        return "Every extracted Decision requirement must be a non-empty object."
    return None


def decision_extraction_error(
    candidate: DecisionExtractionCandidate,
) -> str | None:
    """Validate model-controlled structure before it leaves an LLM adapter."""

    requirements_error = extracted_requirements_error(candidate)
    if requirements_error is not None:
        return requirements_error
    if candidate.mutation.decision.scopes != candidate.mutation.affected_scopes:
        return (
            "`mutation.decision.scopes`, `mutation.affected_scopes`, and requirement "
            "keys must exactly match."
        )
    return None


def repair_evidence_offsets(
    raw_text: str,
    candidate: DecisionExtractionCandidate,
) -> None:
    """Recompute offsets for exact source quotes, in place.

    Model-provided character arithmetic is not evidence. A quote must still occur
    verbatim in the source; fabricated quotes are left untouched so deterministic
    span validation rejects them downstream.
    """

    for span in candidate.evidence_spans:
        if raw_text[span.start : span.end] == span.text:
            continue
        found = raw_text.find(span.text, span.start)
        if found < 0:
            found = raw_text.find(span.text)
        if found < 0:
            continue
        span.start = found
        span.end = found + len(span.text)


class FixtureDecisionExtractor:
    """Deterministic adapter used by the live demo."""

    def __init__(self, mutation: DecisionMutation, *, evidence_text: str | None = None) -> None:
        self._mutation = mutation
        self._evidence_text = evidence_text

    def extract(
        self,
        raw_text: str,
        *,
        scope_vocabulary: set[str] | None = None,
    ) -> DecisionExtractionCandidate:
        del scope_vocabulary
        evidence_text = self._evidence_text or raw_text
        start = raw_text.find(evidence_text)
        if not evidence_text or start < 0:
            raise ValueError("Fixture evidence text must occur in the supplied source text.")
        return DecisionExtractionCandidate(
            mutation=self._mutation.model_copy(deep=True),
            evidence_spans=[
                EvidenceSpan(
                    start=start,
                    end=start + len(evidence_text),
                    text=evidence_text,
                )
            ],
        )


def evidence_span_error(raw_text: str, spans: list[EvidenceSpan]) -> str | None:
    """Return a deterministic validation error for the first nonexistent span."""

    for index, span in enumerate(spans):
        if span.start >= span.end:
            return f"Evidence span {index} must have start before end."
        if span.end > len(raw_text):
            return f"Evidence span {index} extends beyond the supplied source text."
        if raw_text[span.start : span.end] != span.text:
            return f"Evidence span {index} does not exactly match the supplied source text."
    return None


def apply_extracted_decision(
    *,
    authority: IntentAuthority,
    raw_text: str,
    context: TrustedDecisionContext,
    candidate: DecisionExtractionCandidate,
) -> MutationResult:
    """Validate an untrusted extraction, then delegate all verdicts to authority code."""

    span_error = evidence_span_error(raw_text, candidate.evidence_spans)
    if span_error is not None:
        return MutationResult(
            applied=False,
            reason=span_error,
            graph_version=authority.graph.version_label,
            verdict=Verdict.HUMAN_REVIEW,
        )

    mutation = candidate.mutation.model_copy(deep=True)
    mutation.decision.source_ref = context.source_ref
    mutation.decision.approval_status = context.approval_status
    mutation.decision.authority_role = context.authority_role
    mutation.decision.confidence = context.confidence
    mutation.decision.effective_at = context.effective_at
    mutation.decision.scopes = set(context.affected_scopes)
    mutation.decision.validity = ValidityStatus.VALID
    mutation.decision.invalidated_scopes = set()
    mutation.supersedes_id = context.supersedes_id
    mutation.affected_scopes = set(context.affected_scopes)
    mutation.decision.attributes["validated_evidence_spans"] = [
        {
            "source_ref": context.source_ref,
            **span.model_dump(),
        }
        for span in candidate.evidence_spans
    ]
    return authority.apply_decision_change(mutation)
