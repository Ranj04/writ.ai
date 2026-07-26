"""Slack approval-preview notifications.

This module deliberately starts after provider payload normalization. The
notification is a read-only preview built through ``SupervisorInterruptPort``;
signed reactions are correlated to that preview and routed through the shared
permission-checked ``approve()`` path.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol, cast

from writai.intake.approval import (
    ApprovalChannel,
    ApprovalResult,
    PendingApproval,
)
from writai.intake.slack import VerifiedSlackReaction
from writai.supervisor_contract import (
    InterruptRequest,
    InterruptResult,
    SupervisorInterruptPort,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

_MAX_MESSAGE_LENGTH = 10_000
_MAX_LISTED_ASSIGNMENTS = 20


class SlackNotificationError(RuntimeError):
    """The normalized notification could not be safely composed or delivered."""


class SlackDeliveryMode(StrEnum):
    """Whether the injected sender reaches Slack or only records a simulation."""

    SIMULATED = "simulated"
    LIVE = "live"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class SlackThreadReference(_FrozenModel):
    """Provider-neutral identifiers for an existing Slack message thread."""

    channel_id: str = Field(min_length=1, max_length=255)
    parent_message_id: str = Field(min_length=1, max_length=255)


class SlackRequirementDelta(_FrozenModel):
    """A display-safe, normalized requirement change for one affected scope."""

    scope: str = Field(min_length=1, max_length=255)
    previous_summary: str | None = Field(default=None, max_length=500)
    proposed_summary: str = Field(min_length=1, max_length=500)


class SlackDecisionNotification(_FrozenModel):
    """Normalized input accepted from an intake/approval orchestration layer."""

    team_id: str = Field(min_length=1, max_length=255)
    thread: SlackThreadReference
    pending: PendingApproval
    interrupt_request: InterruptRequest
    source_author: str = Field(min_length=1, max_length=255)
    source_excerpt: str = Field(min_length=1, max_length=2_000)
    effective_at: datetime
    requirement_deltas: tuple[SlackRequirementDelta, ...] = Field(
        min_length=1,
        max_length=20,
    )
    assignment_labels: dict[str, str] = Field(
        default_factory=dict,
        max_length=100,
    )
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def validate_normalized_decision(self) -> SlackDecisionNotification:
        if self.effective_at.utcoffset() is None:
            raise ValueError("effective_at must be timezone-aware")
        if not self.interrupt_request.affected_scopes:
            raise ValueError("the interrupt preview must carry an affected scope")
        if (
            self.pending.workspace_id != self.interrupt_request.workspace_id
            or self.pending.decision_id
            != self.interrupt_request.decision_id
            or self.pending.affected_scopes
            != self.interrupt_request.affected_scopes
            or self.pending.effective_at != self.effective_at
        ):
            raise ValueError(
                "the notification must match one immutable pending approval"
            )

        delta_scopes = [delta.scope for delta in self.requirement_deltas]
        if len(delta_scopes) != len(set(delta_scopes)):
            raise ValueError("requirement_deltas must contain each scope once")
        if set(delta_scopes) != set(self.interrupt_request.affected_scopes):
            raise ValueError(
                "requirement delta scopes must exactly match affected scopes"
            )
        if any(not reference.strip() for reference in self.evidence_refs):
            raise ValueError("evidence references must not be blank")
        if any(
            not assignment_id.strip() or not label.strip()
            for assignment_id, label in self.assignment_labels.items()
        ):
            raise ValueError("assignment labels must not be blank")
        return self


class SlackBlastRadius(_FrozenModel):
    """The immutable, non-mutating result of an interrupt preview."""

    interrupted_assignment_ids: tuple[str, ...] = ()
    preserved_assignment_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_partition(self) -> SlackBlastRadius:
        interrupted = self.interrupted_assignment_ids
        preserved = self.preserved_assignment_ids
        if len(interrupted) != len(set(interrupted)):
            raise ValueError("interrupt preview returned duplicate interrupted IDs")
        if len(preserved) != len(set(preserved)):
            raise ValueError("interrupt preview returned duplicate preserved IDs")
        overlap = set(interrupted) & set(preserved)
        if overlap:
            raise ValueError(
                "interrupt preview returned assignments in both partitions: "
                + ", ".join(sorted(overlap))
            )
        return self

    @property
    def interrupted_count(self) -> int:
        return len(self.interrupted_assignment_ids)

    @property
    def preserved_count(self) -> int:
        return len(self.preserved_assignment_ids)

    @property
    def total_assignment_count(self) -> int:
        return self.interrupted_count + self.preserved_count

    @classmethod
    def from_result(cls, result: InterruptResult) -> SlackBlastRadius:
        return cls(
            interrupted_assignment_ids=tuple(
                sorted(result.interrupted_assignment_ids)
            ),
            preserved_assignment_ids=tuple(
                sorted(result.preserved_assignment_ids)
            ),
        )


class SlackThreadMessage(_FrozenModel):
    """Normalized outbound message; a sender translates it to its provider SDK."""

    thread: SlackThreadReference
    decision_id: str = Field(min_length=1, max_length=255)
    delivery_mode: SlackDeliveryMode
    text: str = Field(min_length=1, max_length=_MAX_MESSAGE_LENGTH)
    mentions_enabled: Literal[False] = False


class SlackDeliveryReceipt(_FrozenModel):
    """Normalized sender receipt with no Composio-specific response fields."""

    thread: SlackThreadReference
    message_id: str = Field(min_length=1, max_length=255)
    delivery_mode: SlackDeliveryMode


class SlackNotificationResult(_FrozenModel):
    preview: SlackBlastRadius
    message: SlackThreadMessage
    receipt: SlackDeliveryReceipt


class SlackThreadSender(Protocol):
    """Injected provider adapter.

    Implementations must honor ``mentions_enabled=False`` and must accurately
    declare whether they send externally through ``delivery_mode``.
    """

    @property
    def delivery_mode(self) -> SlackDeliveryMode: ...

    def send_thread_message(
        self,
        message: SlackThreadMessage,
    ) -> SlackDeliveryReceipt: ...


class ComposioTools(Protocol):
    def execute(
        self,
        slug: str,
        *,
        arguments: Mapping[str, Any],
        user_id: str,
        connected_account_id: str,
        version: str,
    ) -> Mapping[str, Any]: ...


class ComposioSlackThreadSender:
    """Pinned, non-retrying live adapter for Slack thread delivery."""

    TOOL_SLUG = "SLACK_SEND_MESSAGE"
    TOOLKIT_VERSION = "20260702_00"
    delivery_mode: Literal[SlackDeliveryMode.LIVE] = SlackDeliveryMode.LIVE

    def __init__(
        self,
        *,
        tools: ComposioTools,
        composio_user_id: str,
        connected_account_id: str,
    ) -> None:
        self._tools = tools
        self._composio_user_id = composio_user_id.strip()
        self._connected_account_id = connected_account_id.strip()
        if not self._composio_user_id or not self._connected_account_id:
            raise SlackNotificationError(
                "Live Slack delivery requires explicit Composio user and "
                "connected-account IDs."
            )
        if self._composio_user_id.casefold() == "default":
            raise SlackNotificationError(
                "Live Slack delivery may not use the default Composio user."
            )

    @classmethod
    def live(
        cls,
        *,
        api_key: str,
        composio_user_id: str,
        connected_account_id: str,
    ) -> ComposioSlackThreadSender:
        if not api_key.strip():
            raise SlackNotificationError(
                "COMPOSIO_API_KEY must be configured for live Slack delivery."
            )
        try:
            from composio import Composio
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise SlackNotificationError(
                "Install writ.ai with the 'composio' extra for live Slack delivery."
            ) from exc
        client = Composio(api_key=api_key, allow_tracking=False)
        return cls(
            tools=cast(ComposioTools, client.tools),
            composio_user_id=composio_user_id,
            connected_account_id=connected_account_id,
        )

    def send_thread_message(
        self,
        message: SlackThreadMessage,
    ) -> SlackDeliveryReceipt:
        if message.delivery_mode is not SlackDeliveryMode.LIVE:
            raise SlackNotificationError(
                "The live Slack sender accepts only LIVE messages."
            )
        try:
            result = self._tools.execute(
                self.TOOL_SLUG,
                arguments={
                    "channel": message.thread.channel_id,
                    "markdown_text": message.text,
                    "thread_ts": message.thread.parent_message_id,
                    "reply_broadcast": False,
                },
                user_id=self._composio_user_id,
                connected_account_id=self._connected_account_id,
                version=self.TOOLKIT_VERSION,
            )
        except Exception as exc:
            # Sending is non-idempotent. Never retry a timeout or opaque error:
            # the provider may already have posted the message.
            raise SlackNotificationError(
                "Composio could not confirm the Slack thread delivery."
            ) from exc

        data = result.get("data") if isinstance(result, Mapping) else None
        error = result.get("error") if isinstance(result, Mapping) else None
        successful = (
            result.get("successful") if isinstance(result, Mapping) else None
        )
        if (
            successful is not True
            or error not in {None, ""}
            or not isinstance(data, Mapping)
            or data.get("ok") is not True
            or data.get("channel") != message.thread.channel_id
        ):
            raise SlackNotificationError(
                "Composio returned an unsuccessful Slack delivery receipt."
            )
        message_id = data.get("ts")
        if not isinstance(message_id, str) or not message_id.strip():
            raise SlackNotificationError(
                "Composio returned no Slack message timestamp."
            )
        return SlackDeliveryReceipt(
            thread=message.thread,
            message_id=message_id.strip(),
            delivery_mode=SlackDeliveryMode.LIVE,
        )


class SlackPendingApprovalResolver(Protocol):
    def resolve(
        self,
        *,
        team_id: str,
        channel_id: str,
        message_ts: str,
    ) -> PendingApproval | None: ...


class SlackApprovalThreadStore(SlackPendingApprovalResolver, Protocol):
    def register(
        self,
        *,
        team_id: str,
        channel_id: str,
        message_ts: str,
        pending: PendingApproval,
    ) -> None: ...

    def consume(
        self,
        *,
        team_id: str,
        channel_id: str,
        message_ts: str,
        event_id: str,
        pending: PendingApproval,
    ) -> bool: ...


class SlackReactionIdentityResolver(Protocol):
    """Resolve a signed Slack user through a provisioned identity binding."""

    def resolve_hexclave_user_id(
        self,
        *,
        pending: PendingApproval,
        reaction: VerifiedSlackReaction,
    ) -> str | None: ...


class SlackApprovalCoordinator(Protocol):
    """Permission-checked approval boundary, local or internal-service backed."""

    def approve(
        self,
        *,
        pending: PendingApproval,
        approver_user_id: str,
        channel: ApprovalChannel,
        evidence_ref: str,
    ) -> ApprovalResult: ...


class InMemorySlackApprovalThreads:
    """Bind one posted approval thread to one immutable pending Decision."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], PendingApproval] = {}
        self._consumed: dict[tuple[str, str, str], str] = {}
        self._lock = RLock()

    def register(
        self,
        *,
        team_id: str,
        channel_id: str,
        message_ts: str,
        pending: PendingApproval,
    ) -> None:
        key = (team_id.strip(), channel_id.strip(), message_ts.strip())
        if not all(key):
            raise SlackNotificationError(
                "Slack approval thread identifiers must not be blank."
            )
        with self._lock:
            if key in self._consumed:
                raise SlackNotificationError(
                    "The Slack approval thread has already been consumed."
                )
            previous = self._items.get(key)
            if previous is not None and previous != pending:
                raise SlackNotificationError(
                    "The Slack thread is already bound to another Decision."
                )
            self._items[key] = pending

    def resolve(
        self,
        *,
        team_id: str,
        channel_id: str,
        message_ts: str,
    ) -> PendingApproval | None:
        with self._lock:
            key = (team_id.strip(), channel_id.strip(), message_ts.strip())
            if key in self._consumed:
                return None
            return self._items.get(key)

    def consume(
        self,
        *,
        team_id: str,
        channel_id: str,
        message_ts: str,
        event_id: str,
        pending: PendingApproval,
    ) -> bool:
        key = (team_id.strip(), channel_id.strip(), message_ts.strip())
        normalized_event_id = event_id.strip()
        if not all(key) or not normalized_event_id:
            raise SlackNotificationError(
                "Slack reaction identifiers must not be blank."
            )
        with self._lock:
            consumed_by = self._consumed.get(key)
            if consumed_by is not None:
                return consumed_by == normalized_event_id
            if self._items.get(key) != pending:
                return False
            self._consumed[key] = normalized_event_id
            return True


class SlackApprovalThreadRecord(_FrozenModel):
    team_id: str = Field(min_length=1, max_length=255)
    channel_id: str = Field(min_length=1, max_length=255)
    message_ts: str = Field(min_length=1, max_length=64)
    pending: PendingApproval
    consumed_by_event_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
    )


class _SlackApprovalThreadDocument(BaseModel):
    schema_version: Literal[1] = 1
    threads: list[SlackApprovalThreadRecord] = Field(default_factory=list)


class JsonSlackApprovalThreads:
    """Atomic, restart-safe Slack thread correlation for one authority process."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = RLock()

    @staticmethod
    def _key(
        *,
        team_id: str,
        channel_id: str,
        message_ts: str,
    ) -> tuple[str, str, str]:
        key = (team_id.strip(), channel_id.strip(), message_ts.strip())
        if not all(key):
            raise SlackNotificationError(
                "Slack approval thread identifiers must not be blank."
            )
        return key

    def _read(self) -> _SlackApprovalThreadDocument:
        if not self.path.exists():
            return _SlackApprovalThreadDocument()
        try:
            return _SlackApprovalThreadDocument.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError) as exc:
            raise SlackNotificationError(
                "The Slack approval thread store is unreadable."
            ) from exc

    def _write(self, document: _SlackApprovalThreadDocument) -> None:
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
        finally:
            if temporary.exists():
                temporary.unlink()

    def register(
        self,
        *,
        team_id: str,
        channel_id: str,
        message_ts: str,
        pending: PendingApproval,
    ) -> None:
        key = self._key(
            team_id=team_id,
            channel_id=channel_id,
            message_ts=message_ts,
        )
        with self._lock:
            document = self._read()
            previous = next(
                (
                    item
                    for item in document.threads
                    if (item.team_id, item.channel_id, item.message_ts) == key
                ),
                None,
            )
            if previous is not None:
                if previous.pending == pending and (
                    previous.consumed_by_event_id is None
                ):
                    return
                raise SlackNotificationError(
                    "The Slack thread is already bound or consumed."
                )
            document.threads.append(
                SlackApprovalThreadRecord(
                    team_id=key[0],
                    channel_id=key[1],
                    message_ts=key[2],
                    pending=pending,
                )
            )
            self._write(document)

    def resolve(
        self,
        *,
        team_id: str,
        channel_id: str,
        message_ts: str,
    ) -> PendingApproval | None:
        key = self._key(
            team_id=team_id,
            channel_id=channel_id,
            message_ts=message_ts,
        )
        with self._lock:
            for item in self._read().threads:
                if (
                    (item.team_id, item.channel_id, item.message_ts) == key
                    and item.consumed_by_event_id is None
                ):
                    return item.pending
        return None

    def consume(
        self,
        *,
        team_id: str,
        channel_id: str,
        message_ts: str,
        event_id: str,
        pending: PendingApproval,
    ) -> bool:
        key = self._key(
            team_id=team_id,
            channel_id=channel_id,
            message_ts=message_ts,
        )
        normalized_event_id = event_id.strip()
        if not normalized_event_id:
            raise SlackNotificationError(
                "The signed Slack reaction event ID must not be blank."
            )
        with self._lock:
            document = self._read()
            for index, item in enumerate(document.threads):
                if (item.team_id, item.channel_id, item.message_ts) != key:
                    continue
                if item.consumed_by_event_id is not None:
                    return item.consumed_by_event_id == normalized_event_id
                if item.pending != pending:
                    return False
                document.threads[index] = item.model_copy(
                    update={"consumed_by_event_id": normalized_event_id}
                )
                self._write(document)
                return True
        return False


@dataclass(frozen=True)
class SlackReactionResult:
    handled: bool
    approval: ApprovalResult | None = None


class SlackReactionApprovalHandler:
    """Route only ✅ additions through the shared permission-checked approver."""

    APPROVE_REACTION = "white_check_mark"

    def __init__(
        self,
        *,
        pending_resolver: SlackApprovalThreadStore,
        identity_resolver: SlackReactionIdentityResolver,
        coordinator: SlackApprovalCoordinator,
    ) -> None:
        self._pending_resolver = pending_resolver
        self._identity_resolver = identity_resolver
        self._coordinator = coordinator
        self._lock = RLock()

    def handle(self, reaction: VerifiedSlackReaction) -> SlackReactionResult:
        if reaction.reaction != self.APPROVE_REACTION:
            return SlackReactionResult(handled=False)
        with self._lock:
            pending = self._pending_resolver.resolve(
                team_id=reaction.team_id,
                channel_id=reaction.channel_id,
                message_ts=reaction.message_ts,
            )
            if pending is None:
                return SlackReactionResult(handled=False)
            approver_user_id = (
                self._identity_resolver.resolve_hexclave_user_id(
                    pending=pending,
                    reaction=reaction,
                )
            )
            if approver_user_id is None:
                # Unmapped Slack identities are intentionally ignored. A signed
                # delivery authenticates Slack/Composio, not a Hexclave human.
                return SlackReactionResult(handled=True)
            approval = self._coordinator.approve(
                pending=pending,
                approver_user_id=approver_user_id,
                channel=ApprovalChannel.SLACK_REACTION,
                evidence_ref=reaction.evidence_ref,
            )
            if approval.approved and not self._pending_resolver.consume(
                team_id=reaction.team_id,
                channel_id=reaction.channel_id,
                message_ts=reaction.message_ts,
                event_id=reaction.event_id,
                pending=pending,
            ):
                raise SlackNotificationError(
                    "The approved Slack reaction lost its thread binding."
                )
            return SlackReactionResult(handled=True, approval=approval)


def preview_blast_radius(
    port: SupervisorInterruptPort,
    request: InterruptRequest,
) -> SlackBlastRadius:
    """Return the assignment partition without changing supervisor state."""

    return SlackBlastRadius.from_result(port.preview(request))


def compose_slack_thread_message(
    *,
    notification: SlackDecisionNotification,
    preview: SlackBlastRadius,
    delivery_mode: SlackDeliveryMode,
) -> SlackThreadMessage:
    """Compose a bounded, deterministic approval-preview thread message."""

    mode = SlackDeliveryMode(delivery_mode)
    mode_description = (
        "SIMULATED — no external Slack post"
        if mode is SlackDeliveryMode.SIMULATED
        else "LIVE — external Slack thread"
    )
    request = notification.interrupt_request
    scope_lines = [
        f"- {_escape_user_text(scope)}"
        for scope in sorted(request.affected_scopes)
    ]
    delta_lines = [
        _format_delta(delta)
        for delta in sorted(
            notification.requirement_deltas,
            key=lambda item: item.scope,
        )
    ]
    evidence_lines = (
        [
            f"- {_escape_user_text(reference)}"
            for reference in notification.evidence_refs
        ]
        or ["- (none supplied)"]
    )
    provenance = " -> ".join(
        _escape_user_text(item)
        for item in request.provenance_path
    ) or "(none supplied)"

    lines = [
        "writ.ai decision approval preview",
        f"Delivery mode: {mode_description}",
        f"Workspace: {_escape_user_text(request.workspace_id)}",
        f"Decision: {_escape_user_text(request.decision_id)}",
        f"Source author: {_escape_user_text(notification.source_author)}",
        f"Source message: {_escape_user_text(notification.source_excerpt)}",
        f"Effective at: {_format_timestamp(notification.effective_at)}",
        "",
        "Affected scopes:",
        *scope_lines,
        "",
        "Extracted requirement changes:",
        *delta_lines,
        "",
        (
            f"Blast radius: {preview.interrupted_count} of "
            f"{preview.total_assignment_count} active assignments would be interrupted."
        ),
        *_format_assignment_partition(
            "Would interrupt",
            preview.interrupted_assignment_ids,
            notification.assignment_labels,
        ),
        *_format_assignment_partition(
            "Would preserve",
            preview.preserved_assignment_ids,
            notification.assignment_labels,
        ),
        f"Interrupt reason: {_escape_user_text(request.interrupt_reason)}",
        f"Redirect instruction: {_escape_user_text(request.redirect_instruction)}",
        f"Provenance path: {provenance}",
        "",
        "Evidence references:",
        *evidence_lines,
        "",
        (
            "This is a read-only blast-radius preview. No assignment was "
            "interrupted and no decision was approved."
        ),
        (
            "A reaction is only an approval request. writ.ai must resolve the "
            "reacting user and verify permission at reaction time."
        ),
    ]
    text = "\n".join(lines)
    if len(text) > _MAX_MESSAGE_LENGTH:
        raise SlackNotificationError(
            "The Slack approval-preview message exceeds 10,000 characters."
        )
    return SlackThreadMessage(
        thread=notification.thread,
        decision_id=request.decision_id,
        delivery_mode=mode,
        text=text,
    )


class SlackBlastRadiusNotifier:
    """Preview and post a Slack thread; never applies or interrupts."""

    def __init__(
        self,
        *,
        interrupt_port: SupervisorInterruptPort,
        sender: SlackThreadSender,
        approval_threads: SlackApprovalThreadStore | None = None,
    ) -> None:
        self._interrupt_port = interrupt_port
        self._sender = sender
        self._approval_threads = approval_threads

    def notify(
        self,
        notification: SlackDecisionNotification,
    ) -> SlackNotificationResult:
        preview = preview_blast_radius(
            self._interrupt_port,
            notification.interrupt_request,
        )
        message = compose_slack_thread_message(
            notification=notification,
            preview=preview,
            delivery_mode=self._sender.delivery_mode,
        )
        receipt = self._sender.send_thread_message(message)
        if receipt.delivery_mode != message.delivery_mode:
            raise SlackNotificationError(
                "The Slack sender receipt reported a different delivery mode."
            )
        if receipt.thread != message.thread:
            raise SlackNotificationError(
                "The Slack sender receipt reported a different thread."
            )
        if self._approval_threads is not None:
            self._approval_threads.register(
                team_id=notification.team_id,
                channel_id=receipt.thread.channel_id,
                message_ts=receipt.message_id,
                pending=notification.pending,
            )
        return SlackNotificationResult(
            preview=preview,
            message=message,
            receipt=receipt,
        )


def _format_delta(delta: SlackRequirementDelta) -> str:
    previous = (
        _escape_user_text(delta.previous_summary)
        if delta.previous_summary is not None
        else "(not supplied)"
    )
    return (
        f"- {_escape_user_text(delta.scope)}: {previous} => "
        f"{_escape_user_text(delta.proposed_summary)}"
    )


def _format_assignment_partition(
    label: str,
    assignment_ids: tuple[str, ...],
    assignment_labels: Mapping[str, str],
) -> list[str]:
    if not assignment_ids:
        return [f"{label}: (none)"]
    displayed = assignment_ids[:_MAX_LISTED_ASSIGNMENTS]
    lines = [
        f"{label} ({len(assignment_ids)}): "
        + ", ".join(
            (
                f"{_escape_user_text(assignment_labels[item])} "
                f"({_escape_user_text(item)})"
                if item in assignment_labels
                else _escape_user_text(item)
            )
            for item in displayed
        )
    ]
    remainder = len(assignment_ids) - len(displayed)
    if remainder:
        lines.append(f"... and {remainder} more")
    return lines


def _format_timestamp(value: datetime) -> str:
    return (
        value.astimezone(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _escape_user_text(value: str) -> str:
    """Collapse controls and neutralize Slack's special ``<...>`` syntax."""

    normalized = " ".join(value.replace("\x00", "").split())
    return (
        normalized.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
