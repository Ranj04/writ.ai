"""Deterministic, review-only replay of CrustData person watcher deliveries.

No CrustData watcher delivery was captured for this implementation: the API key
is unconfigured. The bundled fixture is reconstructed from CrustData's documented
webhook shape and every result is labelled accordingly. This module has no graph
mutation dependency; a person change can only produce human review flags.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from writai.domain import utc_now
from writai.hashing import stable_hash
from writai.intake.approval import ApprovalEvidence
from writai.intake.replay import (
    CrustDataDeliveryKey,
    CrustDataDeliveryReplayError,
    JsonCrustDataDeliveryReplayStore,
)
from writai.workspaces.models import LiveWorkspaceRecord


class CrustDataAuthenticationError(RuntimeError):
    pass


class CrustDataAuthenticationNotConfigured(CrustDataAuthenticationError):
    pass


class CrustDataPayloadError(ValueError):
    pass


class CrustDataWebhookBearerVerifier:
    """Authenticate replay delivery with a caller-managed bearer token."""

    def __init__(self, *, expected_bearer: str) -> None:
        self._expected_bearer = expected_bearer

    def require(self, authorization: str | None) -> None:
        expected = self._expected_bearer.strip()
        if not expected:
            raise CrustDataAuthenticationNotConfigured(
                "CrustData replay authentication is not configured."
            )
        scheme, separator, supplied = (authorization or "").partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not supplied.strip()
            or not hmac.compare_digest(supplied.strip(), expected)
        ):
            raise CrustDataAuthenticationError(
                "CrustData replay authentication failed."
            )


class CrustDataScalarChange(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    field: str = Field(min_length=1, max_length=500)
    type: Literal["changed"]
    from_value: Any = Field(alias="from")
    to: Any


class CrustDataAddedChange(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    field: str = Field(min_length=1, max_length=500)
    type: Literal["added"]
    new_elements: tuple[dict[str, Any], ...] = Field(min_length=1)


CrustDataPersonChange = Annotated[
    CrustDataScalarChange | CrustDataAddedChange,
    Field(discriminator="type"),
]


class CrustDataBasicProfile(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=500)
    current_title: str | None = Field(default=None, max_length=500)
    headline: str | None = Field(default=None, max_length=1_000)


class CrustDataPersonRecord(BaseModel):
    """The configured watcher projection; extra documented field groups are allowed."""

    model_config = ConfigDict(extra="allow", frozen=True)

    crustdata_person_id: int = Field(ge=1)
    basic_profile: CrustDataBasicProfile


class CrustDataPersonResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    changes: tuple[CrustDataPersonChange, ...] = Field(min_length=1)
    record: CrustDataPersonRecord


class CrustDataDeliverySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delivered: int = Field(ge=1)
    total_count: int = Field(ge=1)
    max_results_per_run: int = Field(ge=1)
    truncated: bool


class CrustDataDeliveryMetadata(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    watch_id: int = Field(ge=1)
    kind: Literal["entity"]
    dataset: Literal["person"]
    api_version: str = Field(min_length=1, max_length=64)
    run_id: int = Field(ge=1)
    notification_id: str = Field(min_length=1, max_length=255)
    delivered_at: datetime
    summary: CrustDataDeliverySummary

    @model_validator(mode="after")
    def validate_delivery_time(self) -> CrustDataDeliveryMetadata:
        if self.delivered_at.utcoffset() is None:
            raise ValueError("CrustData delivered_at must be timezone-aware.")
        return self


class CrustDataPersonWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metadata: CrustDataDeliveryMetadata
    results: tuple[CrustDataPersonResult, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_delivery_counts(self) -> CrustDataPersonWebhookPayload:
        if self.metadata.summary.delivered != len(self.results):
            raise ValueError("CrustData delivered count must match results.")
        person_ids = [item.record.crustdata_person_id for item in self.results]
        if len(person_ids) != len(set(person_ids)):
            raise ValueError("A CrustData delivery may contain each person only once.")
        return self


class CrustDataFixtureProvenance(BaseModel):
    """Pinned honesty contract for the currently available replay fixture."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    kind: Literal["documentation-reconstructed"]
    label: Literal[
        "documentation-reconstructed payload, replayed (not captured from CrustData)"
    ]
    captured_from_crustdata: Literal[False]
    documentation_url: Literal[
        "https://docs.crustdata.com/watcher-docs/person/entity"
    ]
    notice: str = Field(min_length=1, max_length=1_000)


class CrustDataReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_provenance: CrustDataFixtureProvenance
    payload: CrustDataPersonWebhookPayload


class PersonChangeKind(StrEnum):
    ROLE_CHANGE = "role-change"
    DEPARTURE = "departure"


class PersonChangeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    change_type: Literal["changed", "added"]
    old_value: Any = None
    new_value: Any = None


class PersonObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    person_id: str
    person_name: str
    change_kind: PersonChangeKind
    changes: tuple[PersonChangeEvidence, ...] = Field(min_length=1)


class DecisionApprovalReviewFlag(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    flag_id: str = Field(pattern=r"^crustdata-review-[0-9a-f]{64}$")
    review_status: Literal["pending-human-review"] = "pending-human-review"
    human_confirmation_required: Literal[True] = True
    graph_mutated: Literal[False] = False
    source_label: Literal[
        "documentation-reconstructed payload, replayed (not captured from CrustData)"
    ] = "documentation-reconstructed payload, replayed (not captured from CrustData)"
    person_id: str
    person_name: str
    change_kind: PersonChangeKind
    changes: tuple[PersonChangeEvidence, ...]
    workspace_id: str
    decision_id: str
    permission_id: str
    approval_channel: str
    approval_evidence_ref: str
    approved_at: datetime
    explanation: str


class CrustDataObservationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["completed", "delivery-reserved"]
    source_label: Literal[
        "documentation-reconstructed payload, replayed (not captured from CrustData)"
    ] = "documentation-reconstructed payload, replayed (not captured from CrustData)"
    fixture_provenance: CrustDataFixtureProvenance
    delivery_key: CrustDataDeliveryKey
    duplicate: bool
    graph_mutated: Literal[False] = False
    human_review_required: bool
    observed_person_ids: tuple[str, ...]
    flags: tuple[DecisionApprovalReviewFlag, ...] = ()
    existing_flag_ids: tuple[str, ...] = ()
    processed_at: datetime = Field(default_factory=utc_now)


def approval_evidence_from_workspace_records(
    records: Iterable[LiveWorkspaceRecord],
) -> tuple[ApprovalEvidence, ...]:
    """Read every persisted human approval without changing workspace state."""

    evidence: list[ApprovalEvidence] = []
    for record in records:
        if record.baseline_approval_evidence is not None:
            evidence.append(record.baseline_approval_evidence.model_copy(deep=True))
        evidence.extend(
            item.approval_evidence.model_copy(deep=True)
            for item in record.approved_mutations
            if item.approval_evidence is not None
        )
    return tuple(evidence)


def observe_person_changes(
    payload: CrustDataPersonWebhookPayload,
) -> tuple[PersonObservation, ...]:
    """Classify only explicit role/departure transitions in the delivered diff."""

    observations: list[PersonObservation] = []
    for result in payload.results:
        changes = tuple(_change_evidence(item) for item in result.changes)
        departure = any(_is_departure(item) for item in result.changes)
        role_change = any(_is_role_change(item) for item in result.changes)
        if not departure and not role_change:
            continue
        observations.append(
            PersonObservation(
                person_id=str(result.record.crustdata_person_id),
                person_name=result.record.basic_profile.name,
                change_kind=(
                    PersonChangeKind.DEPARTURE
                    if departure
                    else PersonChangeKind.ROLE_CHANGE
                ),
                changes=changes,
            )
        )
    return tuple(observations)


def _change_evidence(change: CrustDataPersonChange) -> PersonChangeEvidence:
    if isinstance(change, CrustDataScalarChange):
        return PersonChangeEvidence(
            field=change.field,
            change_type=change.type,
            old_value=change.from_value,
            new_value=change.to,
        )
    return PersonChangeEvidence(
        field=change.field,
        change_type=change.type,
        old_value=None,
        new_value=change.new_elements,
    )


def _is_departure(change: CrustDataPersonChange) -> bool:
    return (
        isinstance(change, CrustDataScalarChange)
        and change.field
        in {
            "basic_profile.current_title",
            "experience.employment_details.current.name",
        }
        and change.from_value is not None
        and change.from_value != ""
        and (change.to is None or change.to == "")
    )


def _is_role_change(change: CrustDataPersonChange) -> bool:
    if isinstance(change, CrustDataAddedChange):
        return change.field == "experience.employment_details.current"
    return (
        change.field
        in {
            "basic_profile.current_title",
            "experience.employment_details.current.name",
        }
        and change.from_value != change.to
        and change.to is not None
        and change.to != ""
    )


def _approval_key(evidence: ApprovalEvidence) -> tuple[str, str, str, datetime]:
    return (
        evidence.workspace_id,
        evidence.decision_id,
        evidence.evidence_ref,
        evidence.approved_at,
    )


def _json_value(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _review_flag(
    *,
    delivery_key: CrustDataDeliveryKey,
    observation: PersonObservation,
    evidence: ApprovalEvidence,
) -> DecisionApprovalReviewFlag:
    fingerprint = stable_hash(
        {
            "delivery_key": delivery_key.model_dump(mode="json"),
            "person_id": observation.person_id,
            "workspace_id": evidence.workspace_id,
            "decision_id": evidence.decision_id,
            "approval_evidence_ref": evidence.evidence_ref,
            "approved_at": evidence.approved_at,
        }
    ).removeprefix("sha256:")
    change_text = "; ".join(
        (
            f"{change.field} {change.change_type} from "
            f"{_json_value(change.old_value)} to {_json_value(change.new_value)}"
        )
        for change in observation.changes
    )
    return DecisionApprovalReviewFlag(
        flag_id=f"crustdata-review-{fingerprint}",
        person_id=observation.person_id,
        person_name=observation.person_name,
        change_kind=observation.change_kind,
        changes=observation.changes,
        workspace_id=evidence.workspace_id,
        decision_id=evidence.decision_id,
        permission_id=evidence.permission_id,
        approval_channel=evidence.channel.value,
        approval_evidence_ref=evidence.evidence_ref,
        approved_at=evidence.approved_at,
        explanation=(
            f"{observation.person_name} ({observation.person_id}) had a "
            f"{observation.change_kind.value}: {change_text}. Review Decision "
            f"{evidence.decision_id} in Workspace {evidence.workspace_id} because "
            f"this person approved it at {evidence.approved_at.isoformat()} with "
            f"evidence {evidence.evidence_ref}. Human confirmation is required; "
            "the graph was not changed."
        ),
    )


class CrustDataPersonObservationService:
    """Join person changes to persisted approvals and durably reserve delivery IDs."""

    def __init__(
        self,
        *,
        replay_store: JsonCrustDataDeliveryReplayStore,
        expected_api_version: str,
    ) -> None:
        self._replay_store = replay_store
        self._expected_api_version = expected_api_version

    def process(
        self,
        request: CrustDataReplayRequest,
        *,
        approval_evidence: Iterable[ApprovalEvidence],
    ) -> CrustDataObservationResult:
        payload = request.payload
        if payload.metadata.api_version != self._expected_api_version:
            raise CrustDataPayloadError(
                "The CrustData payload API version does not match configuration."
            )
        key = CrustDataDeliveryKey(
            watch_id=payload.metadata.watch_id,
            run_id=payload.metadata.run_id,
            notification_id=payload.metadata.notification_id,
        )
        if not self._replay_store.reserve(key):
            return self._duplicate_result(key, request.fixture_provenance)

        observations = observe_person_changes(payload)
        approvals = sorted(approval_evidence, key=_approval_key)
        flags = tuple(
            _review_flag(
                delivery_key=key,
                observation=observation,
                evidence=evidence,
            )
            for observation in observations
            for evidence in approvals
            if evidence.approver_user_id == observation.person_id
        )
        result = CrustDataObservationResult(
            status="completed",
            fixture_provenance=request.fixture_provenance,
            delivery_key=key,
            duplicate=False,
            human_review_required=bool(flags),
            observed_person_ids=tuple(item.person_id for item in observations),
            flags=flags,
        )
        self._replay_store.complete(
            key,
            result=result.model_dump(mode="json"),
        )
        return result

    def _duplicate_result(
        self,
        key: CrustDataDeliveryKey,
        provenance: CrustDataFixtureProvenance,
    ) -> CrustDataObservationResult:
        record = self._replay_store.get(key)
        if record is None:
            raise CrustDataDeliveryReplayError(
                "The duplicate CrustData delivery reservation disappeared."
            )
        if record.result is None:
            return CrustDataObservationResult(
                status="delivery-reserved",
                fixture_provenance=provenance,
                delivery_key=key,
                duplicate=True,
                human_review_required=False,
                observed_person_ids=(),
            )
        previous = CrustDataObservationResult.model_validate(record.result)
        return previous.model_copy(
            update={
                "duplicate": True,
                "flags": (),
                "existing_flag_ids": tuple(
                    item.flag_id for item in previous.flags
                ),
            }
        )
