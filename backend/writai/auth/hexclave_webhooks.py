"""Verified Hexclave authority-change webhooks and redacted evidence storage."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from svix.webhooks import Webhook
from writai.domain import utc_now

HexclaveAuthorityEventType = Literal[
    "team_permission.created",
    "team_permission.deleted",
    "team_membership.created",
    "team_membership.deleted",
]

SUPPORTED_HEXCLAVE_AUTHORITY_EVENTS = frozenset(
    {
        "team_permission.created",
        "team_permission.deleted",
        "team_membership.created",
        "team_membership.deleted",
    }
)
HEXCLAVE_WEBHOOK_MAX_BODY_BYTES = 64 * 1024


class HexclaveWebhookError(ValueError):
    """A sanitized Hexclave webhook failure."""


class HexclaveWebhookConfigurationError(HexclaveWebhookError):
    """The webhook verifier is missing required configuration."""


class HexclaveWebhookVerificationError(HexclaveWebhookError):
    """The Svix signature or timestamp could not be verified."""


class HexclaveWebhookPayloadError(HexclaveWebhookError):
    """A verified payload does not match the documented event contract."""


class HexclaveWebhookEvidenceStoreError(RuntimeError):
    """The redacted webhook evidence store is unavailable or inconsistent."""


class _HexclaveEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: str = Field(min_length=1, max_length=255)
    data: dict[str, Any]


class VerifiedHexclaveAuthorityEvent(BaseModel):
    """Only the authority identifiers needed to invalidate cached permissions."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    delivery_id: str = Field(min_length=1, max_length=255)
    event_type: HexclaveAuthorityEventType
    delivered_at: datetime
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    team_id: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=255)
    permission_id: str | None = Field(default=None, min_length=1, max_length=255)

    @property
    def evidence_ref(self) -> str:
        return f"hexclave-webhook://{self.delivery_id}"


class HexclaveWebhookVerification(BaseModel):
    """A verified delivery; unsupported signed event types carry no authority event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=255)
    event: VerifiedHexclaveAuthorityEvent | None = None


class HexclaveCacheInvalidationRequest(BaseModel):
    """Sanitized metadata forwarded across the trusted service boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    delivery_id: str = Field(min_length=1, max_length=255)
    event_type: HexclaveAuthorityEventType
    evidence_ref: str = Field(min_length=1, max_length=1_000)


def _header(
    headers: object,
    canonical_name: str,
    legacy_name: str,
) -> str:
    items_method = getattr(headers, "items", None)
    if not callable(items_method):
        raise HexclaveWebhookVerificationError(
            "Hexclave webhook signature verification failed."
        )
    matches = [
        value.strip()
        for key, value in items_method()
        if isinstance(key, str)
        and key.casefold()
        in {canonical_name.casefold(), legacy_name.casefold()}
        and isinstance(value, str)
        and value.strip()
    ]
    if not matches or len(set(matches)) != 1:
        raise HexclaveWebhookVerificationError(
            "Hexclave webhook signature verification failed."
        )
    return matches[0]


def _verification_headers(headers: object) -> dict[str, str]:
    items_method = getattr(headers, "items", None)
    if not callable(items_method):
        raise HexclaveWebhookVerificationError(
            "Hexclave webhook signature verification failed."
        )
    return {
        key: value
        for key, value in items_method()
        if isinstance(key, str) and isinstance(value, str)
    }


def _required_identifier(data: dict[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise HexclaveWebhookPayloadError(
            "Hexclave returned an invalid authority-change payload."
        )
    return value.strip()


class SvixHexclaveWebhookVerifier:
    """Verify the raw delivery before exposing a schema-checked authority event."""

    def __init__(self, *, secret: str | None) -> None:
        normalized = secret.strip() if isinstance(secret, str) else ""
        if not normalized:
            raise HexclaveWebhookConfigurationError(
                "HEXCLAVE_WEBHOOK_SECRET must be configured."
            )
        try:
            self._webhook = Webhook(normalized)
        except Exception as exc:
            raise HexclaveWebhookConfigurationError(
                "HEXCLAVE_WEBHOOK_SECRET is invalid."
            ) from exc

    def verify(
        self,
        *,
        body: bytes,
        headers: object,
    ) -> HexclaveWebhookVerification:
        if len(body) > HEXCLAVE_WEBHOOK_MAX_BODY_BYTES:
            raise HexclaveWebhookPayloadError(
                "The Hexclave webhook payload is too large."
            )
        delivery_id = _header(headers, "svix-id", "webhook-id")
        timestamp = _header(headers, "svix-timestamp", "webhook-timestamp")
        _header(headers, "svix-signature", "webhook-signature")
        try:
            payload = self._webhook.verify(
                body,
                _verification_headers(headers),
            )
        except Exception as exc:
            raise HexclaveWebhookVerificationError(
                "Hexclave webhook signature verification failed."
            ) from exc

        try:
            envelope = _HexclaveEnvelope.model_validate(payload)
        except ValidationError as exc:
            raise HexclaveWebhookPayloadError(
                "Hexclave returned an invalid webhook payload."
            ) from exc

        event_type = envelope.type
        if event_type not in SUPPORTED_HEXCLAVE_AUTHORITY_EVENTS:
            return HexclaveWebhookVerification(
                delivery_id=delivery_id,
                event_type=event_type,
            )

        try:
            delivered_at = datetime.fromtimestamp(
                int(timestamp),
                tz=UTC,
            )
        except (OverflowError, ValueError) as exc:
            raise HexclaveWebhookVerificationError(
                "Hexclave webhook signature verification failed."
            ) from exc

        permission_id = (
            _required_identifier(envelope.data, "id")
            if event_type.startswith("team_permission.")
            else None
        )
        try:
            event = VerifiedHexclaveAuthorityEvent(
                delivery_id=delivery_id,
                event_type=cast(HexclaveAuthorityEventType, event_type),
                delivered_at=delivered_at,
                payload_sha256=hashlib.sha256(body).hexdigest(),
                team_id=_required_identifier(envelope.data, "team_id"),
                user_id=_required_identifier(envelope.data, "user_id"),
                permission_id=permission_id,
            )
        except ValidationError as exc:
            raise HexclaveWebhookPayloadError(
                "Hexclave returned an invalid authority-change payload."
            ) from exc
        return HexclaveWebhookVerification(
            delivery_id=delivery_id,
            event_type=event_type,
            event=event,
        )


class HexclaveWebhookEvidenceRecord(BaseModel):
    """Redacted, immutable evidence that a signed authority event was handled."""

    model_config = ConfigDict(extra="forbid")

    delivery_id: str = Field(min_length=1, max_length=255)
    event_type: HexclaveAuthorityEventType
    delivered_at: datetime
    received_at: datetime = Field(default_factory=utc_now)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ref: str = Field(min_length=1, max_length=1_000)
    team_id: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=255)
    permission_id: str | None = Field(default=None, min_length=1, max_length=255)
    status: Literal["reserved", "completed"] = "reserved"
    authority_caches_cleared: int | None = Field(default=None, ge=0)
    agent_caches_cleared: int | None = Field(default=None, ge=0)


class _HexclaveWebhookEvidenceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    deliveries: list[HexclaveWebhookEvidenceRecord] = Field(default_factory=list)


class JsonHexclaveWebhookEvidenceStore:
    """Atomic, idempotent storage containing no raw webhook body or credentials."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = RLock()

    def _read(self) -> _HexclaveWebhookEvidenceDocument:
        if not self.path.exists():
            return _HexclaveWebhookEvidenceDocument()
        try:
            return _HexclaveWebhookEvidenceDocument.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError) as exc:
            raise HexclaveWebhookEvidenceStoreError(
                "The Hexclave webhook evidence store is unreadable."
            ) from exc

    def _write(self, document: _HexclaveWebhookEvidenceDocument) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            output = os.fdopen(
                descriptor,
                "w",
                encoding="utf-8",
            )
            descriptor = None
            with output:
                output.write(document.model_dump_json(indent=2) + "\n")
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
            self.path.chmod(0o600)
        except OSError as exc:
            raise HexclaveWebhookEvidenceStoreError(
                "The Hexclave webhook evidence store is unavailable."
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary.exists():
                temporary.unlink()

    def reserve(self, event: VerifiedHexclaveAuthorityEvent) -> str:
        """Return ``completed`` for a handled replay; reserved deliveries are retried."""

        candidate = HexclaveWebhookEvidenceRecord(
            delivery_id=event.delivery_id,
            event_type=event.event_type,
            delivered_at=event.delivered_at,
            payload_sha256=event.payload_sha256,
            evidence_ref=event.evidence_ref,
            team_id=event.team_id,
            user_id=event.user_id,
            permission_id=event.permission_id,
        )
        with self._lock:
            document = self._read()
            for item in document.deliveries:
                if item.delivery_id != event.delivery_id:
                    continue
                immutable = item.model_copy(
                    update={
                        "received_at": candidate.received_at,
                        "status": candidate.status,
                        "authority_caches_cleared": None,
                        "agent_caches_cleared": None,
                    }
                )
                if immutable != candidate:
                    raise HexclaveWebhookEvidenceStoreError(
                        "A Hexclave webhook delivery ID was reused."
                    )
                return item.status
            document.deliveries.append(candidate)
            self._write(document)
            return "reserved"

    def complete(
        self,
        delivery_id: str,
        *,
        authority_caches_cleared: int,
        agent_caches_cleared: int,
    ) -> HexclaveWebhookEvidenceRecord:
        with self._lock:
            document = self._read()
            for index, item in enumerate(document.deliveries):
                if item.delivery_id != delivery_id:
                    continue
                completed = item.model_copy(
                    update={
                        "status": "completed",
                        "authority_caches_cleared": authority_caches_cleared,
                        "agent_caches_cleared": agent_caches_cleared,
                    }
                )
                if item.status == "completed" and item != completed:
                    raise HexclaveWebhookEvidenceStoreError(
                        "Completed Hexclave webhook evidence is immutable."
                    )
                document.deliveries[index] = completed
                self._write(document)
                return completed.model_copy(deep=True)
        raise HexclaveWebhookEvidenceStoreError(
            "The Hexclave webhook was not reserved before completion."
        )

    def get(self, delivery_id: str) -> HexclaveWebhookEvidenceRecord | None:
        with self._lock:
            for item in self._read().deliveries:
                if item.delivery_id == delivery_id:
                    return item.model_copy(deep=True)
        return None
