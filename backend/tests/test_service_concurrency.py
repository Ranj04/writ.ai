from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from dragback.domain import AuthorizationResult, Verdict
from dragback.services import agent_api, support


def test_agent_reset_waits_for_an_in_flight_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization_started = Event()
    release_authorization = Event()
    reset_started = Event()
    reset_finished = Event()

    def slow_authorize(_url: str, **_kwargs: object) -> httpx.Response:
        authorization_started.set()
        assert release_authorization.wait(timeout=1)
        result = AuthorizationResult(
            verdict=Verdict.ALLOW,
            reason="Plan matches current approved requirements.",
            graph_version="graph-v17",
            task_id="TICKET-100",
        )
        return httpx.Response(200, json=result.model_dump(mode="json"))

    def concurrent_reset() -> dict[str, object]:
        reset_started.set()
        response = agent_api.reset_demo()
        reset_finished.set()
        return response

    monkeypatch.setattr(support.httpx, "post", slow_authorize)
    agent_api.reset_demo()

    with ThreadPoolExecutor(max_workers=2) as pool:
        start_future = pool.submit(agent_api.start_demo)
        assert authorization_started.wait(timeout=1)
        reset_future = pool.submit(concurrent_reset)
        assert reset_started.wait(timeout=1)
        assert reset_finished.wait(timeout=0.05) is False
        release_authorization.set()

        start_response = start_future.result(timeout=1)
        reset_future.result(timeout=1)

    assert start_response["run"] is not None
    assert agent_api.state_payload()["run"] is None
