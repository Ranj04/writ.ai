from __future__ import annotations

from typing import cast
from urllib.parse import urlparse

import httpx
import pytest
from dragback.fixtures import load_decision_v18
from dragback.services import agent_api, authority_api, executor_api, support
from fastapi.testclient import TestClient


def test_three_service_demo_rejects_old_grant_and_accepts_corrected_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = TestClient(authority_api.app)
    agent = TestClient(agent_api.app)
    executor = TestClient(executor_api.app)

    def route_post(url: str, **kwargs: object) -> httpx.Response:
        parsed = urlparse(url)
        if parsed.port == 8001:
            return authority.post(
                parsed.path,
                json=kwargs.get("json"),
                headers=cast(dict[str, str], kwargs.get("headers", {})),
            )
        raise httpx.ConnectError(
            f"Unexpected service URL: {url}",
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(support.httpx, "post", route_post)

    reset = agent.post("/demo/reset-all")
    assert reset.status_code == 200
    assert reset.json()["graph_version"] == "graph-v17"

    initial = agent.post("/demo/start").json()
    assert initial["last_authorization"]["verdict"] == "ALLOW"
    assert initial["run"]["graph_snapshot"] == "graph-v17"
    assert initial["initial_grant_token"]
    assert agent.post("/demo/tests-pass").status_code == 200

    mutation = authority.post(
        "/decisions/ingest",
        json=load_decision_v18().model_dump(mode="json"),
    )
    assert mutation.status_code == 200
    assert mutation.json()["graph_version"] == "graph-v18"

    old_execution = executor.post(
        "/execute",
        json={
            "token": initial["initial_grant_token"],
            "run_id": initial["run"]["run_id"],
            "task_id": initial["run"]["ticket_id"],
            "plan": initial["initial_plan"],
        },
    )
    assert old_execution.status_code == 200
    assert old_execution.json()["applied"] is False
    assert "stale" in old_execution.json()["reason"].lower()

    recheck = agent.post("/demo/recheck")
    assert recheck.status_code == 200
    assert recheck.json()["last_authorization"]["verdict"] == "REPLAN"

    corrected = agent.post("/demo/replan")
    assert corrected.status_code == 200
    corrected_body = corrected.json()
    assert corrected_body["run"]["plan"]["id"] == "PLAN-028"
    assert corrected_body["last_authorization"]["verdict"] == "ALLOW"
    assert corrected_body["run"]["graph_snapshot"] == "graph-v18"

    new_execution = executor.post(
        "/execute",
        json={
            "token": corrected_body["run"]["grant_token"],
            "run_id": corrected_body["run"]["run_id"],
            "task_id": corrected_body["run"]["ticket_id"],
            "plan": corrected_body["run"]["plan"],
        },
    )
    assert new_execution.status_code == 200
    assert new_execution.json()["applied"] is True

    graph = authority.get("/demo/state").json()
    artifacts = {artifact["id"]: artifact for artifact in graph["artifacts"]}
    assert artifacts["TASK-101"]["validity"] == "VALID"
    assert artifacts["TASK-102"]["validity"] == "INVALIDATED"
    assert artifacts["PLAN-027"]["validity"] == "NEEDS_REVIEW"


def test_coordinated_reset_preserves_agent_state_when_authority_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, run = agent_api.load_graph_fixture()
    agent_api.run_state = run.model_copy(deep=True)

    def fail_reset(**_kwargs: object) -> agent_api.AuthorityResetState:
        raise support.ApiError(
            status_code=503,
            code="AUTHORITY_UNAVAILABLE",
            message="Intent authority is unavailable.",
            retryable=True,
        )

    monkeypatch.setattr(agent_api, "post_model", fail_reset)

    response = TestClient(agent_api.app).post("/demo/reset-all")

    assert response.status_code == 503
    assert agent_api.run_state is not None
    assert agent_api.run_state.run_id == run.run_id
    agent_api.reset_demo()


def test_state_event_routes_and_aura_reset_alias_are_exposed() -> None:
    authority_routes = {
        path
        for route in authority_api.app.routes
        if isinstance(path := getattr(route, "path", None), str)
    }
    agent_routes = {
        path
        for route in agent_api.app.routes
        if isinstance(path := getattr(route, "path", None), str)
    }

    assert {"/events", "/graph/reset"} <= authority_routes
    assert {"/events", "/demo/reset-all"} <= agent_routes
