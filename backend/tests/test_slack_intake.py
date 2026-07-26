from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from writai.intake.slack import (
    CHANNEL_MESSAGE_SLUG,
    REACTION_ADDED_SLUG,
    ComposioSlackWebhookVerifier,
    SlackWebhookError,
)

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "whsec_writai_test"


class FixtureSchemaTriggers:
    def __init__(self, parsed: dict[str, object]) -> None:
        self._schemas = json.loads(
            (FIXTURES / "composio_slack_schemas.json").read_text()
        )
        self.parsed = parsed
        self.calls: list[tuple[str, object]] = []

    def get_type(self, slug: str):
        self.calls.append(("get_type", slug))
        return SimpleNamespace(payload=self._schemas[slug])

    def parse(self, *, body, headers, verify_secret):
        self.calls.append(
            ("parse", {"body": body, "headers": headers, "secret": verify_secret})
        )
        return self.parsed


class CanonicalHmacTriggers(FixtureSchemaTriggers):
    """Non-optional Composio/Standard Webhooks verification vector."""

    def __init__(
        self,
        parsed: dict[str, object],
        *,
        now: int,
        tolerance_seconds: int = 300,
    ) -> None:
        super().__init__(parsed)
        self._now = now
        self._tolerance_seconds = tolerance_seconds

    def parse(self, *, body, headers, verify_secret):
        normalized_headers = {
            str(key).casefold(): str(value) for key, value in headers.items()
        }
        webhook_id = normalized_headers["webhook-id"]
        timestamp = normalized_headers["webhook-timestamp"]
        signature = normalized_headers["webhook-signature"]
        if abs(self._now - int(timestamp)) > self._tolerance_seconds:
            raise ValueError("timestamp outside tolerance")
        raw = body if isinstance(body, bytes) else str(body).encode()
        signed = b".".join(
            (webhook_id.encode(), timestamp.encode(), raw)
        )
        expected = base64.b64encode(
            hmac.new(
                verify_secret.encode(),
                signed,
                hashlib.sha256,
            ).digest()
        ).decode()
        supplied = signature.removeprefix("v1,")
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("signature mismatch")
        return super().parse(
            body=body,
            headers=headers,
            verify_secret=verify_secret,
        )


def _message_event(
    *,
    slug: str = CHANNEL_MESSAGE_SLUG,
    connection_user_id: str = "writai-user-emanuel",
) -> dict[str, object]:
    return {
        "version": "V3",
        "payload": {
            "id": "trigger_fixture_001",
            "uuid": "trigger_fixture_001",
            "user_id": connection_user_id,
            "trigger_slug": slug,
            "toolkit_slug": "SLACK",
            "payload": {
                "channel": "C-COMPLIANCE",
                "team_id": "T-WRITAI",
                "text": "Approved: exports must be admin-only, effective immediately.",
                "ts": "1784952300.000001",
                "user": "U-COMPLIANCE",
            },
        },
    }


def test_get_type_is_checked_before_any_webhook_is_parsed() -> None:
    triggers = FixtureSchemaTriggers(_message_event())
    verifier = ComposioSlackWebhookVerifier(
        triggers=triggers,
        webhook_secret=SECRET,
    )
    message = verifier.parse_message(
        body=b"{}",
        headers={"webhook-id": "msg-fixture-1"},
    )

    assert triggers.calls[:2] == [
        ("get_type", CHANNEL_MESSAGE_SLUG),
        ("get_type", REACTION_ADDED_SLUG),
    ]
    assert triggers.calls[2][0] == "parse"
    assert message.author_user_id == "U-COMPLIANCE"
    assert message.source_ref == (
        "slack://T-WRITAI/C-COMPLIANCE/1784952300.000001"
    )
    assert message.delivered_at.tzinfo is not None
    assert message.event_id == "msg-fixture-1"


def test_signed_unrelated_composio_event_is_rejected() -> None:
    triggers = FixtureSchemaTriggers(_message_event(slug="GITHUB_COMMIT_EVENT"))
    verifier = ComposioSlackWebhookVerifier(
        triggers=triggers,
        webhook_secret=SECRET,
    )
    with pytest.raises(SlackWebhookError, match="wrong trigger"):
        verifier.parse_message(
            body=b"{}",
            headers={"webhook-id": "msg-unrelated"},
        )


def test_default_composio_user_id_is_rejected() -> None:
    triggers = FixtureSchemaTriggers(
        _message_event(connection_user_id="default")
    )
    verifier = ComposioSlackWebhookVerifier(
        triggers=triggers,
        webhook_secret=SECRET,
    )
    with pytest.raises(SlackWebhookError, match="non-default"):
        verifier.parse_message(
            body=b"{}",
            headers={"webhook-id": "msg-default-user"},
        )


def test_verified_delivery_requires_unique_webhook_id_header() -> None:
    verifier = ComposioSlackWebhookVerifier(
        triggers=FixtureSchemaTriggers(_message_event()),
        webhook_secret=SECRET,
    )

    with pytest.raises(SlackWebhookError, match="no webhook ID"):
        verifier.parse_message(body=b"{}", headers={})


def _canonical_headers(
    *,
    body: bytes,
    webhook_id: str,
    timestamp: int,
) -> dict[str, str]:
    signed = b".".join(
        (webhook_id.encode(), str(timestamp).encode(), body)
    )
    signature = base64.b64encode(
        hmac.new(SECRET.encode(), signed, hashlib.sha256).digest()
    ).decode()
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": f"v1,{signature}",
    }


def test_non_optional_canonical_signature_vector_forwards_raw_body() -> None:
    now = 1_784_952_300
    raw = b'{"signed":"raw bytes"}'
    verifier = ComposioSlackWebhookVerifier(
        triggers=CanonicalHmacTriggers(_message_event(), now=now),
        webhook_secret=SECRET,
    )

    message = verifier.parse_message(
        body=raw,
        headers=_canonical_headers(
            body=raw,
            webhook_id="msg-canonical-1",
            timestamp=now,
        ),
    )

    assert message.event_id == "msg-canonical-1"


@pytest.mark.parametrize("timestamp_offset", [-301, 301])
def test_non_optional_canonical_signature_vector_rejects_stale_and_future(
    timestamp_offset: int,
) -> None:
    now = 1_784_952_300
    raw = b'{"signed":"raw bytes"}'
    verifier = ComposioSlackWebhookVerifier(
        triggers=CanonicalHmacTriggers(_message_event(), now=now),
        webhook_secret=SECRET,
    )

    with pytest.raises(SlackWebhookError, match="verification failed"):
        verifier.parse_message(
            body=raw,
            headers=_canonical_headers(
                body=raw,
                webhook_id="msg-outside-window",
                timestamp=now + timestamp_offset,
            ),
        )


def test_real_composio_parser_verifies_stored_v3_fixture_signature() -> None:
    composio = pytest.importorskip("composio")
    raw = (FIXTURES / "composio_slack_message_v3.json").read_bytes()
    timestamp = str(int(time.time()))
    webhook_id = "msg_fixture_001"
    signed = b".".join(
        (webhook_id.encode(), timestamp.encode(), raw)
    )
    signature = base64.b64encode(
        hmac.new(SECRET.encode(), signed, hashlib.sha256).digest()
    ).decode()
    sdk_triggers = composio.Composio(
        api_key="test",
        allow_tracking=False,
    ).triggers
    schemas = json.loads(
        (FIXTURES / "composio_slack_schemas.json").read_text()
    )

    class RealParseCapturedSchema:
        def get_type(self, slug: str):
            return SimpleNamespace(payload=schemas[slug])

        def parse(self, *, body, headers, verify_secret):
            return sdk_triggers.parse(
                body=body,
                headers=headers,
                verify_secret=verify_secret,
            )

    verifier = ComposioSlackWebhookVerifier(
        triggers=RealParseCapturedSchema(),
        webhook_secret=SECRET,
    )
    message = verifier.parse_message(
        body=raw,
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": f"v1,{signature}",
        },
    )
    assert message.event_id == webhook_id
    assert message.text == (
        "Approved: exports must be admin-only, effective immediately."
    )

    with pytest.raises(SlackWebhookError, match="verification failed"):
        verifier.parse_message(
            body=raw,
            headers={
                "webhook-id": webhook_id,
                "webhook-timestamp": timestamp,
                "webhook-signature": "v1,invalid",
            },
        )
