from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from threading import Condition
from typing import Any

from fastapi import Request
from pydantic import BaseModel

from dragback.services.support import event_payload


@dataclass(frozen=True)
class StreamEvent:
    sequence: int
    envelope: dict[str, Any]

    def encode(self) -> str:
        event_type = str(self.envelope["event"])
        data = json.dumps(self.envelope, separators=(",", ":"), sort_keys=True)
        return f"id: {self.sequence}\nevent: {event_type}\ndata: {data}\n\n"


class EventBroker:
    """Small process-local broker for deterministic demo state changes.

    API mutations run in FastAPI worker threads while SSE generators run on the
    event loop. A threading condition bridges those two execution contexts
    without polling the graph or agent state.
    """

    def __init__(self, *, history_size: int = 100) -> None:
        self._condition = Condition()
        self._sequence = 0
        self._events: deque[StreamEvent] = deque(maxlen=history_size)

    @property
    def current_sequence(self) -> int:
        with self._condition:
            return self._sequence

    def publish(
        self,
        event_type: str,
        data: BaseModel | Mapping[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> StreamEvent:
        envelope = event_payload(event_type, data, correlation_id=correlation_id)
        with self._condition:
            self._sequence += 1
            item = StreamEvent(sequence=self._sequence, envelope=envelope)
            self._events.append(item)
            self._condition.notify_all()
            return item

    def _events_after(self, sequence: int, timeout_seconds: float) -> list[StreamEvent]:
        with self._condition:
            if self._sequence <= sequence:
                self._condition.wait_for(
                    lambda: self._sequence > sequence,
                    timeout=timeout_seconds,
                )
            return [item for item in self._events if item.sequence > sequence]

    async def wait_for_events(
        self,
        sequence: int,
        *,
        timeout_seconds: float = 15.0,
    ) -> list[StreamEvent]:
        return await asyncio.to_thread(self._events_after, sequence, timeout_seconds)


def snapshot_event(
    *,
    sequence: int,
    event_type: str,
    data: BaseModel | Mapping[str, Any],
    correlation_id: str | None = None,
) -> StreamEvent:
    return StreamEvent(
        sequence=sequence,
        envelope=event_payload(event_type, data, correlation_id=correlation_id),
    )


async def stream_events(
    *,
    request: Request,
    broker: EventBroker,
    initial: StreamEvent,
) -> AsyncIterator[str]:
    """Yield an immediate snapshot, then every published change and heartbeat."""

    cursor = initial.sequence
    yield "retry: 2000\n" + initial.encode()

    while not await request.is_disconnected():
        items = await broker.wait_for_events(cursor)
        if not items:
            yield ": keep-alive\n\n"
            continue
        for item in items:
            cursor = item.sequence
            yield item.encode()
