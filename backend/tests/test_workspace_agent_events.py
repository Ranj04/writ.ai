from __future__ import annotations

import asyncio
import json
from typing import cast

from dragback.services import agent_api
from dragback.services.events import StreamEvent
from fastapi import Request


def _clear_workspace_brokers() -> None:
    with agent_api.workspace_event_brokers_lock:
        agent_api.workspace_event_brokers.clear()


def test_workspace_event_publication_is_redacted_and_isolated() -> None:
    _clear_workspace_brokers()
    alpha = agent_api._workspace_event_broker("workspace-alpha")
    beta = agent_api._workspace_event_broker("workspace-beta")

    agent_api._publish_workspace(
        "workspace-alpha",
        {
            "id": "workspace-alpha",
            "name": "Alpha",
            "supervisor": {
                "assignments": [
                    {
                        "task_id": "TASK-ALPHA",
                        "run_id": "RUN-ALPHA",
                        "state": "interrupted",
                    }
                ]
            },
            "grant": {
                "payload": {"authorization_id": "AUTH-ALPHA"},
                "token": "must-not-stream",
            },
        },
    )

    async def receive() -> tuple[list[StreamEvent], list[StreamEvent]]:
        return (
            await alpha.wait_for_events(0, timeout_seconds=0.01),
            await beta.wait_for_events(0, timeout_seconds=0.01),
        )

    alpha_events, beta_events = asyncio.run(receive())

    assert len(alpha_events) == 1
    assert beta_events == []
    envelope = alpha_events[0].envelope
    assert envelope["data"]["id"] == "workspace-alpha"
    assert envelope["data"]["name"] == "Alpha"
    assert envelope["data"]["supervisor"]["assignments"][0]["state"] == (
        "interrupted"
    )
    assert envelope["data"]["grant"]["payload"]["authorization_id"] == (
        "AUTH-ALPHA"
    )
    assert envelope["data"]["grant"]["token"] == "[REDACTED]"
    assert "must-not-stream" not in json.dumps(envelope)


def test_workspace_stream_starts_with_full_redacted_immediate_snapshot(
    monkeypatch,
) -> None:
    _clear_workspace_brokers()
    workspace = {
        "id": "workspace-alpha",
        "name": "Alpha",
        "status": "authorized",
        "graph_version": "graph-v17",
        "supervisor": {
            "assignments": [
                {
                    "task_id": "TASK-ALPHA",
                    "run_id": "RUN-ALPHA",
                    "state": "running",
                }
            ]
        },
        "grant_token": "must-not-stream",
    }
    monkeypatch.setattr(
        agent_api.workspace_orchestrator,
        "get",
        lambda workspace_id: workspace if workspace_id == "workspace-alpha" else None,
    )

    class DisconnectedRequest:
        async def is_disconnected(self) -> bool:
            return True

    response = agent_api.live_workspace_events(
        "workspace-alpha",
        cast(Request, DisconnectedRequest()),
    )

    async def first_chunk() -> str:
        raw = await anext(response.body_iterator.__aiter__())
        return raw if isinstance(raw, str) else bytes(raw).decode()

    chunk = asyncio.run(first_chunk())
    data_line = next(
        line.removeprefix("data: ")
        for line in chunk.splitlines()
        if line.startswith("data: ")
    )
    envelope = json.loads(data_line)

    assert envelope["event"] == "live-workspace.supervisor.snapshot"
    assert envelope["data"]["id"] == "workspace-alpha"
    assert envelope["data"]["name"] == "Alpha"
    assert envelope["data"]["graph_version"] == "graph-v17"
    assert envelope["data"]["supervisor"]["assignments"][0]["state"] == "running"
    assert envelope["data"]["grant_token"] == "[REDACTED]"
    assert "must-not-stream" not in chunk


def test_workspace_response_publishes_complete_mutated_view() -> None:
    _clear_workspace_brokers()
    workspace = {
        "id": "workspace-alpha",
        "name": "Alpha",
        "status": "change-applied",
        "graph_version": "graph-v18",
        "tasks": [{"id": "TASK-ALPHA", "validity": "INVALIDATED"}],
        "supervisor": {
            "assignments": [
                {
                    "task_id": "TASK-ALPHA",
                    "run_id": "RUN-ALPHA",
                    "state": "interrupted",
                }
            ]
        },
    }

    response = agent_api._workspace_response("workspace-alpha", workspace)
    events = asyncio.run(
        agent_api._workspace_event_broker("workspace-alpha").wait_for_events(
            0,
            timeout_seconds=0.01,
        )
    )

    assert response["status"] == "change-applied"
    assert response["graph_version"] == "graph-v18"
    assert len(events) == 1
    assert events[0].envelope["data"]["tasks"] == workspace["tasks"]
    assert events[0].envelope["data"]["supervisor"] == workspace["supervisor"]
