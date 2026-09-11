from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from writai.domain import AuthorizationResult, Verdict
from writai.fixtures import load_decision_v18, load_graph_fixture
from writai.services import agent_api, authority_api, executor_api, support

CORRELATION_ID = "demo-run-27"


@pytest.fixture(autouse=True)
def _reset_public_intake_limiter() -> None:
    support.public_intake_limiter.reset()


def _request_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == support.logger.name and record.getMessage() == "request"
    ]


def _record_contains(record: logging.LogRecord, value: str) -> bool:
    return any(value in str(item) for item in record.__dict__.values())


def test_uvicorn_access_log_is_silenced_before_it_can_log_raw_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access_logger = logging.getLogger("uvicorn.access")
    records: list[logging.LogRecord] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _CaptureHandler()
    access_logger.addHandler(handler)
    monkeypatch.setattr(support, "_logging_configured", False)
    monkeypatch.setattr(access_logger, "disabled", False)
    monkeypatch.setattr(access_logger, "level", logging.INFO)
    try:
        support.configure_logging()
        access_logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:1234",
            "GET",
            "/live-workspaces/ws-secret-in-access-log",
            "1.1",
            404,
        )
    finally:
        access_logger.removeHandler(handler)

    assert access_logger.getEffectiveLevel() >= logging.WARNING
    assert records == []


def test_every_request_writes_one_log_line_carrying_its_correlation_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        response = TestClient(authority_api.app).get("/health")

    records = _request_records(caplog)
    assert len(records) == 1
    record = cast(Any, records[0])
    assert record.correlation_id == response.json()["correlation_id"]
    assert record.path == "/health"
    assert record.status == 200
    assert record.duration_ms >= 0


def test_a_request_log_line_never_contains_a_workspace_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace_id = "ws-secret-do-not-log"
    with caplog.at_level(logging.INFO):
        TestClient(agent_api.app).get(f"/live-workspaces/{workspace_id}")

    records = _request_records(caplog)
    assert len(records) == 1
    record = cast(Any, records[0])
    assert record.path == "/live-workspaces/{workspace_id}"
    assert not any(_record_contains(record, workspace_id) for record in caplog.records)


def test_an_unmatched_route_logs_the_placeholder_not_the_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        TestClient(authority_api.app).get("/nope/ws-secret")

    records = _request_records(caplog)
    assert len(records) == 1
    record = cast(Any, records[0])
    assert record.path == "<unmatched>"
    assert not any(_record_contains(record, "ws-secret") for record in caplog.records)


def test_public_slack_intake_is_rate_limited() -> None:
    client = TestClient(authority_api.app)

    responses = [client.post("/intake/slack/ws-1", content=b"{}") for _ in range(61)]

    assert [response.status_code for response in responses[:60]] == [503] * 60
    assert responses[60].status_code == 429
    assert responses[60].json()["error"]["code"] == "RATE_LIMITED"


def test_the_rate_limit_precedes_signature_verification() -> None:
    client = TestClient(authority_api.app)

    for _ in range(60):
        assert client.post("/intake/slack/ws-1", content=b"{}").status_code == 503
    response = client.post("/intake/slack/ws-1", content=b"{}")

    assert response.status_code == 429
    assert response.status_code != 503


def test_authorize_is_not_rate_limited() -> None:
    client = TestClient(authority_api.app)

    responses = [client.post("/authorize", json={}) for _ in range(100)]

    assert all(response.status_code != 429 for response in responses)


def test_the_limiter_evicts_idle_buckets() -> None:
    now = 0.0
    limiter = support.TokenBucketLimiter(1, clock=lambda: now)

    assert limiter.allow("/first", "client")
    assert not limiter.allow("/first", "client")
    now = 61.0
    assert limiter.allow("/second", "client")

    assert ("/first", "client") not in limiter._buckets
    assert ("/second", "client") in limiter._buckets


@pytest.mark.parametrize(
    "service_app",
    [authority_api.app, agent_api.app, executor_api.app],
    ids=["authority", "agent", "executor"],
)
def test_every_service_returns_the_request_correlation_id(service_app: FastAPI) -> None:
    response = TestClient(service_app).get(
        "/health", headers={support.CORRELATION_ID_HEADER: CORRELATION_ID}
    )

    assert response.status_code == 200
    assert response.headers[support.CORRELATION_ID_HEADER] == CORRELATION_ID
    assert response.json()["correlation_id"] == CORRELATION_ID


@pytest.mark.parametrize("origin", support.DEMO_FRONTEND_ORIGINS)
@pytest.mark.parametrize(
    "service_app",
    [authority_api.app, agent_api.app, executor_api.app],
    ids=["authority", "agent", "executor"],
)
def test_local_frontend_origins_are_allowed(
    service_app: FastAPI,
    origin: str,
) -> None:
    response = TestClient(service_app).options(
        "/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


def test_destructive_demo_reset_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_version = authority_api.runtime.graph.version_label
    monkeypatch.setattr(
        authority_api,
        "settings",
        replace(authority_api.settings, demo_reset_enabled=False),
    )
    monkeypatch.setattr(
        agent_api,
        "settings",
        replace(agent_api.settings, demo_reset_enabled=False),
    )

    authority_response = TestClient(authority_api.app).post("/graph/reset")
    agent_response = TestClient(agent_api.app).post("/demo/reset-all")

    assert authority_response.status_code == 403
    assert authority_response.json()["error"]["code"] == "DEMO_RESET_DISABLED"
    assert authority_api.runtime.graph.version_label == authority_version
    assert agent_response.status_code == 403
    assert agent_response.json()["error"]["code"] == "DEMO_RESET_DISABLED"


def test_validation_errors_use_the_shared_error_contract() -> None:
    response = TestClient(executor_api.app).post(
        "/execute",
        json={},
        headers={support.CORRELATION_ID_HEADER: CORRELATION_ID},
    )

    assert response.status_code == 422
    assert response.headers[support.CORRELATION_ID_HEADER] == CORRELATION_ID
    assert response.json() == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "The request payload is invalid.",
            "retryable": False,
            "details": {
                "issues": [
                    {"location": "body.token", "type": "missing"},
                    {"location": "body.run_id", "type": "missing"},
                    {"location": "body.task_id", "type": "missing"},
                    {"location": "body.plan", "type": "missing"},
                ]
            },
        },
        "correlation_id": CORRELATION_ID,
    }


def test_agent_forwards_correlation_id_to_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_headers: dict[str, str] = {}

    def fake_post(_url: str, **kwargs: object) -> httpx.Response:
        captured_headers.update(cast(dict[str, str], kwargs["headers"]))
        result = AuthorizationResult(
            verdict=Verdict.ALLOW,
            reason="Plan matches current approved requirements.",
            graph_version="graph-v17",
            task_id="TICKET-100",
        )
        return httpx.Response(200, json=result.model_dump(mode="json"))

    monkeypatch.setattr(support.httpx, "post", fake_post)
    client = TestClient(agent_api.app)
    client.post("/demo/reset")

    response = client.post(
        "/demo/start", headers={support.CORRELATION_ID_HEADER: CORRELATION_ID}
    )

    assert response.status_code == 200
    assert captured_headers[support.CORRELATION_ID_HEADER] == CORRELATION_ID
    assert response.headers[support.CORRELATION_ID_HEADER] == CORRELATION_ID
    assert response.json()["correlation_id"] == CORRELATION_ID


def test_authority_timeout_has_a_stable_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_headers: dict[str, str] = {}

    def timeout(_url: str, **kwargs: object) -> httpx.Response:
        captured_headers.update(cast(dict[str, str], kwargs["headers"]))
        raise httpx.ReadTimeout(
            "host-specific timeout details",
            request=httpx.Request("POST", "http://authority.invalid/authorize"),
        )

    monkeypatch.setattr(support.httpx, "post", timeout)
    client = TestClient(agent_api.app)
    client.post("/demo/reset")

    response = client.post(
        "/demo/start", headers={support.CORRELATION_ID_HEADER: CORRELATION_ID}
    )

    assert response.status_code == 504
    assert captured_headers[support.CORRELATION_ID_HEADER] == CORRELATION_ID
    assert response.json() == {
        "error": {
            "code": "AUTHORITY_TIMEOUT",
            "message": "Intent authority timed out.",
            "retryable": True,
        },
        "correlation_id": CORRELATION_ID,
    }


@pytest.mark.parametrize(
    ("fake_response", "expected_code"),
    [
        (
            lambda: httpx.Response(503, json={"detail": "private upstream failure"}),
            "AUTHORITY_ERROR",
        ),
        (lambda: httpx.Response(200, content=b"not-json"), "AUTHORITY_INVALID_RESPONSE"),
    ],
)
def test_executor_rejects_bad_authority_responses(
    monkeypatch: pytest.MonkeyPatch,
    fake_response: Callable[[], httpx.Response],
    expected_code: str,
) -> None:
    def fake_post(_url: str, **_kwargs: object) -> httpx.Response:
        return fake_response()

    monkeypatch.setattr(support.httpx, "post", fake_post)
    _, _, _, run = load_graph_fixture()
    response = TestClient(executor_api.app).post(
        "/execute",
        json={
            "token": "unused-by-fake-authority",
            "run_id": run.run_id,
            "task_id": run.ticket_id,
            "plan": run.plan.model_dump(mode="json"),
        },
        headers={support.CORRELATION_ID_HEADER: CORRELATION_ID},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == expected_code
    assert "private upstream failure" not in response.text
    assert response.json()["correlation_id"] == CORRELATION_ID


def test_tests_pass_cannot_override_a_replan_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(_url: str, **_kwargs: object) -> httpx.Response:
        result = AuthorizationResult(
            verdict=Verdict.REPLAN,
            reason="The plan conflicts with current approved requirements.",
            graph_version="graph-v18",
            task_id="TICKET-100",
            affected_scopes={"export.authorization"},
            current_requirements={"export.authorization": {"audience": "admin_only"}},
        )
        return httpx.Response(200, json=result.model_dump(mode="json"))

    monkeypatch.setattr(support.httpx, "post", fake_post)
    client = TestClient(agent_api.app)
    client.post("/demo/reset")
    client.post("/demo/start")

    response = client.post(
        "/demo/tests-pass",
        headers={support.CORRELATION_ID_HEADER: CORRELATION_ID},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"
    assert response.json()["correlation_id"] == CORRELATION_ID


def test_replan_requires_a_replan_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(_url: str, **_kwargs: object) -> httpx.Response:
        result = AuthorizationResult(
            verdict=Verdict.ALLOW,
            reason="Plan matches current approved requirements.",
            graph_version="graph-v17",
            task_id="TICKET-100",
        )
        return httpx.Response(200, json=result.model_dump(mode="json"))

    monkeypatch.setattr(support.httpx, "post", fake_post)
    client = TestClient(agent_api.app)
    client.post("/demo/reset")
    client.post("/demo/start")

    response = client.post(
        "/demo/replan",
        headers={support.CORRELATION_ID_HEADER: CORRELATION_ID},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REPLAN_NOT_AUTHORIZED"


def test_agent_preserves_the_initial_plan_after_replanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter(
        [
            AuthorizationResult(
                verdict=Verdict.ALLOW,
                reason="Plan matches current approved requirements.",
                graph_version="graph-v17",
                task_id="TICKET-100",
            ),
            AuthorizationResult(
                verdict=Verdict.REPLAN,
                reason="The plan conflicts with current approved requirements.",
                graph_version="graph-v18",
                task_id="TICKET-100",
                affected_scopes={"export.authorization"},
                current_requirements={
                    "export.authorization": {"audience": "admin_only"}
                },
            ),
            AuthorizationResult(
                verdict=Verdict.ALLOW,
                reason="Plan matches current approved requirements.",
                graph_version="graph-v18",
                task_id="TICKET-100",
            ),
        ]
    )

    def fake_post(_url: str, **_kwargs: object) -> httpx.Response:
        result = next(results)
        return httpx.Response(200, json=result.model_dump(mode="json"))

    monkeypatch.setattr(support.httpx, "post", fake_post)
    client = TestClient(agent_api.app)
    client.post("/demo/reset")
    client.post("/demo/start")
    client.post("/demo/recheck")

    response = client.post("/demo/replan")

    assert response.status_code == 200
    assert response.json()["run"]["plan"]["id"] == "PLAN-028"
    assert response.json()["initial_plan"]["id"] == "PLAN-027"
    assert "initial_grant_token" in response.json()


def test_authority_maps_missing_graph_artifact_without_leaking_store_errors() -> None:
    client = TestClient(authority_api.app)
    client.post("/demo/reset")
    mutation = load_decision_v18().model_copy(update={"supersedes_id": "MISSING"})

    response = client.post(
        "/decisions/ingest",
        json=mutation.model_dump(mode="json"),
        headers={support.CORRELATION_ID_HEADER: CORRELATION_ID},
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "ARTIFACT_NOT_FOUND",
            "message": "The superseded artifact does not exist.",
            "retryable": False,
        },
        "correlation_id": CORRELATION_ID,
    }


def test_event_envelope_carries_its_originating_correlation_id() -> None:
    event = support.event_payload(
        "loop.state.changed",
        {"run_id": "RUN-27", "state": "REPLAN"},
        correlation_id=CORRELATION_ID,
    )

    assert event == {
        "event": "loop.state.changed",
        "data": {"run_id": "RUN-27", "state": "REPLAN"},
        "correlation_id": CORRELATION_ID,
    }
