"""Durable replay reservation for signed external deliveries."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from writai.domain import utc_now


class SlackDeliveryKey(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    workspace_id: str = Field(min_length=1, max_length=255)
    connection_user_id: str = Field(min_length=1, max_length=255)
    trigger_kind: str = Field(min_length=1, max_length=255)
    event_id: str = Field(min_length=1, max_length=255)


class SlackDeliveryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: SlackDeliveryKey
    status: Literal["reserved", "completed"] = "reserved"
    reserved_at: datetime = Field(default_factory=utc_now)
    result: dict[str, Any] | None = None


class _SlackDeliveryDocument(BaseModel):
    schema_version: Literal[1] = 1
    deliveries: list[SlackDeliveryRecord] = Field(default_factory=list)


class SlackDeliveryReplayError(RuntimeError):
    pass


class JsonSlackDeliveryReplayStore:
    """Reserve each signed event before extraction and persist completion."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = RLock()

    def _read(self) -> _SlackDeliveryDocument:
        if not self.path.exists():
            return _SlackDeliveryDocument()
        try:
            return _SlackDeliveryDocument.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError) as exc:
            raise SlackDeliveryReplayError(
                "The Slack delivery replay store is unreadable."
            ) from exc

    def _write(self, document: _SlackDeliveryDocument) -> None:
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

    def reserve(self, key: SlackDeliveryKey) -> bool:
        """Return true exactly once for a key, including across restarts."""

        with self._lock:
            document = self._read()
            if any(item.key == key for item in document.deliveries):
                return False
            document.deliveries.append(
                SlackDeliveryRecord(key=key)
            )
            self._write(document)
            return True

    def complete(
        self,
        key: SlackDeliveryKey,
        *,
        result: dict[str, Any],
    ) -> None:
        with self._lock:
            document = self._read()
            for index, item in enumerate(document.deliveries):
                if item.key != key:
                    continue
                if item.status == "completed":
                    if item.result != result:
                        raise SlackDeliveryReplayError(
                            "A completed Slack delivery result is immutable."
                        )
                    return
                document.deliveries[index] = item.model_copy(
                    update={
                        "status": "completed",
                        "result": dict(result),
                    }
                )
                self._write(document)
                return
            raise SlackDeliveryReplayError(
                "The Slack delivery was not reserved before completion."
            )

    def get(self, key: SlackDeliveryKey) -> SlackDeliveryRecord | None:
        with self._lock:
            for item in self._read().deliveries:
                if item.key == key:
                    return item.model_copy(deep=True)
        return None


class CrustDataDeliveryKey(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    watch_id: int = Field(ge=1)
    run_id: int = Field(ge=1)
    notification_id: str = Field(min_length=1, max_length=255)


class CrustDataDeliveryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: CrustDataDeliveryKey
    status: Literal["reserved", "completed"] = "reserved"
    reserved_at: datetime = Field(default_factory=utc_now)
    result: dict[str, Any] | None = None


class _CrustDataDeliveryDocument(BaseModel):
    schema_version: Literal[1] = 1
    deliveries: list[CrustDataDeliveryRecord] = Field(default_factory=list)


class CrustDataDeliveryReplayError(RuntimeError):
    pass


class JsonCrustDataDeliveryReplayStore:
    """Reserve each replayed watcher delivery before raising review flags."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = RLock()

    def _read(self) -> _CrustDataDeliveryDocument:
        if not self.path.exists():
            return _CrustDataDeliveryDocument()
        try:
            return _CrustDataDeliveryDocument.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError) as exc:
            raise CrustDataDeliveryReplayError(
                "The CrustData delivery replay store is unreadable."
            ) from exc

    def _write(self, document: _CrustDataDeliveryDocument) -> None:
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

    def reserve(self, key: CrustDataDeliveryKey) -> bool:
        """Return true once per *completed* delivery, including across restarts.

        A record that is still ``reserved`` is reclaimed rather than treated as
        already handled. The reservation is written before the flags are
        computed, so a crash, or a failure writing the completed result, would
        otherwise leave that delivery permanently reserved: every retry would
        report a duplicate with zero flags, and the human review it should have
        raised would be lost silently. Losing a review flag is the one outcome
        this path exists to prevent.

        The cost is that two processes racing the same delivery could both
        compute flags. ``complete`` keeps a completed result immutable, so the
        second one either matches or raises rather than overwriting — and the
        store is single-writer by design (see `docs/ARCHITECTURE.md`).
        """

        with self._lock:
            document = self._read()
            for index, item in enumerate(document.deliveries):
                if item.key != key:
                    continue
                if item.status == "completed":
                    return False
                # Reclaim the abandoned reservation, refreshing its timestamp so
                # a stuck delivery is visible as recent rather than ancient.
                document.deliveries[index] = CrustDataDeliveryRecord(key=key)
                self._write(document)
                return True
            document.deliveries.append(CrustDataDeliveryRecord(key=key))
            self._write(document)
            return True

    def complete(
        self,
        key: CrustDataDeliveryKey,
        *,
        result: dict[str, Any],
    ) -> None:
        with self._lock:
            document = self._read()
            for index, item in enumerate(document.deliveries):
                if item.key != key:
                    continue
                if item.status == "completed":
                    if item.result != result:
                        raise CrustDataDeliveryReplayError(
                            "A completed CrustData delivery result is immutable."
                        )
                    return
                document.deliveries[index] = item.model_copy(
                    update={
                        "status": "completed",
                        "result": dict(result),
                    }
                )
                self._write(document)
                return
            raise CrustDataDeliveryReplayError(
                "The CrustData delivery was not reserved before completion."
            )

    def get(
        self,
        key: CrustDataDeliveryKey,
    ) -> CrustDataDeliveryRecord | None:
        with self._lock:
            for item in self._read().deliveries:
                if item.key == key:
                    return item.model_copy(deep=True)
        return None
