from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from writai.domain import Artifact
from writai.intake.decisions import (
    build_workspace_proposal,
    select_seeded_supersession_root,
)
from writai.intake.gate import (
    DeterministicIntakeGate,
    GateResult,
    IntakeGateDisposition,
)
from writai.llm.extractor import DecisionExtractor
from writai.workspaces.models import WorkspaceProposalRequest

CHANNEL_MESSAGE_SLUG = "SLACK_CHANNEL_MESSAGE_RECEIVED"
REACTION_ADDED_SLUG = "SLACK_MESSAGE_REACTION_ADDED"

_MESSAGE_FIELDS = frozenset({"channel", "team_id", "text", "ts", "user"})
_REACTION_FIELDS = frozenset(
    {
        "event_ts",
        "message_channel",
        "message_ts",
        "reaction",
        "team_id",
        "user",
    }
)


class SlackWebhookError(ValueError):
    """A signed delivery is not the expected, schema-checked Slack event."""


class ComposioTriggerType(Protocol):
    payload: Mapping[str, Any]


class ComposioTriggers(Protocol):
    def get_type(self, slug: str) -> ComposioTriggerType: ...

    def parse(
        self,
        *,
        body: str | bytes | Mapping[str, Any],
        headers: Mapping[str, Any],
        verify_secret: str,
    ) -> Mapping[str, Any]: ...


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class VerifiedSlackMessage(_FrozenModel):
    event_id: str = Field(min_length=1, max_length=255)
    connection_user_id: str = Field(min_length=1, max_length=255)
    author_user_id: str = Field(min_length=1, max_length=255)
    team_id: str = Field(min_length=1, max_length=255)
    channel_id: str = Field(min_length=1, max_length=255)
    message_ts: str = Field(min_length=1, max_length=64)
    delivered_at: datetime
    text: str = Field(min_length=1, max_length=40_000)
    source_ref: str = Field(min_length=1, max_length=1_000)


class VerifiedSlackReaction(_FrozenModel):
    event_id: str = Field(min_length=1, max_length=255)
    connection_user_id: str = Field(min_length=1, max_length=255)
    reacting_user_id: str = Field(min_length=1, max_length=255)
    team_id: str = Field(min_length=1, max_length=255)
    channel_id: str = Field(min_length=1, max_length=255)
    message_ts: str = Field(min_length=1, max_length=64)
    reaction: str = Field(min_length=1, max_length=255)
    delivered_at: datetime
    evidence_ref: str = Field(min_length=1, max_length=1_000)


class SlackMessageVerifier(Protocol):
    def parse_message(
        self,
        *,
        body: str | bytes,
        headers: Mapping[str, Any],
    ) -> VerifiedSlackMessage: ...


class ComposioSlackWebhookVerifier:
    """Verify raw Composio webhooks before exposing normalized Slack fields.

    `get_type()` is deliberately called during construction and its payload
    schema is checked before `parse()` can run. The Composio import remains in
    the live factory so the deterministic demo does not require the optional
    dependency.
    """

    def __init__(
        self,
        *,
        triggers: ComposioTriggers,
        webhook_secret: str,
    ) -> None:
        if not webhook_secret:
            raise SlackWebhookError("COMPOSIO_WEBHOOK_SECRET must be configured.")
        self._triggers = triggers
        self._secret = webhook_secret
        self._validate_trigger_schema(CHANNEL_MESSAGE_SLUG, _MESSAGE_FIELDS)
        self._validate_trigger_schema(REACTION_ADDED_SLUG, _REACTION_FIELDS)

    @classmethod
    def live(
        cls,
        *,
        api_key: str,
        webhook_secret: str,
    ) -> ComposioSlackWebhookVerifier:
        if not api_key:
            raise SlackWebhookError("COMPOSIO_API_KEY must be configured.")
        try:
            from composio import Composio
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise SlackWebhookError(
                "Install writ.ai with the 'composio' extra for live Slack intake."
            ) from exc
        client = Composio(api_key=api_key, allow_tracking=False)
        return cls(
            triggers=cast(ComposioTriggers, client.triggers),
            webhook_secret=webhook_secret,
        )

    def _validate_trigger_schema(
        self,
        slug: str,
        expected_fields: frozenset[str],
    ) -> None:
        trigger_type = self._triggers.get_type(slug)
        schema = trigger_type.payload
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            raise SlackWebhookError(
                f"Composio trigger {slug} returned no payload property schema."
            )
        missing = expected_fields - {
            key for key in properties if isinstance(key, str)
        }
        if missing:
            raise SlackWebhookError(
                f"Composio trigger {slug} is missing expected payload fields: "
                + ", ".join(sorted(missing))
            )

    def _parse(
        self,
        *,
        body: str | bytes,
        headers: Mapping[str, Any],
        expected_slug: str,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], str]:
        try:
            result = self._triggers.parse(
                body=body,
                headers=headers,
                verify_secret=self._secret,
            )
        except Exception as exc:
            # Vendor exception details are intentionally not reflected to callers.
            raise SlackWebhookError(
                "Composio webhook signature or payload verification failed."
            ) from exc
        delivery_id = _webhook_delivery_id(headers)
        version = result.get("version")
        version_value = getattr(version, "value", version)
        if version_value != "V3":
            raise SlackWebhookError("Only Composio V3 webhook envelopes are accepted.")
        event = result.get("payload")
        if not isinstance(event, Mapping):
            raise SlackWebhookError("Composio returned no normalized trigger event.")
        if event.get("trigger_slug") != expected_slug:
            raise SlackWebhookError("The signed webhook has the wrong trigger type.")
        toolkit = event.get("toolkit_slug")
        if not isinstance(toolkit, str) or toolkit.casefold() != "slack":
            raise SlackWebhookError("The signed webhook is not a Slack event.")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise SlackWebhookError("The Slack trigger payload is missing.")
        return event, payload, delivery_id

    def parse_message(
        self,
        *,
        body: str | bytes,
        headers: Mapping[str, Any],
    ) -> VerifiedSlackMessage:
        event, payload, delivery_id = self._parse(
            body=body,
            headers=headers,
            expected_slug=CHANNEL_MESSAGE_SLUG,
        )
        values = _require_strings(payload, _MESSAGE_FIELDS)
        connection_user_id = _connection_user_id(event)
        delivered_at = _slack_timestamp(values["ts"])
        source_ref = (
            f"slack://{values['team_id']}/{values['channel']}/{values['ts']}"
        )
        return VerifiedSlackMessage(
            event_id=delivery_id,
            connection_user_id=connection_user_id,
            author_user_id=values["user"],
            team_id=values["team_id"],
            channel_id=values["channel"],
            message_ts=values["ts"],
            delivered_at=delivered_at,
            text=values["text"],
            source_ref=source_ref,
        )

    def parse_reaction(
        self,
        *,
        body: str | bytes,
        headers: Mapping[str, Any],
    ) -> VerifiedSlackReaction:
        event, payload, delivery_id = self._parse(
            body=body,
            headers=headers,
            expected_slug=REACTION_ADDED_SLUG,
        )
        values = _require_strings(payload, _REACTION_FIELDS)
        connection_user_id = _connection_user_id(event)
        delivered_at = _slack_timestamp(values["event_ts"])
        evidence_ref = (
            f"slack://{values['team_id']}/{values['message_channel']}/"
            f"{values['message_ts']}#reaction-{values['event_ts']}"
        )
        return VerifiedSlackReaction(
            event_id=delivery_id,
            connection_user_id=connection_user_id,
            reacting_user_id=values["user"],
            team_id=values["team_id"],
            channel_id=values["message_channel"],
            message_ts=values["message_ts"],
            reaction=values["reaction"],
            delivered_at=delivered_at,
            evidence_ref=evidence_ref,
        )


@dataclass(frozen=True)
class SlackIntakeOutcome:
    """One verified delivery routed to review or parked; never an authority verdict."""

    workspace_id: str
    message: VerifiedSlackMessage
    gate: GateResult
    proposal: WorkspaceProposalRequest | None


class SlackDecisionIntake:
    """Signed Slack source → untrusted extraction → deterministic review gate."""

    def __init__(
        self,
        *,
        verifier: SlackMessageVerifier,
        extractor: DecisionExtractor,
        gate: DeterministicIntakeGate,
    ) -> None:
        self._verifier = verifier
        self._extractor = extractor
        self._gate = gate

    def ingest(
        self,
        *,
        workspace_id: str,
        body: str | bytes,
        headers: Mapping[str, Any],
        current_decisions: list[Artifact],
        authority_user_id: str,
        supersession_root_id: str,
    ) -> SlackIntakeOutcome:
        message = self._verifier.parse_message(body=body, headers=headers)
        return self.ingest_verified(
            workspace_id=workspace_id,
            message=message,
            current_decisions=current_decisions,
            authority_user_id=authority_user_id,
            supersession_root_id=supersession_root_id,
        )

    def ingest_verified(
        self,
        *,
        workspace_id: str,
        message: VerifiedSlackMessage,
        current_decisions: list[Artifact],
        authority_user_id: str,
        supersession_root_id: str,
    ) -> SlackIntakeOutcome:
        """Process a message whose signature and workspace binding are verified."""

        candidate = self._extractor.extract(
            message.text,
            scope_vocabulary=self._gate.known_scopes,
        )
        affected_scopes = set(candidate.mutation.affected_scopes)
        gate = self._gate.evaluate(
            author_id=authority_user_id,
            affected_scopes=affected_scopes,
            confidence=candidate.mutation.decision.confidence,
        )
        if gate.disposition is IntakeGateDisposition.PARKED:
            return SlackIntakeOutcome(
                workspace_id=workspace_id,
                message=message,
                gate=gate,
                proposal=None,
            )
        if gate.permission_id is None:  # defensive invariant
            raise SlackWebhookError(
                "The intake gate returned no authority permission."
            )
        superseded = select_seeded_supersession_root(
            decisions=current_decisions,
            affected_scopes=affected_scopes,
            root_decision_id=supersession_root_id,
        )
        proposal = build_workspace_proposal(
            candidate=candidate,
            raw_text=message.text,
            source_ref=message.source_ref,
            author_user_id=message.author_user_id,
            authority_user_id=authority_user_id,
            delivered_at=message.delivered_at,
            superseded_decision=superseded,
            authority_permission_id=gate.permission_id,
        )
        return SlackIntakeOutcome(
            workspace_id=workspace_id,
            message=message,
            gate=gate,
            proposal=proposal,
        )


def _require_strings(
    payload: Mapping[str, Any],
    fields: frozenset[str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in sorted(fields):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SlackWebhookError(
                f"The verified Slack payload is missing required field {field!r}."
            )
        result[field] = value.strip()
    return result


def _webhook_delivery_id(headers: Mapping[str, Any]) -> str:
    for key, value in headers.items():
        if (
            isinstance(key, str)
            and key.casefold() == "webhook-id"
            and isinstance(value, str)
            and value.strip()
        ):
            return value.strip()
    raise SlackWebhookError("The verified Composio delivery has no webhook ID.")


def _connection_user_id(event: Mapping[str, Any]) -> str:
    value = event.get("user_id")
    if (
        not isinstance(value, str)
        or not value.strip()
        or value.strip().casefold() == "default"
    ):
        raise SlackWebhookError(
            "The Composio connection must use an explicit non-default user ID."
        )
    return value.strip()


def _slack_timestamp(value: str) -> datetime:
    try:
        seconds = Decimal(value)
    except InvalidOperation as exc:
        raise SlackWebhookError("Slack supplied an invalid event timestamp.") from exc
    if not seconds.is_finite() or seconds <= 0:
        raise SlackWebhookError("Slack supplied an invalid event timestamp.")
    return datetime.fromtimestamp(float(seconds), tz=UTC)
