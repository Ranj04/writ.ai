"""Signed, single-use approval links and the email approval channel.

Delivery is deliberately read-only. A link first opens a confirmation surface;
only an explicit redemption is allowed to call the bound approval port. The
agent service behind that port reloads the proposal and uses
``ApprovalCoordinator`` for the current Hexclave permission check and apply.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlparse

import httpx
from dragback.hashing import stable_hash
from dragback.intake.approval import (
    ApprovalChannel,
    ApprovalResult,
    PendingApproval,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)

_MAX_TOKEN_LENGTH = 8_192
_MAX_LINK_TTL_SECONDS = 86_400
_MAX_FORWARD_ASSERTION_TTL_SECONDS = 120
_CLOCK_SKEW = timedelta(seconds=60)
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
_TOKEN_ID_PATTERN = r"^[A-Za-z0-9_-]{16,128}$"
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"


def is_high_entropy_ntfy_topic(value: str) -> bool:
    """Conservative local check; operator ACL confirmation is still required."""

    topic = value.strip()
    return (
        len(topic) >= 24
        and len(set(topic)) >= 12
        and any(character.isalpha() for character in topic)
        and any(character.isdigit() for character in topic)
    )


class ApprovalLinkError(RuntimeError):
    """A signed approval link could not be safely used."""


class ApprovalLinkConfigurationError(ApprovalLinkError):
    """Approval-link configuration is absent or unsafe."""


class InvalidApprovalLink(ApprovalLinkError):
    """The link is malformed, forged, or for another approval channel."""


class ExpiredApprovalLink(ApprovalLinkError):
    """The authenticated link is outside its short validity window."""


class ReplayedApprovalLink(ApprovalLinkError):
    """The authenticated link was already redeemed."""


class ReplayedApprovalAssertion(ApprovalLinkError):
    """The authenticated channel assertion was already presented."""


class ApprovalLinkStoreError(ApprovalLinkError):
    """The durable single-use ledger is unavailable or inconsistent."""


class EmailNotificationError(RuntimeError):
    """An approval email could not be safely composed or delivered."""


class ApprovalRecipientBindingError(ApprovalLinkConfigurationError):
    """A configured approver has no matching notification destination."""


class NotificationDeliveryMode(StrEnum):
    SIMULATED = "simulated"
    LIVE = "live"


class ApprovalDeliveryProvider(StrEnum):
    EMAIL = "email"
    NTFY = "ntfy"
    PUSHOVER = "pushover"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class ApprovalRecipientBinding(_FrozenModel):
    """Server-owned mapping from one Hexclave identity to delivery addresses."""

    workspace_id: str = Field(min_length=1, max_length=128)
    approver_user_id: str = Field(min_length=1, max_length=255)
    email: str | None = Field(
        default=None,
        min_length=3,
        max_length=320,
        pattern=_EMAIL_PATTERN,
    )
    ntfy_topic: str | None = Field(default=None, min_length=1, max_length=256)
    pushover_user_key: SecretStr | None = None

    @model_validator(mode="after")
    def validate_destinations(self) -> ApprovalRecipientBinding:
        if not any((self.email, self.ntfy_topic, self.pushover_user_key)):
            raise ValueError(
                "an approval recipient needs at least one destination"
            )
        if self.ntfy_topic is not None and (
            "/" in self.ntfy_topic
            or any(character.isspace() for character in self.ntfy_topic)
            or not is_high_entropy_ntfy_topic(self.ntfy_topic)
        ):
            raise ValueError(
                "the ntfy topic must be valid and high entropy"
            )
        if (
            self.pushover_user_key is not None
            and not self.pushover_user_key.get_secret_value().strip()
        ):
            raise ValueError("the Pushover user key must not be blank")
        return self


class _ApprovalRecipientDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    recipients: tuple[ApprovalRecipientBinding, ...]

    @model_validator(mode="after")
    def validate_unique_identities(self) -> _ApprovalRecipientDocument:
        identities = [
            (item.workspace_id, item.approver_user_id)
            for item in self.recipients
        ]
        if len(identities) != len(set(identities)):
            raise ValueError(
                "approval recipient identities must be unique per Workspace"
            )
        return self


class ApprovalRecipientDirectory:
    """Resolve notification destinations without trusting request payloads."""

    def __init__(
        self,
        recipients: tuple[ApprovalRecipientBinding, ...],
        *,
        ntfy_server: str,
    ) -> None:
        self._recipients = {
            (item.workspace_id, item.approver_user_id): item
            for item in recipients
        }
        self._ntfy_server = ntfy_server.rstrip("/")

    @classmethod
    def from_json(
        cls,
        value: str | None,
        *,
        ntfy_server: str,
    ) -> ApprovalRecipientDirectory:
        if value is None or not value.strip():
            raise ApprovalRecipientBindingError(
                "DRAGBACK_APPROVAL_RECIPIENT_BINDINGS must be configured."
            )
        try:
            document = _ApprovalRecipientDocument.model_validate_json(value)
        except (TypeError, ValueError, ValidationError) as exc:
            raise ApprovalRecipientBindingError(
                "DRAGBACK_APPROVAL_RECIPIENT_BINDINGS is invalid."
            ) from exc
        return cls(document.recipients, ntfy_server=ntfy_server)

    def destination(
        self,
        *,
        workspace_id: str,
        approver_user_id: str,
        provider: ApprovalDeliveryProvider,
    ) -> str:
        address = self.delivery_address(
            workspace_id=workspace_id,
            approver_user_id=approver_user_id,
            provider=provider,
        )
        if provider is ApprovalDeliveryProvider.NTFY:
            return f"{self._ntfy_server}/{address}"
        return address

    def delivery_address(
        self,
        *,
        workspace_id: str,
        approver_user_id: str,
        provider: ApprovalDeliveryProvider,
    ) -> str:
        binding = self._recipients.get(
            (workspace_id.strip(), approver_user_id.strip())
        )
        if binding is None:
            raise ApprovalRecipientBindingError(
                "The approver has no configured notification destination."
            )
        if provider is ApprovalDeliveryProvider.EMAIL:
            destination = binding.email
        elif provider is ApprovalDeliveryProvider.NTFY:
            destination = binding.ntfy_topic
        else:
            destination = (
                binding.pushover_user_key.get_secret_value()
                if binding.pushover_user_key is not None
                else None
            )
        if destination is None or not destination.strip():
            raise ApprovalRecipientBindingError(
                f"The approver has no configured {provider.value} destination."
            )
        return destination.strip()

    def verify(self, claims: ApprovalLinkClaims) -> None:
        self._verify_values(
            workspace_id=claims.workspace_id,
            approver_user_id=claims.approver_user_id,
            channel=claims.channel,
            provider=claims.delivery_provider,
            binding_hash=claims.recipient_binding_hash,
        )

    def verify_assertion(
        self,
        claims: ChannelApprovalAssertionClaims,
    ) -> None:
        if (
            claims.delivery_provider is None
            or claims.recipient_binding_hash is None
        ):
            raise InvalidApprovalLink(
                "The channel assertion has no recipient binding."
            )
        self._verify_values(
            workspace_id=claims.workspace_id,
            approver_user_id=claims.approver_user_id,
            channel=claims.channel,
            provider=claims.delivery_provider,
            binding_hash=claims.recipient_binding_hash,
        )

    def _verify_values(
        self,
        *,
        workspace_id: str,
        approver_user_id: str,
        channel: ApprovalChannel,
        provider: ApprovalDeliveryProvider,
        binding_hash: str,
    ) -> None:
        try:
            destination = self.destination(
                workspace_id=workspace_id,
                approver_user_id=approver_user_id,
                provider=provider,
            )
        except ApprovalRecipientBindingError as exc:
            raise InvalidApprovalLink(
                "The approval recipient binding is no longer current."
            ) from exc
        expected = recipient_binding_hash(
            channel=channel,
            provider=provider,
            recipient_ref=destination,
        )
        if not hmac.compare_digest(expected, binding_hash):
            raise InvalidApprovalLink(
                "The approval recipient binding is no longer current."
            )


class ApprovalImpact(_FrozenModel):
    """The exact non-mutating assignment partition shown to an approver."""

    interrupted_assignment_ids: tuple[str, ...] = ()
    preserved_assignment_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_partition(self) -> ApprovalImpact:
        interrupted = self.interrupted_assignment_ids
        preserved = self.preserved_assignment_ids
        if len(interrupted) != len(set(interrupted)):
            raise ValueError("interrupted assignment IDs must be unique")
        if len(preserved) != len(set(preserved)):
            raise ValueError("preserved assignment IDs must be unique")
        if set(interrupted) & set(preserved):
            raise ValueError(
                "an assignment cannot be both interrupted and preserved"
            )
        if any(not value.strip() for value in (*interrupted, *preserved)):
            raise ValueError("assignment IDs must not be blank")
        return self

    @property
    def interrupted_count(self) -> int:
        return len(self.interrupted_assignment_ids)

    @property
    def total_count(self) -> int:
        return (
            len(self.interrupted_assignment_ids)
            + len(self.preserved_assignment_ids)
        )


class ApprovalLinkClaims(_FrozenModel):
    """Authenticated capability bound to one human and one proposal instance."""

    schema_version: Literal[1] = 1
    token_id: str = Field(pattern=_TOKEN_ID_PATTERN)
    workspace_id: str = Field(min_length=1, max_length=128)
    decision_id: str = Field(min_length=1, max_length=160)
    approver_user_id: str = Field(min_length=1, max_length=255)
    channel: ApprovalChannel
    delivery_provider: ApprovalDeliveryProvider
    proposal_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    proposal_instance_id: str = Field(min_length=1, max_length=255)
    recipient_binding_hash: str = Field(pattern=_SHA256_PATTERN)
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_claims(self) -> ApprovalLinkClaims:
        if self.channel not in {
            ApprovalChannel.PUSH,
            ApprovalChannel.EMAIL,
        }:
            raise ValueError(
                "signed approval links are only valid for push or email"
            )
        if (
            self.channel is ApprovalChannel.EMAIL
            and self.delivery_provider is not ApprovalDeliveryProvider.EMAIL
        ) or (
            self.channel is ApprovalChannel.PUSH
            and self.delivery_provider
            not in {
                ApprovalDeliveryProvider.NTFY,
                ApprovalDeliveryProvider.PUSHOVER,
            }
        ):
            raise ValueError(
                "the approval channel and delivery provider do not match"
            )
        if (
            self.issued_at.utcoffset() is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("approval-link timestamps must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("approval links must expire after issuance")
        return self


class ApprovalLinkUse(_FrozenModel):
    token_id: str = Field(pattern=_TOKEN_ID_PATTERN)
    claims_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    consumed_at: datetime

    @model_validator(mode="after")
    def validate_time(self) -> ApprovalLinkUse:
        if self.consumed_at.utcoffset() is None:
            raise ValueError("approval-link consumption time must be aware")
        return self


class _ApprovalLinkUseDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    uses: list[ApprovalLinkUse] = Field(default_factory=list)


class ApprovalLinkUseStore(Protocol):
    """Durably reserve an authenticated capability exactly once."""

    def consume(
        self,
        claims: ApprovalLinkClaims,
        *,
        consumed_at: datetime,
    ) -> bool: ...


class InMemoryApprovalLinkUseStore:
    def __init__(self) -> None:
        self._uses: dict[str, ApprovalLinkUse] = {}
        self._lock = RLock()

    def consume(
        self,
        claims: ApprovalLinkClaims,
        *,
        consumed_at: datetime,
    ) -> bool:
        fingerprint = stable_hash(claims)
        with self._lock:
            existing = self._uses.get(claims.token_id)
            if existing is not None:
                if existing.claims_fingerprint != fingerprint:
                    raise ApprovalLinkStoreError(
                        "An approval-link ID is bound to different claims."
                    )
                return False
            self._uses[claims.token_id] = ApprovalLinkUse(
                token_id=claims.token_id,
                claims_fingerprint=fingerprint,
                consumed_at=consumed_at,
            )
            return True


class JsonApprovalLinkUseStore:
    """Single-process durable ledger for at-most-once link redemption."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = RLock()

    def consume(
        self,
        claims: ApprovalLinkClaims,
        *,
        consumed_at: datetime,
    ) -> bool:
        fingerprint = stable_hash(claims)
        with self._lock:
            document = self._read()
            for use in document.uses:
                if use.token_id != claims.token_id:
                    continue
                if use.claims_fingerprint != fingerprint:
                    raise ApprovalLinkStoreError(
                        "An approval-link ID is bound to different claims."
                    )
                return False
            document.uses.append(
                ApprovalLinkUse(
                    token_id=claims.token_id,
                    claims_fingerprint=fingerprint,
                    consumed_at=consumed_at,
                )
            )
            self._write(document)
            return True

    def _read(self) -> _ApprovalLinkUseDocument:
        if not self.path.exists():
            return _ApprovalLinkUseDocument()
        try:
            return _ApprovalLinkUseDocument.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ApprovalLinkStoreError(
                "The approval-link use store is unreadable."
            ) from exc

    def _write(self, document: _ApprovalLinkUseDocument) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.tmp"
        )
        try:
            temporary.write_text(
                document.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
            self.path.chmod(0o600)
        except OSError as exc:
            raise ApprovalLinkStoreError(
                "The approval-link use store could not be persisted."
            ) from exc
        finally:
            if temporary.exists():
                temporary.unlink()


class _SqliteAtomicUseStore:
    """Cross-process atomic replay ledger backed by a UNIQUE primary key."""

    def __init__(self, path: str | Path, *, table: str) -> None:
        if table not in {"approval_link_uses", "approval_assertion_uses"}:
            raise ApprovalLinkStoreError("The replay-ledger table is invalid.")
        self.path = Path(path).expanduser()
        self._table = table
        self._initialize()

    def consume(
        self,
        *,
        use_id: str,
        claims_fingerprint: str,
        consumed_at: datetime,
    ) -> bool:
        if consumed_at.utcoffset() is None:
            raise ApprovalLinkStoreError(
                "Replay-ledger timestamps must be timezone-aware."
            )
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=5,
                isolation_level=None,
            )
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                f"INSERT OR IGNORE INTO {self._table} "
                "(use_id, claims_fingerprint, consumed_at) VALUES (?, ?, ?)",
                (
                    use_id,
                    claims_fingerprint,
                    consumed_at.astimezone(UTC).isoformat(),
                ),
            )
            if inserted.rowcount == 1:
                connection.execute("COMMIT")
                return True
            existing = connection.execute(
                f"SELECT claims_fingerprint FROM {self._table} "
                "WHERE use_id = ?",
                (use_id,),
            ).fetchone()
            if existing is None or existing[0] != claims_fingerprint:
                connection.execute("ROLLBACK")
                raise ApprovalLinkStoreError(
                    "A replay-ledger ID is bound to different claims."
                )
            connection.execute("COMMIT")
            return False
        except ApprovalLinkStoreError:
            raise
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise ApprovalLinkStoreError(
                "The atomic approval replay ledger is unavailable."
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(self.path, timeout=5) as connection:
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute(
                    f"CREATE TABLE IF NOT EXISTS {self._table} ("
                    "use_id TEXT PRIMARY KEY,"
                    "claims_fingerprint TEXT NOT NULL,"
                    "consumed_at TEXT NOT NULL"
                    ")"
                )
            self.path.chmod(0o600)
        except (OSError, sqlite3.Error) as exc:
            raise ApprovalLinkStoreError(
                "The atomic approval replay ledger could not be initialized."
            ) from exc


class SqliteApprovalLinkUseStore:
    """Durable link ledger whose primary-key insert is atomic across processes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._store = _SqliteAtomicUseStore(
            self.path,
            table="approval_link_uses",
        )

    def consume(
        self,
        claims: ApprovalLinkClaims,
        *,
        consumed_at: datetime,
    ) -> bool:
        return self._store.consume(
            use_id=claims.token_id,
            claims_fingerprint=stable_hash(claims),
            consumed_at=consumed_at,
        )


def recipient_binding_hash(
    *,
    channel: ApprovalChannel,
    provider: ApprovalDeliveryProvider,
    recipient_ref: str,
) -> str:
    """Hash the exact server-resolved delivery destination into a capability."""

    normalized = recipient_ref.strip()
    if provider is ApprovalDeliveryProvider.EMAIL:
        normalized = normalized.casefold()
    if not normalized:
        raise ApprovalLinkConfigurationError(
            "An approval recipient binding is required."
        )
    valid_pair = (
        channel is ApprovalChannel.EMAIL
        and provider is ApprovalDeliveryProvider.EMAIL
    ) or (
        channel is ApprovalChannel.PUSH
        and provider
        in {
            ApprovalDeliveryProvider.NTFY,
            ApprovalDeliveryProvider.PUSHOVER,
        }
    )
    if not valid_pair:
        raise ApprovalLinkConfigurationError(
            "The approval channel and delivery provider do not match."
        )
    digest_input = f"{channel.value}\0{provider.value}\0{normalized}"
    return "sha256:" + hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


class ApprovalLinkSigner:
    """Issue and authenticate compact HMAC approval capabilities."""

    def __init__(
        self,
        *,
        secret: str,
        ttl_seconds: int = 900,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        token_id_factory: Callable[[], str] = (
            lambda: secrets.token_urlsafe(24)
        ),
    ) -> None:
        if len(secret.encode("utf-8")) < 32:
            raise ApprovalLinkConfigurationError(
                "The approval-link HMAC secret must contain at least 32 bytes."
            )
        if not 1 <= ttl_seconds <= _MAX_LINK_TTL_SECONDS:
            raise ApprovalLinkConfigurationError(
                "The approval-link TTL must be between 1 and 86400 seconds."
            )
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._token_id_factory = token_id_factory

    def issue(
        self,
        *,
        pending: PendingApproval,
        approver_user_id: str,
        channel: ApprovalChannel,
        delivery_provider: ApprovalDeliveryProvider,
        recipient_ref: str,
    ) -> str:
        normalized_user_id = approver_user_id.strip()
        normalized_recipient = recipient_ref.strip()
        if not normalized_user_id or not normalized_recipient:
            raise ApprovalLinkConfigurationError(
                "An approver identity and recipient binding are required."
            )
        now = self._aware_now()
        claims = ApprovalLinkClaims(
            token_id=self._token_id_factory(),
            workspace_id=pending.workspace_id,
            decision_id=pending.decision_id,
            approver_user_id=normalized_user_id,
            channel=channel,
            delivery_provider=delivery_provider,
            proposal_fingerprint=pending.proposal_fingerprint,
            proposal_instance_id=pending.proposal_instance_id,
            recipient_binding_hash=recipient_binding_hash(
                channel=channel,
                provider=delivery_provider,
                recipient_ref=normalized_recipient,
            ),
            issued_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )
        body = _b64encode(
            json.dumps(
                claims.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(
                self._secret,
                body.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        return f"{body}.{signature}"

    def decode(
        self,
        token: str,
        *,
        expected_channel: ApprovalChannel,
    ) -> ApprovalLinkClaims:
        normalized = token.strip()
        if (
            not normalized
            or len(normalized) > _MAX_TOKEN_LENGTH
            or not normalized.isascii()
            or any(character.isspace() for character in normalized)
        ):
            raise InvalidApprovalLink("The approval link is malformed.")
        parts = normalized.split(".")
        if len(parts) != 2 or not all(parts):
            raise InvalidApprovalLink("The approval link is malformed.")
        body, signature = parts
        expected_signature = _b64encode(
            hmac.new(
                self._secret,
                body.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(signature, expected_signature):
            raise InvalidApprovalLink(
                "The approval-link signature is invalid."
            )
        try:
            raw: Any = json.loads(_b64decode(body).decode("utf-8"))
            claims = ApprovalLinkClaims.model_validate(raw)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise InvalidApprovalLink(
                "The approval-link claims are invalid."
            ) from exc
        if (
            expected_channel
            not in {ApprovalChannel.PUSH, ApprovalChannel.EMAIL}
            or claims.channel is not expected_channel
        ):
            raise InvalidApprovalLink(
                "The approval link belongs to another channel."
            )
        now = self._aware_now()
        if claims.issued_at > now + _CLOCK_SKEW:
            raise InvalidApprovalLink(
                "The approval link was issued in the future."
            )
        if claims.expires_at <= now:
            raise ExpiredApprovalLink("The approval link has expired.")
        return claims

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.utcoffset() is None:
            raise ApprovalLinkConfigurationError(
                "The approval-link clock must be timezone-aware."
            )
        return now


class ChannelApprovalAssertionClaims(_FrozenModel):
    """Short-lived authority-to-agent capability minted after channel proof."""

    schema_version: Literal[1] = 1
    audience: Literal["dragback-agent-channel-approval"] = (
        "dragback-agent-channel-approval"
    )
    assertion_id: str = Field(pattern=_TOKEN_ID_PATTERN)
    source_token_id: str = Field(pattern=_TOKEN_ID_PATTERN)
    workspace_id: str = Field(min_length=1, max_length=128)
    decision_id: str = Field(min_length=1, max_length=160)
    approver_user_id: str = Field(min_length=1, max_length=255)
    channel: ApprovalChannel
    evidence_ref: str = Field(min_length=1, max_length=1_000)
    delivery_provider: ApprovalDeliveryProvider | None = None
    proposal_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    proposal_instance_id: str = Field(min_length=1, max_length=255)
    recipient_binding_hash: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
    )
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_assertion(
        self,
    ) -> ChannelApprovalAssertionClaims:
        is_slack = self.channel is ApprovalChannel.SLACK_REACTION
        valid_notification = (
            self.channel is ApprovalChannel.EMAIL
            and self.delivery_provider is ApprovalDeliveryProvider.EMAIL
            and self.recipient_binding_hash is not None
        ) or (
            self.channel is ApprovalChannel.PUSH
            and self.delivery_provider
            in {
                ApprovalDeliveryProvider.NTFY,
                ApprovalDeliveryProvider.PUSHOVER,
            }
            and self.recipient_binding_hash is not None
        )
        if (
            is_slack
            and (
                self.delivery_provider is not None
                or self.recipient_binding_hash is not None
            )
        ) or (not is_slack and not valid_notification):
            raise ValueError(
                "the channel assertion has invalid delivery binding claims"
            )
        if (
            self.issued_at.utcoffset() is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError(
                "notification assertion timestamps must be timezone-aware"
            )
        if self.expires_at <= self.issued_at:
            raise ValueError(
                "notification assertions must expire after issuance"
            )
        return self


class ChannelApprovalAssertionUseStore(Protocol):
    def consume(
        self,
        claims: ChannelApprovalAssertionClaims,
        *,
        consumed_at: datetime,
    ) -> bool: ...


class SqliteChannelApprovalAssertionUseStore:
    """Independent durable ledger for authority-to-agent capabilities."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._store = _SqliteAtomicUseStore(
            self.path,
            table="approval_assertion_uses",
        )

    def consume(
        self,
        claims: ChannelApprovalAssertionClaims,
        *,
        consumed_at: datetime,
    ) -> bool:
        return self._store.consume(
            use_id=claims.assertion_id,
            claims_fingerprint=stable_hash(claims),
            consumed_at=consumed_at,
        )


class ChannelApprovalAssertionSigner:
    """Mint and verify a domain-separated authority-to-agent capability."""

    _PREFIX = "daf1"

    def __init__(
        self,
        *,
        secret: str,
        ttl_seconds: int = 30,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        assertion_id_factory: Callable[[], str] = (
            lambda: secrets.token_urlsafe(24)
        ),
    ) -> None:
        secret_bytes = secret.encode("utf-8")
        if len(secret_bytes) < 32:
            raise ApprovalLinkConfigurationError(
                "The approval assertion secret must contain at least 32 bytes."
            )
        if not 1 <= ttl_seconds <= _MAX_FORWARD_ASSERTION_TTL_SECONDS:
            raise ApprovalLinkConfigurationError(
                "The approval assertion TTL must be between 1 and 120 seconds."
            )
        self._key = hmac.new(
            secret_bytes,
            b"dragback:notification-forward:v1",
            hashlib.sha256,
        ).digest()
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._assertion_id_factory = assertion_id_factory

    def issue_notification(self, claims: ApprovalLinkClaims) -> SecretStr:
        now = self._aware_now()
        expires_at = min(
            claims.expires_at,
            now + timedelta(seconds=self._ttl_seconds),
        )
        assertion = ChannelApprovalAssertionClaims(
            assertion_id=self._assertion_id_factory(),
            source_token_id=claims.token_id,
            workspace_id=claims.workspace_id,
            decision_id=claims.decision_id,
            approver_user_id=claims.approver_user_id,
            channel=claims.channel,
            evidence_ref=(
                f"{claims.channel.value}://approval/{claims.token_id}"
            ),
            delivery_provider=claims.delivery_provider,
            proposal_fingerprint=claims.proposal_fingerprint,
            proposal_instance_id=claims.proposal_instance_id,
            recipient_binding_hash=claims.recipient_binding_hash,
            issued_at=now,
            expires_at=expires_at,
        )
        return self._encode(assertion)

    def issue_slack(
        self,
        *,
        pending: PendingApproval,
        approver_user_id: str,
        evidence_ref: str,
    ) -> SecretStr:
        now = self._aware_now()
        return self._encode(
            ChannelApprovalAssertionClaims(
                assertion_id=self._assertion_id_factory(),
                source_token_id=secrets.token_urlsafe(24),
                workspace_id=pending.workspace_id,
                decision_id=pending.decision_id,
                approver_user_id=approver_user_id,
                channel=ApprovalChannel.SLACK_REACTION,
                evidence_ref=evidence_ref,
                proposal_fingerprint=pending.proposal_fingerprint,
                proposal_instance_id=pending.proposal_instance_id,
                issued_at=now,
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
        )

    def _encode(
        self,
        assertion: ChannelApprovalAssertionClaims,
    ) -> SecretStr:
        body = _b64encode(
            json.dumps(
                assertion.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signed = f"{self._PREFIX}.{body}"
        signature = _b64encode(
            hmac.new(
                self._key,
                signed.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        return SecretStr(f"{signed}.{signature}")

    def decode(
        self,
        token: str,
        *,
        expected_workspace_id: str,
        expected_decision_id: str,
    ) -> ChannelApprovalAssertionClaims:
        normalized = token.strip()
        if (
            not normalized
            or len(normalized) > _MAX_TOKEN_LENGTH
            or not normalized.isascii()
            or any(character.isspace() for character in normalized)
        ):
            raise InvalidApprovalLink(
                "The notification approval assertion is malformed."
            )
        parts = normalized.split(".")
        if (
            len(parts) != 3
            or parts[0] != self._PREFIX
            or not all(parts)
        ):
            raise InvalidApprovalLink(
                "The notification approval assertion is malformed."
            )
        prefix, body, signature = parts
        expected_signature = _b64encode(
            hmac.new(
                self._key,
                f"{prefix}.{body}".encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(signature, expected_signature):
            raise InvalidApprovalLink(
                "The notification approval assertion signature is invalid."
            )
        try:
            raw: Any = json.loads(_b64decode(body).decode("utf-8"))
            claims = ChannelApprovalAssertionClaims.model_validate(raw)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise InvalidApprovalLink(
                "The notification approval assertion claims are invalid."
            ) from exc
        if (
            claims.workspace_id != expected_workspace_id
            or claims.decision_id != expected_decision_id
        ):
            raise InvalidApprovalLink(
                "The notification approval assertion targets another proposal."
            )
        now = self._aware_now()
        if claims.issued_at > now + _CLOCK_SKEW:
            raise InvalidApprovalLink(
                "The notification approval assertion was issued in the future."
            )
        if claims.expires_at <= now:
            raise ExpiredApprovalLink(
                "The notification approval assertion has expired."
            )
        return claims

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.utcoffset() is None:
            raise ApprovalLinkConfigurationError(
                "The approval assertion clock must be timezone-aware."
            )
        return now


class BoundApprovalPort(Protocol):
    """Trusted boundary that forwards claims to the shared approval path."""

    def approve_bound(
        self,
        *,
        claims: ApprovalLinkClaims,
        evidence_ref: str,
    ) -> ApprovalResult: ...


class ApprovalRecipientVerifier(Protocol):
    """Re-resolve the current server-side destination for signed claims."""

    def verify(self, claims: ApprovalLinkClaims) -> None: ...


@dataclass(frozen=True)
class ApprovalLinkRedemption:
    claims: ApprovalLinkClaims
    result: ApprovalResult


class ApprovalLinkRedeemer:
    """Authenticate, consume once, then invoke the shared approval boundary."""

    def __init__(
        self,
        *,
        signer: ApprovalLinkSigner,
        use_store: ApprovalLinkUseStore,
        approval_port: BoundApprovalPort,
        recipient_verifier: ApprovalRecipientVerifier,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._signer = signer
        self._use_store = use_store
        self._approval_port = approval_port
        self._recipient_verifier = recipient_verifier
        self._clock = clock

    def inspect(
        self,
        *,
        token: str,
        expected_channel: ApprovalChannel,
    ) -> ApprovalLinkClaims:
        """Validate without reserving; safe for email scanners and previews."""

        claims = self._signer.decode(
            token,
            expected_channel=expected_channel,
        )
        self._recipient_verifier.verify(claims)
        return claims

    def redeem(
        self,
        *,
        token: str,
        expected_channel: ApprovalChannel,
    ) -> ApprovalLinkRedemption:
        claims = self.inspect(
            token=token,
            expected_channel=expected_channel,
        )
        now = self._clock()
        if now.utcoffset() is None:
            raise ApprovalLinkConfigurationError(
                "The approval-link clock must be timezone-aware."
            )
        if not self._use_store.consume(claims, consumed_at=now):
            raise ReplayedApprovalLink(
                "The approval link has already been used."
            )
        result = self._approval_port.approve_bound(
            claims=claims,
            evidence_ref=(
                f"{claims.channel.value}://approval/{claims.token_id}"
            ),
        )
        return ApprovalLinkRedemption(claims=claims, result=result)


class ApprovalLinkUrls(_FrozenModel):
    """Secret-bearing URLs; model dumps and reprs redact their values."""

    confirmation_url: SecretStr
    redemption_url: SecretStr


def build_approval_link_urls(
    *,
    public_base_url: str,
    channel: ApprovalChannel,
    token: str,
) -> ApprovalLinkUrls:
    if channel not in {ApprovalChannel.PUSH, ApprovalChannel.EMAIL}:
        raise ApprovalLinkConfigurationError(
            "Approval links require the push or email channel."
        )
    base = _safe_public_base_url(public_base_url)
    encoded = quote(token, safe="")
    return ApprovalLinkUrls(
        confirmation_url=SecretStr(
            f"{base}/approvals/{channel.value}/confirm#token={encoded}"
        ),
        redemption_url=SecretStr(
            f"{base}/approvals/{channel.value}/redeem"
        ),
    )


class EmailApprovalDispatchRequest(_FrozenModel):
    approver_user_id: str = Field(min_length=1, max_length=255)


class ApprovalEmail(_FrozenModel):
    recipient_email: str = Field(
        min_length=3,
        max_length=320,
        pattern=_EMAIL_PATTERN,
    )
    sender_email: str = Field(
        min_length=3,
        max_length=320,
        pattern=_EMAIL_PATTERN,
    )
    subject: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=20_000)
    html: str = Field(min_length=1, max_length=40_000)
    delivery_mode: NotificationDeliveryMode


class EmailDeliveryReceipt(_FrozenModel):
    provider: str = Field(min_length=1, max_length=64)
    message_id: str = Field(min_length=1, max_length=255)
    delivery_mode: NotificationDeliveryMode


class ApprovalEmailSender(Protocol):
    @property
    def delivery_mode(self) -> NotificationDeliveryMode: ...

    def send(self, message: ApprovalEmail) -> EmailDeliveryReceipt: ...


class ResendEmailSender:
    """Pinned REST adapter for a real Resend email submission."""

    API_URL = "https://api.resend.com/emails"
    delivery_mode: Literal[NotificationDeliveryMode.LIVE] = (
        NotificationDeliveryMode.LIVE
    )

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 5,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise EmailNotificationError("The Resend API key is missing.")
        if timeout_seconds <= 0:
            raise EmailNotificationError(
                "The Resend timeout must be greater than zero."
            )
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client or httpx.Client()

    def send(self, message: ApprovalEmail) -> EmailDeliveryReceipt:
        if message.delivery_mode is not self.delivery_mode:
            raise EmailNotificationError(
                "The email message has the wrong delivery mode."
            )
        try:
            response = self._http_client.post(
                self.API_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "from": message.sender_email,
                    "to": [message.recipient_email],
                    "subject": message.subject,
                    "text": message.text,
                    "html": message.html,
                },
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmailNotificationError(
                "The Resend approval email could not be delivered."
            ) from exc
        message_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(message_id, str) or not message_id.strip():
            raise EmailNotificationError(
                "Resend returned an invalid delivery receipt."
            )
        return EmailDeliveryReceipt(
            provider="resend",
            message_id=message_id,
            delivery_mode=self.delivery_mode,
        )


class EmailNotificationResult(_FrozenModel):
    receipt: EmailDeliveryReceipt
    token_id: str = Field(pattern=_TOKEN_ID_PATTERN)
    proposal_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    proposal_instance_id: str = Field(min_length=1, max_length=255)
    expires_at: datetime


class EmailApprovalNotifier:
    """Send a proposal-bound email without performing any approval."""

    def __init__(
        self,
        *,
        signer: ApprovalLinkSigner,
        public_base_url: str,
        sender_email: str,
        sender: ApprovalEmailSender,
    ) -> None:
        self._signer = signer
        self._public_base_url = public_base_url
        self._sender_email = sender_email
        self._sender = sender

    def notify(
        self,
        *,
        pending: PendingApproval,
        approver_user_id: str,
        recipient_email: str,
        impact: ApprovalImpact = ApprovalImpact(),
    ) -> EmailNotificationResult:
        token = self._signer.issue(
            pending=pending,
            approver_user_id=approver_user_id,
            channel=ApprovalChannel.EMAIL,
            delivery_provider=ApprovalDeliveryProvider.EMAIL,
            recipient_ref=recipient_email.casefold(),
        )
        claims = self._signer.decode(
            token,
            expected_channel=ApprovalChannel.EMAIL,
        )
        urls = build_approval_link_urls(
            public_base_url=self._public_base_url,
            channel=ApprovalChannel.EMAIL,
            token=token,
        )
        confirmation_url = urls.confirmation_url.get_secret_value()
        mode = self._sender.delivery_mode
        mode_label = (
            "LIVE external email"
            if mode is NotificationDeliveryMode.LIVE
            else "SIMULATED email; no external delivery"
        )
        scopes = ", ".join(sorted(pending.affected_scopes))
        impact_text = (
            f"{impact.interrupted_count} of {impact.total_count} active "
            "assignments would be interrupted."
        )
        text = (
            f"Dragback approval requested ({mode_label}).\n\n"
            f"Decision: {pending.title}\n"
            f"Proposed change: {pending.text}\n"
            f"Affected scopes: {scopes}\n"
            f"Effective at: {pending.effective_at.isoformat()}\n"
            f"Blast radius: {impact_text}\n"
            f"Proposal: {pending.proposal_fingerprint}\n"
            f"Instance: {pending.proposal_instance_id}\n\n"
            "Open this link to review. Opening it does not approve anything; "
            "the confirmation page requires an explicit Approve action. "
            "Hexclave permission is checked again at approval time.\n"
            f"{confirmation_url}"
        )
        safe_title = html.escape(pending.title)
        safe_text = html.escape(pending.text)
        safe_scopes = html.escape(scopes)
        safe_effective = html.escape(pending.effective_at.isoformat())
        safe_impact = html.escape(impact_text)
        safe_fingerprint = html.escape(pending.proposal_fingerprint)
        safe_instance = html.escape(pending.proposal_instance_id)
        safe_url = html.escape(confirmation_url, quote=True)
        html_body = (
            "<h1>Dragback approval requested</h1>"
            f"<p><strong>Delivery:</strong> {html.escape(mode_label)}</p>"
            f"<p><strong>Decision:</strong> {safe_title}</p>"
            f"<p><strong>Proposed change:</strong> {safe_text}</p>"
            f"<p><strong>Affected scopes:</strong> {safe_scopes}</p>"
            f"<p><strong>Effective at:</strong> {safe_effective}</p>"
            f"<p><strong>Blast radius:</strong> {safe_impact}</p>"
            f"<p><strong>Proposal:</strong> {safe_fingerprint}<br>"
            f"<strong>Instance:</strong> {safe_instance}</p>"
            "<p>Opening the link does not approve anything. The confirmation "
            "page requires an explicit Approve action, and Hexclave permission "
            "is checked again at approval time.</p>"
            f'<p><a href="{safe_url}" rel="noreferrer">Review approval</a></p>'
        )
        message = ApprovalEmail(
            recipient_email=recipient_email,
            sender_email=self._sender_email,
            subject=f"Dragback approval: {pending.title}"[:500],
            text=text,
            html=html_body,
            delivery_mode=mode,
        )
        receipt = self._sender.send(message)
        if receipt.delivery_mode is not mode:
            raise EmailNotificationError(
                "The email sender returned a different delivery mode."
            )
        return EmailNotificationResult(
            receipt=receipt,
            token_id=claims.token_id,
            proposal_fingerprint=claims.proposal_fingerprint,
            proposal_instance_id=claims.proposal_instance_id,
            expires_at=claims.expires_at,
        )


def approval_confirmation_html(
    *,
    channel: ApprovalChannel,
) -> str:
    """Render a generic fragment handoff; the server never receives the token."""

    if channel not in {ApprovalChannel.PUSH, ApprovalChannel.EMAIL}:
        raise ApprovalLinkConfigurationError(
            "Approval confirmation requires the push or email channel."
        )
    action = f"/approvals/{channel.value}/redeem"
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        "<title>Confirm Dragback approval</title></head><body>"
        "<h1>Confirm Dragback approval</h1>"
        "<p>This action will recheck your current Hexclave permission and "
        "apply only the exact proposal instance shown in your notification.</p>"
        f'<form method="post" action="{html.escape(action, quote=True)}">'
        '<input id="approval-token" type="hidden" name="token" disabled>'
        '<button id="approve-button" type="submit" disabled>'
        "Approve this exact proposal</button></form>"
        "<script>(()=>{const p=new URLSearchParams("
        "window.location.hash.slice(1));const t=p.get('token');"
        "history.replaceState(null,'',window.location.pathname);"
        "if(!t)return;const i=document.getElementById('approval-token');"
        "const b=document.getElementById('approve-button');"
        "i.value=t;i.disabled=false;b.disabled=false;})();</script>"
        "</body></html>"
    )


def _safe_public_base_url(value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.query or parsed.fragment or not parsed.netloc:
        raise ApprovalLinkConfigurationError(
            "The approval public base URL is invalid."
        )
    is_loopback_http = (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )
    if parsed.scheme != "https" and not is_loopback_http:
        raise ApprovalLinkConfigurationError(
            "The approval public base URL must use HTTPS outside loopback."
        )
    return normalized


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )
