"""Deterministic, review-only replay of CrustData person watcher deliveries.

The bundled fallback fixture is reconstructed from CrustData's documented webhook
shape and is labelled accordingly. A captured delivery uses separate provenance
and a server-owned CrustData-person-to-Hexclave-user binding. This module has no
graph mutation dependency; a person change can only produce human review flags.
"""

from __future__ import annotations

import hmac
import json
import os
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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


class CrustDataAuthenticationConfigurationError(CrustDataAuthenticationError):
    pass


class CrustDataPayloadError(ValueError):
    pass


class CrustDataIdentityMappingError(RuntimeError):
    pass


class CrustDataCaptureError(RuntimeError):
    pass


class CrustDataCapturedReplayError(ValueError):
    pass


class CrustDataWebhookBearerVerifier:
    """Authenticate a capture or replay request with a caller-managed bearer."""

    def __init__(self, *, expected_bearer: str) -> None:
        self._expected_bearer = expected_bearer

    def require(self, authorization: str | None) -> None:
        expected = self._expected_bearer.strip()
        if not expected:
            raise CrustDataAuthenticationNotConfigured(
                "CrustData delivery authentication is not configured."
            )
        scheme, separator, supplied = (authorization or "").partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not supplied.strip()
            or not hmac.compare_digest(supplied.strip(), expected)
        ):
            raise CrustDataAuthenticationError(
                "CrustData delivery authentication failed."
            )


def require_distinct_crustdata_bearers(
    *,
    webhook_bearer: str | None,
    replay_bearer: str | None,
) -> None:
    """Prevent the external callback sender from inheriting operator replay access."""

    webhook = (webhook_bearer or "").strip()
    replay = (replay_bearer or "").strip()
    if webhook and replay and hmac.compare_digest(webhook, replay):
        raise CrustDataAuthenticationConfigurationError(
            "CrustData callback and replay credentials must be distinct."
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
    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=500)
    current_title: str | None = Field(default=None, max_length=500)
    headline: str | None = Field(default=None, max_length=1_000)


class CrustDataPersonRecord(BaseModel):
    """The minimal person projection retained for deterministic review."""

    model_config = ConfigDict(extra="ignore", frozen=True)

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


class CrustDataCapturedProvenance(BaseModel):
    """Server-owned callback capture provenance; no vendor signature is asserted."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    kind: Literal["captured"]
    label: Literal[
        "configured CrustData callback payload, replayed from server capture "
        "(not live; no vendor signature verified)"
    ] = (
        "configured CrustData callback payload, replayed from server capture "
        "(not live; no vendor signature verified)"
    )
    received_by_configured_callback: Literal[True]
    callback_authentication: Literal["configured-shared-bearer"]
    vendor_signature_verified: Literal[False] = False
    captured_at: datetime
    capture_evidence_ref: str = Field(min_length=1, max_length=1_000)
    notice: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_capture_time(self) -> CrustDataCapturedProvenance:
        if self.captured_at.utcoffset() is None:
            raise ValueError("CrustData captured_at must be timezone-aware.")
        return self


CrustDataReplayProvenance = Annotated[
    CrustDataFixtureProvenance | CrustDataCapturedProvenance,
    Field(discriminator="kind"),
]
CrustDataSourceLabel = Literal[
    "documentation-reconstructed payload, replayed (not captured from CrustData)",
    "configured CrustData callback payload, replayed from server capture "
    "(not live; no vendor signature verified)",
]


class CrustDataPersonIdentityBinding(BaseModel):
    """Human-provisioned binding between vendor and authority identities."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    crustdata_person_id: int = Field(ge=1)
    hexclave_user_id: str = Field(min_length=1, max_length=255)
    evidence_ref: str = Field(min_length=1, max_length=1_000)


class CrustDataPersonIdentityBindings(BaseModel):
    """Server-owned identity directory; webhook data cannot assert this mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    people: tuple[CrustDataPersonIdentityBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_bindings(self) -> CrustDataPersonIdentityBindings:
        person_ids = [item.crustdata_person_id for item in self.people]
        if len(person_ids) != len(set(person_ids)):
            raise ValueError("Each CrustData person may have only one identity binding.")
        user_ids = [item.hexclave_user_id for item in self.people]
        if len(user_ids) != len(set(user_ids)):
            raise ValueError("Each Hexclave user may have only one CrustData identity.")
        return self

    def binding_for(
        self,
        crustdata_person_id: str,
    ) -> CrustDataPersonIdentityBinding | None:
        try:
            normalized_person_id = int(crustdata_person_id)
        except ValueError:
            return None
        return next(
            (
                item
                for item in self.people
                if item.crustdata_person_id == normalized_person_id
            ),
            None,
        )

    @classmethod
    def from_json(
        cls,
        raw: str | None,
    ) -> CrustDataPersonIdentityBindings | None:
        if raw is None or not raw.strip():
            return None
        try:
            return cls.model_validate_json(raw)
        except (TypeError, ValueError, ValidationError) as exc:
            raise CrustDataIdentityMappingError(
                "CrustData person identity bindings are invalid."
            ) from exc


class CrustDataReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_provenance: CrustDataReplayProvenance
    payload: CrustDataPersonWebhookPayload


class CrustDataCaptureReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["captured"] = "captured"
    source_label: Literal[
        "configured CrustData callback payload, captured for replay "
        "(not processed live; no vendor signature verified)"
    ] = (
        "configured CrustData callback payload, captured for replay "
        "(not processed live; no vendor signature verified)"
    )
    capture_id: str = Field(pattern=r"^crustdata-capture-[0-9a-f]{64}$")
    capture_file_name: str = Field(
        pattern=r"^crustdata-capture-[0-9a-f]{64}\.json$"
    )
    delivery_key: CrustDataDeliveryKey
    captured_at: datetime
    duplicate: bool
    graph_mutated: Literal[False] = False
    human_review_created: Literal[False] = False


class FileCrustDataCaptureStore:
    """Persist minimized callback deliveries as immutable replay files."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser()
        self._lock = RLock()

    def capture(
        self,
        payload: CrustDataPersonWebhookPayload,
    ) -> CrustDataCaptureReceipt:
        key, capture_id, file_name, path = self._location(payload)

        with self._lock:
            if path.exists():
                existing = self._read_existing(path)
                if existing.payload != payload:
                    raise CrustDataCaptureError(
                        "A CrustData capture key already exists with another payload."
                    )
                provenance = existing.fixture_provenance
                if not isinstance(provenance, CrustDataCapturedProvenance):
                    raise CrustDataCaptureError(
                        "The existing CrustData capture provenance is invalid."
                    )
                return CrustDataCaptureReceipt(
                    capture_id=capture_id,
                    capture_file_name=file_name,
                    delivery_key=key,
                    captured_at=provenance.captured_at,
                    duplicate=True,
                )

            captured_at = utc_now()
            request = CrustDataReplayRequest(
                fixture_provenance=CrustDataCapturedProvenance(
                    kind="captured",
                    received_by_configured_callback=True,
                    callback_authentication="configured-shared-bearer",
                    vendor_signature_verified=False,
                    captured_at=captured_at,
                    capture_evidence_ref=f"crustdata://captures/{capture_id}",
                    notice=(
                        "Received by writ.ai's configured CrustData callback with "
                        "the shared bearer and stored for replay. CrustData does "
                        "not document a vendor signature; replaying this file is "
                        "not a live delivery."
                    ),
                ),
                payload=payload,
            )
            self._write(path, request)
            return CrustDataCaptureReceipt(
                capture_id=capture_id,
                capture_file_name=file_name,
                delivery_key=key,
                captured_at=captured_at,
                duplicate=False,
            )

    def require_stored(self, request: CrustDataReplayRequest) -> None:
        """Require exact equality with the immutable server-side callback capture."""

        provenance = request.fixture_provenance
        if not isinstance(provenance, CrustDataCapturedProvenance):
            raise CrustDataCapturedReplayError(
                "The replay does not carry captured provenance."
            )
        _, capture_id, _, path = self._location(request.payload)
        expected_evidence_ref = f"crustdata://captures/{capture_id}"
        if provenance.capture_evidence_ref != expected_evidence_ref:
            raise CrustDataCapturedReplayError(
                "Captured replay provenance does not match its delivery key."
            )

        with self._lock:
            if not path.is_file():
                raise CrustDataCapturedReplayError(
                    "Captured replay provenance has no server-side capture."
                )
            existing = self._read_existing(path)
            if existing != request:
                raise CrustDataCapturedReplayError(
                    "Captured replay content does not match the server-side capture."
                )

    def _location(
        self,
        payload: CrustDataPersonWebhookPayload,
    ) -> tuple[CrustDataDeliveryKey, str, str, Path]:
        key = CrustDataDeliveryKey(
            watch_id=payload.metadata.watch_id,
            run_id=payload.metadata.run_id,
            notification_id=payload.metadata.notification_id,
        )
        fingerprint = stable_hash(
            key.model_dump(mode="json")
        ).removeprefix("sha256:")
        capture_id = f"crustdata-capture-{fingerprint}"
        file_name = f"{capture_id}.json"
        return key, capture_id, file_name, self.directory / file_name

    def _read_existing(self, path: Path) -> CrustDataReplayRequest:
        try:
            return CrustDataReplayRequest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, TypeError, ValueError, ValidationError) as exc:
            raise CrustDataCaptureError(
                "The existing CrustData capture is unreadable."
            ) from exc

    def _write(self, path: Path, request: CrustDataReplayRequest) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor: int | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = None
                handle.write(request.model_dump_json(indent=2, by_alias=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except OSError as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                descriptor = None
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise CrustDataCaptureError(
                "The CrustData capture could not be persisted."
            ) from exc


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
    source_label: CrustDataSourceLabel = (
        "documentation-reconstructed payload, replayed (not captured from CrustData)"
    )
    person_id: str
    person_name: str
    change_kind: PersonChangeKind
    changes: tuple[PersonChangeEvidence, ...]
    identity_binding_evidence_ref: str | None = None
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
    source_label: CrustDataSourceLabel = (
        "documentation-reconstructed payload, replayed (not captured from CrustData)"
    )
    fixture_provenance: CrustDataReplayProvenance
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
    source_label: CrustDataSourceLabel,
    identity_binding_evidence_ref: str | None,
) -> DecisionApprovalReviewFlag:
    fingerprint_source = {
        "delivery_key": delivery_key.model_dump(mode="json"),
        "person_id": observation.person_id,
        "workspace_id": evidence.workspace_id,
        "decision_id": evidence.decision_id,
        "approval_evidence_ref": evidence.evidence_ref,
        "approved_at": evidence.approved_at,
    }
    if identity_binding_evidence_ref is not None:
        fingerprint_source["identity_binding_evidence_ref"] = (
            identity_binding_evidence_ref
        )
    fingerprint = stable_hash(fingerprint_source).removeprefix("sha256:")
    change_text = "; ".join(
        (
            f"{change.field} {change.change_type} from "
            f"{_json_value(change.old_value)} to {_json_value(change.new_value)}"
        )
        for change in observation.changes
    )
    return DecisionApprovalReviewFlag(
        flag_id=f"crustdata-review-{fingerprint}",
        source_label=source_label,
        person_id=observation.person_id,
        person_name=observation.person_name,
        change_kind=observation.change_kind,
        changes=observation.changes,
        identity_binding_evidence_ref=identity_binding_evidence_ref,
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
            + (
                f"identity binding evidence {identity_binding_evidence_ref}; "
                if identity_binding_evidence_ref is not None
                else ""
            )
            + "the graph was not changed."
        ),
    )


class CrustDataPersonObservationService:
    """Join person changes to persisted approvals and durably reserve delivery IDs."""

    def __init__(
        self,
        *,
        replay_store: JsonCrustDataDeliveryReplayStore,
        expected_api_version: str,
        identity_bindings: CrustDataPersonIdentityBindings | None = None,
    ) -> None:
        self._replay_store = replay_store
        self._expected_api_version = expected_api_version
        self._identity_bindings = identity_bindings

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
        captured = isinstance(
            request.fixture_provenance,
            CrustDataCapturedProvenance,
        )
        resolved_identities = tuple(
            (
                observation,
                self._resolve_approver_identity(
                    observation,
                    captured=captured,
                ),
            )
            for observation in observations
        )
        approvals = sorted(approval_evidence, key=_approval_key)
        flags = tuple(
            _review_flag(
                delivery_key=key,
                observation=observation,
                evidence=evidence,
                source_label=request.fixture_provenance.label,
                identity_binding_evidence_ref=(
                    binding.evidence_ref if binding is not None else None
                ),
            )
            for observation, binding in resolved_identities
            for evidence in approvals
            if evidence.approver_user_id
            == (
                binding.hexclave_user_id
                if binding is not None
                else observation.person_id
            )
        )
        result = CrustDataObservationResult(
            status="completed",
            source_label=request.fixture_provenance.label,
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

    def _resolve_approver_identity(
        self,
        observation: PersonObservation,
        *,
        captured: bool,
    ) -> CrustDataPersonIdentityBinding | None:
        if not captured:
            return None
        if self._identity_bindings is None:
            raise CrustDataIdentityMappingError(
                "Captured CrustData deliveries require server-owned identity bindings."
            )
        binding = self._identity_bindings.binding_for(observation.person_id)
        if binding is None:
            raise CrustDataIdentityMappingError(
                "A captured CrustData person has no Hexclave identity binding."
            )
        return binding

    def _duplicate_result(
        self,
        key: CrustDataDeliveryKey,
        provenance: CrustDataReplayProvenance,
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
