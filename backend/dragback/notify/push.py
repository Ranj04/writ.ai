"""ntfy and Pushover delivery for proposal-bound phone approvals."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlencode, urlparse

import httpx
from dragback.intake.approval import ApprovalChannel, PendingApproval
from dragback.notify.email import (
    ApprovalDeliveryProvider,
    ApprovalImpact,
    ApprovalLinkSigner,
    NotificationDeliveryMode,
    build_approval_link_urls,
    is_high_entropy_ntfy_topic,
)
from pydantic import BaseModel, ConfigDict, Field, SecretStr

_PUSH_MESSAGE_MAX_LENGTH = 1_024


class PushNotificationError(RuntimeError):
    """A phone approval notification could not be safely delivered."""


class PushProvider(StrEnum):
    NTFY = "ntfy"
    PUSHOVER = "pushover"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class PushApprovalDispatchRequest(_FrozenModel):
    approver_user_id: str = Field(min_length=1, max_length=255)
    provider: Literal["ntfy", "pushover"] = "ntfy"


class PushApprovalMessage(_FrozenModel):
    title: str = Field(min_length=1, max_length=250)
    text: str = Field(min_length=1, max_length=_PUSH_MESSAGE_MAX_LENGTH)
    decision_id: str = Field(min_length=1, max_length=160)
    confirmation_url: SecretStr
    redemption_url: SecretStr
    redemption_body: SecretStr
    delivery_mode: NotificationDeliveryMode


class PushDeliveryReceipt(_FrozenModel):
    provider: Literal["ntfy", "pushover"]
    message_id: str = Field(min_length=1, max_length=255)
    delivery_mode: NotificationDeliveryMode


class ApprovalPushSender(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def delivery_mode(self) -> NotificationDeliveryMode: ...

    @property
    def recipient_binding_ref(self) -> str: ...

    def send(self, message: PushApprovalMessage) -> PushDeliveryReceipt: ...


class NtfyPushSender:
    """One real ntfy POST with review and explicit-POST approval actions."""

    provider: Literal["ntfy"] = "ntfy"
    delivery_mode: Literal[NotificationDeliveryMode.LIVE] = (
        NotificationDeliveryMode.LIVE
    )

    def __init__(
        self,
        *,
        server_url: str,
        topic: str,
        access_token: str,
        private_topic_confirmed: bool,
        timeout_seconds: float = 5,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._server_url = _safe_https_url(
            server_url,
            label="ntfy server",
        )
        self._topic = topic.strip()
        if (
            not self._topic
            or len(self._topic) > 256
            or "/" in self._topic
            or any(character.isspace() for character in self._topic)
            or not is_high_entropy_ntfy_topic(self._topic)
        ):
            raise PushNotificationError(
                "The ntfy topic must be valid and high entropy."
            )
        self._access_token = access_token.strip()
        if not self._access_token:
            raise PushNotificationError(
                "An ntfy access token is required for approval delivery."
            )
        if not private_topic_confirmed:
            raise PushNotificationError(
                "NTFY_PRIVATE_TOPIC_CONFIRMED must attest that anonymous "
                "reads are denied by reservation or server ACL."
            )
        if timeout_seconds <= 0:
            raise PushNotificationError(
                "The ntfy timeout must be greater than zero."
            )
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client or httpx.Client()

    @property
    def recipient_binding_ref(self) -> str:
        return f"{self._server_url}/{self._topic}"

    def send(self, message: PushApprovalMessage) -> PushDeliveryReceipt:
        if message.delivery_mode is not self.delivery_mode:
            raise PushNotificationError(
                "The ntfy message has the wrong delivery mode."
            )
        confirmation_url = message.confirmation_url.get_secret_value()
        redemption_url = message.redemption_url.get_secret_value()
        redemption_body = quote(
            message.redemption_body.get_secret_value(),
            safe="",
        )
        actions = (
            "http, Approve exact proposal, "
            f"{redemption_url}, method=POST, body={redemption_body}, "
            "headers.Content-Type=application/x-www-form-urlencoded, "
            "clear=true; "
            f"view, Review details, {confirmation_url}, clear=false"
        )
        try:
            response = self._http_client.post(
                f"{self._server_url}/{quote(self._topic, safe='')}",
                content=message.text.encode("utf-8"),
                headers={
                    "Title": _header_value(message.title),
                    "Priority": "high",
                    "Tags": "warning,lock",
                    "Actions": actions,
                    "Content-Type": "text/plain; charset=utf-8",
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._access_token}",
                },
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PushNotificationError(
                "The ntfy approval notification could not be delivered."
            ) from exc
        message_id = payload.get("id") if isinstance(payload, dict) else None
        returned_topic = (
            payload.get("topic") if isinstance(payload, dict) else None
        )
        if (
            not isinstance(message_id, str)
            or not message_id.strip()
            or returned_topic != self._topic
        ):
            raise PushNotificationError(
                "ntfy returned an invalid delivery receipt."
            )
        return PushDeliveryReceipt(
            provider=self.provider,
            message_id=message_id,
            delivery_mode=self.delivery_mode,
        )


class PushoverPushSender:
    """Pushover delivery; its supplementary URL opens the POST confirmation."""

    API_URL = "https://api.pushover.net/1/messages.json"
    provider: Literal["pushover"] = "pushover"
    delivery_mode: Literal[NotificationDeliveryMode.LIVE] = (
        NotificationDeliveryMode.LIVE
    )

    def __init__(
        self,
        *,
        app_token: str,
        user_key: str,
        timeout_seconds: float = 5,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not app_token.strip() or not user_key.strip():
            raise PushNotificationError(
                "The Pushover application token and user key are required."
            )
        if timeout_seconds <= 0:
            raise PushNotificationError(
                "The Pushover timeout must be greater than zero."
            )
        self._app_token = app_token.strip()
        self._user_key = user_key.strip()
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client or httpx.Client()

    @property
    def recipient_binding_ref(self) -> str:
        # The secret key is never placed in claims; only its digest is signed.
        return self._user_key

    def send(self, message: PushApprovalMessage) -> PushDeliveryReceipt:
        if message.delivery_mode is not self.delivery_mode:
            raise PushNotificationError(
                "The Pushover message has the wrong delivery mode."
            )
        try:
            response = self._http_client.post(
                self.API_URL,
                data={
                    "token": self._app_token,
                    "user": self._user_key,
                    "title": message.title,
                    "message": message.text,
                    "priority": "0",
                    "url": message.confirmation_url.get_secret_value(),
                    "url_title": "Review and approve",
                },
                headers={"Accept": "application/json"},
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PushNotificationError(
                "The Pushover approval notification could not be delivered."
            ) from exc
        status = payload.get("status") if isinstance(payload, dict) else None
        request_id = (
            payload.get("request") if isinstance(payload, dict) else None
        )
        if (
            status != 1
            or not isinstance(request_id, str)
            or not request_id.strip()
        ):
            raise PushNotificationError(
                "Pushover returned an invalid delivery receipt."
            )
        return PushDeliveryReceipt(
            provider=self.provider,
            message_id=request_id,
            delivery_mode=self.delivery_mode,
        )


class PushNotificationResult(_FrozenModel):
    receipt: PushDeliveryReceipt
    token_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    proposal_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    proposal_instance_id: str = Field(min_length=1, max_length=255)
    expires_at: datetime


class PushApprovalNotifier:
    """Send a read-only phone notification; only its action may approve."""

    def __init__(
        self,
        *,
        signer: ApprovalLinkSigner,
        public_base_url: str,
        sender: ApprovalPushSender,
    ) -> None:
        self._signer = signer
        self._public_base_url = public_base_url
        self._sender = sender

    def notify(
        self,
        *,
        pending: PendingApproval,
        approver_user_id: str,
        impact: ApprovalImpact = ApprovalImpact(),
    ) -> PushNotificationResult:
        token = self._signer.issue(
            pending=pending,
            approver_user_id=approver_user_id,
            channel=ApprovalChannel.PUSH,
            delivery_provider=ApprovalDeliveryProvider(
                self._sender.provider
            ),
            recipient_ref=self._sender.recipient_binding_ref,
        )
        claims = self._signer.decode(
            token,
            expected_channel=ApprovalChannel.PUSH,
        )
        urls = build_approval_link_urls(
            public_base_url=self._public_base_url,
            channel=ApprovalChannel.PUSH,
            token=token,
        )
        mode = self._sender.delivery_mode
        mode_label = (
            "LIVE external push"
            if mode is NotificationDeliveryMode.LIVE
            else "SIMULATED push; no external delivery"
        )
        scopes = ", ".join(sorted(pending.affected_scopes))
        text = (
            f"{mode_label}. {pending.text} Scopes: {scopes}. "
            f"Blast radius: {impact.interrupted_count} of "
            f"{impact.total_count} active assignments. Tap Approve only after "
            "review; current Hexclave permission is checked again."
        )[:_PUSH_MESSAGE_MAX_LENGTH]
        message = PushApprovalMessage(
            title=f"Dragback approval: {pending.title}"[:250],
            text=text,
            decision_id=pending.decision_id,
            confirmation_url=urls.confirmation_url,
            redemption_url=urls.redemption_url,
            redemption_body=SecretStr(urlencode({"token": token})),
            delivery_mode=mode,
        )
        receipt = self._sender.send(message)
        if receipt.delivery_mode is not mode:
            raise PushNotificationError(
                "The push sender returned a different delivery mode."
            )
        if receipt.provider != self._sender.provider:
            raise PushNotificationError(
                "The push sender returned a different provider."
            )
        return PushNotificationResult(
            receipt=receipt,
            token_id=claims.token_id,
            proposal_fingerprint=claims.proposal_fingerprint,
            proposal_instance_id=claims.proposal_instance_id,
            expires_at=claims.expires_at,
        )


def _header_value(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def _safe_https_url(value: str, *, label: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlparse(normalized)
    is_loopback_http = (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )
    if (
        not parsed.netloc
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and not is_loopback_http)
    ):
        raise PushNotificationError(
            f"The {label} must use HTTPS outside loopback."
        )
    return normalized
