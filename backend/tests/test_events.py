from __future__ import annotations

import asyncio
import json
from typing import cast

from dragback.services.events import (
    EventBroker,
    StreamEvent,
    snapshot_event,
    stream_events,
)
from fastapi import Request


def test_event_broker_wakes_subscribers_with_correlated_state() -> None:
    broker = EventBroker()

    async def receive() -> tuple[StreamEvent, list[StreamEvent]]:
        waiting = asyncio.create_task(broker.wait_for_events(0, timeout_seconds=1))
        await asyncio.sleep(0)
        published = broker.publish(
            "graph.state.changed",
            {"graph_version": "graph-v18"},
            correlation_id="decision-018",
        )
        received = await waiting
        return published, received

    published, received_items = asyncio.run(receive())
    received = received_items[0]

    assert received == published
    assert received.envelope == {
        "event": "graph.state.changed",
        "data": {"graph_version": "graph-v18"},
        "correlation_id": "decision-018",
    }


def test_snapshot_event_encodes_valid_server_sent_event() -> None:
    item = snapshot_event(
        sequence=7,
        event_type="loop.state.snapshot",
        data={"state": "REPLAN"},
        correlation_id="run-27",
    )

    lines = item.encode().strip().splitlines()

    assert lines[:2] == ["id: 7", "event: loop.state.snapshot"]
    assert json.loads(lines[2].removeprefix("data: ")) == item.envelope


def test_event_broker_delivers_the_same_change_to_multiple_subscribers() -> None:
    broker = EventBroker()

    async def receive_both() -> tuple[list[StreamEvent], list[StreamEvent]]:
        first = asyncio.create_task(broker.wait_for_events(0, timeout_seconds=1))
        second = asyncio.create_task(broker.wait_for_events(0, timeout_seconds=1))
        await asyncio.sleep(0)
        broker.publish("graph.state.changed", {"graph_version": "graph-v18"})
        return await first, await second

    first_items, second_items = asyncio.run(receive_both())

    assert len(first_items) == 1
    assert second_items == first_items


def test_stream_starts_with_retry_and_correlated_snapshot() -> None:
    class DisconnectedRequest:
        async def is_disconnected(self) -> bool:
            return True

    broker = EventBroker()
    initial = snapshot_event(
        sequence=0,
        event_type="graph.state.snapshot",
        data={"graph_version": "graph-v17"},
        correlation_id="stream-17",
    )

    async def first_chunk() -> str:
        stream = stream_events(
            request=cast(Request, DisconnectedRequest()),
            broker=broker,
            initial=initial,
        )
        return await anext(stream)

    chunk = asyncio.run(first_chunk())

    assert chunk.startswith("retry: 2000\nid: 0\nevent: graph.state.snapshot\n")
    assert '"correlation_id":"stream-17"' in chunk
