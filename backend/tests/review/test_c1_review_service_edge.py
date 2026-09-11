"""Track C round-1 adversarial review tests (reviewer: fable).

Targets the seams named in the review addendum: the privacy of the new request
log line, the limiter's own memory, middleware ordering, blast radius on the
enforcement path, and the truth of ``PUBLIC_INTAKE_ROUTES``.

Tests whose name starts with ``test_finding_`` are the committed failing tests
for findings in ``.review/c/1/findings.json``. Everything else is a probe that
passes today and pins behaviour the build prompt stated in prose.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from writai.config import settings
from writai.services import agent_api, authority_api, executor_api, support

REPO_ROOT = Path(__file__).resolve().parents[3]
APPS = [authority_api.app, agent_api.app, executor_api.app]


@pytest.fixture(autouse=True)
def _reset_limiter() -> Iterator[None]:
    support.public_intake_limiter.reset()
    yield
    support.public_intake_limiter.reset()


def _all_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return list(caplog.records)


def _record_mentions(record: logging.LogRecord, needle: str) -> bool:
    if needle in record.getMessage():
        return True
    return any(needle in str(value) for value in record.__dict__.values())


def _request_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == support.logger.name and record.getMessage() == "request"
    ]


# --------------------------------------------------------------------------- #
# C1 — the log line must never carry user data
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("app", "method", "path"),
    [
        (agent_api.app, "GET", "/live-workspaces/{secret}"),
        (agent_api.app, "POST", "/supervisor/sessions/{secret}/check"),
        (agent_api.app, "POST", "/supervisor/sessions/{secret}/acknowledge"),
        (authority_api.app, "POST", "/intake/slack/{secret}"),
        (authority_api.app, "GET", "/nope/{secret}"),
        (executor_api.app, "GET", "/does-not-exist/{secret}"),
    ],
)
def test_no_log_record_carries_path_query_header_or_body_data(
    caplog: pytest.LogCaptureFixture,
    app: Any,
    method: str,
    path: str,
) -> None:
    secret_path = f"sk-path-{uuid.uuid4().hex}"
    secret_query = f"sk-query-{uuid.uuid4().hex}"
    secret_header = f"sk-header-{uuid.uuid4().hex}"
    secret_body = f"sk-body-{uuid.uuid4().hex}"
    with caplog.at_level(logging.DEBUG):
        TestClient(app).request(
            method,
            path.format(secret=secret_path),
            params={"token": secret_query},
            headers={
                "Authorization": f"Bearer {secret_header}",
                "X-writ.ai-Internal-Authorization": secret_header,
                "X-Hook-Api-Key": secret_header,
            },
            json={"session_id": secret_body, "tool_input": secret_body},
        )

    records = _request_records(caplog)
    assert len(records) == 1, "exactly one request line per request"
    for needle in (secret_path, secret_query, secret_header, secret_body):
        offenders = [r for r in _all_records(caplog) if _record_mentions(r, needle)]
        assert not offenders, f"{needle!r} leaked into {offenders[0].__dict__}"


def test_a_traversal_looking_correlation_id_is_rejected_and_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = TestClient(authority_api.app).get(
            "/health", headers={"X-Correlation-ID": "../../etc/passwd"}
        )

    uuid.UUID(response.headers["X-Correlation-ID"])  # a fresh id was minted
    assert not any(_record_mentions(r, "etc/passwd") for r in _all_records(caplog))


def test_a_405_on_a_parameterised_route_logs_the_template(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        response = TestClient(agent_api.app).get(
            "/supervisor/sessions/ws-secret-405/check"
        )

    assert response.status_code == 405
    records = _request_records(caplog)
    assert len(records) == 1
    record = cast(Any, records[0])
    assert "ws-secret-405" not in str(record.path)
    assert record.status == 405


def test_the_rate_limited_response_still_carries_a_correlation_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(authority_api.app)
    for _ in range(60):
        client.post("/webhooks/hexclave", content=b"{}")
    with caplog.at_level(logging.INFO):
        response = client.post("/webhooks/hexclave", content=b"{}")

    assert response.status_code == 429
    assert response.headers["X-Correlation-ID"] == response.json()["correlation_id"]
    records = _request_records(caplog)
    assert len(records) == 1
    record = cast(Any, records[0])
    assert record.status == 429
    assert record.correlation_id == response.json()["correlation_id"]


def test_the_production_logging_path_formats_bare_records_without_error() -> None:
    """Outside pytest, root has no handlers, so ``basicConfig`` installs one.

    A record that carries none of the request extras must still format, and the
    request line must reach stderr with the correlation id from the header.
    """

    script = """
import logging, sys
from fastapi.testclient import TestClient
from writai.services.authority_api import app
logging.getLogger("bare").info("no-extras")
logging.getLogger("bare").warning("bare-warning")
with TestClient(app) as c:
    r = c.get("/health")
print("CORR=" + r.headers["X-Correlation-ID"])
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(REPO_ROOT / "backend"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    correlation_id = completed.stdout.strip().split("CORR=")[-1]
    assert "Logging error" not in completed.stderr
    assert "KeyError" not in completed.stderr
    request_lines = [
        line for line in completed.stderr.splitlines() if "message=request" in line
    ]
    assert len(request_lines) == 1, completed.stderr
    assert f"correlation_id={correlation_id}" in request_lines[0]
    assert "path=/health" in request_lines[0]
    assert "bare-warning" in completed.stderr


# --------------------------------------------------------------------------- #
# C2 — blast radius, ordering, and route truth
# --------------------------------------------------------------------------- #


def _registered_post_paths() -> set[str]:
    paths: set[str] = set()
    for app in APPS:
        for route in app.routes:
            if isinstance(route, APIRoute) and route.methods and "POST" in route.methods:
                paths.add(route.path)
    return paths


def test_every_public_intake_template_matches_a_registered_post_route() -> None:
    registered = _registered_post_paths()
    for template in support.PUBLIC_INTAKE_ROUTES:
        assert template in registered, f"{template} limits nothing"


def test_the_enforcement_check_route_is_never_rate_limited() -> None:
    client = TestClient(agent_api.app)
    statuses = {
        client.post(
            "/supervisor/sessions/session-under-load/check", json={}
        ).status_code
        for _ in range(200)
    }
    assert 429 not in statuses, statuses


@pytest.mark.parametrize(
    ("app", "method", "path"),
    [
        (agent_api.app, "POST", "/supervisor/sessions/start"),
        (agent_api.app, "POST", "/supervisor/sessions/s-1/acknowledge"),
        (agent_api.app, "GET", "/live-workspaces/ws-under-load"),
        (agent_api.app, "GET", "/live-workspaces"),
        (authority_api.app, "GET", "/health"),
        (authority_api.app, "POST", "/authorize"),
        (executor_api.app, "GET", "/health"),
        # Orchestrator adjudication: this parameter contradicted this file's own
        # finding F1, which established that /intake/slack/{workspace_id}/reaction
        # performs the same anonymous Slack signature verification as the other
        # public intakes and therefore MUST be rate-limited. F1 is the considered
        # position, with a written rationale; this entry was a leftover from the
        # scope list. Removed rather than weakened - see test_finding_f1_* below,
        # which is untouched and still asserts the reaction intake is limited.
    ],
)
def test_non_intake_routes_are_never_rate_limited(
    app: Any, method: str, path: str
) -> None:
    client = TestClient(app)
    statuses = {client.request(method, path, json={}).status_code for _ in range(150)}
    assert 429 not in statuses, statuses


@pytest.mark.parametrize(
    ("app", "path"),
    [
        (authority_api.app, "/webhooks/hexclave"),
        (agent_api.app, "/intake/crustdata/person/capture"),
    ],
)
def test_the_other_two_public_intakes_are_rate_limited_before_verification(
    app: Any, path: str
) -> None:
    client = TestClient(app)
    responses = [client.post(path, content=b"{}") for _ in range(61)]
    assert all(r.status_code != 429 for r in responses[:60])
    assert responses[60].status_code == 429
    assert responses[60].json()["error"]["code"] == "RATE_LIMITED"


def test_the_off_switch_disables_the_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(support, "settings", replace(settings, rate_limit_enabled=False))
    client = TestClient(authority_api.app)
    statuses = {
        client.post("/intake/slack/ws-1", content=b"{}").status_code for _ in range(70)
    }
    assert statuses == {503}


def test_finding_f1_slack_reaction_intake_is_not_rate_limited() -> None:
    """FINDING F1 (fails today).

    ``/intake/slack/{workspace_id}/reaction`` runs the same
    ``_live_slack_verifier`` signature verification as ``/intake/slack/{workspace_id}``
    and is reachable by the same anonymous caller, but ``_public_intake_route``
    explicitly rejects any path with a second segment after the workspace id, so it
    stays unbounded. Passing this test means the reaction intake is limited too.
    """

    client = TestClient(authority_api.app)
    responses = [
        client.post("/intake/slack/ws-1/reaction", content=b"{}") for _ in range(61)
    ]
    assert responses[0].status_code == 503, "fail-closed path, no credentials"
    assert responses[60].status_code == 429, (
        "the 61st reaction event still performed verification work: "
        f"{responses[60].status_code}"
    )


# --------------------------------------------------------------------------- #
# C2 — the limiter's own memory and concurrency
# --------------------------------------------------------------------------- #


def test_ten_thousand_distinct_hosts_do_not_grow_the_registry_across_windows() -> None:
    now = 0.0
    limiter = support.TokenBucketLimiter(60, clock=lambda: now)

    for index in range(10_000):
        assert limiter.allow("/intake/slack/{workspace_id}", f"10.0.{index // 256}.{index % 256}")
    # Within one window every distinct host holds a bucket: bounded by the window,
    # not by the request count.
    assert len(limiter._buckets) == 10_000

    now = 60.0 + 1e-6
    assert limiter.allow("/intake/slack/{workspace_id}", "new-host")
    assert len(limiter._buckets) == 1
    assert ("/intake/slack/{workspace_id}", "new-host") in limiter._buckets

    # A second round from fresh hosts, then another window: still bounded.
    for index in range(10_000):
        limiter.allow("/webhooks/hexclave", f"host-{index}")
    now = 121.0
    limiter.allow("/webhooks/hexclave", "last")
    assert len(limiter._buckets) == 1


def test_a_bucket_touched_by_denied_requests_only_is_still_evicted_when_idle() -> None:
    now = 0.0
    limiter = support.TokenBucketLimiter(1, clock=lambda: now)
    assert limiter.allow("/r", "attacker")
    for _ in range(50):
        assert not limiter.allow("/r", "attacker")
    now = 30.0
    assert not limiter.allow("/r", "attacker")  # 0.5 tokens refilled
    now = 91.0  # 61 s idle since the last touch
    assert limiter.allow("/r", "other")
    assert ("/r", "attacker") not in limiter._buckets


def test_concurrent_callers_never_over_admit() -> None:
    limiter = support.TokenBucketLimiter(100, clock=lambda: 0.0)
    admitted: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def worker() -> None:
        barrier.wait()
        local = [limiter.allow("/r", "host") for _ in range(50)]
        with lock:
            admitted.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(admitted) == 100
    assert len(admitted) == 800


def test_a_bucket_starts_full_and_refills_at_the_configured_rate() -> None:
    now = 0.0
    limiter = support.TokenBucketLimiter(60, clock=lambda: now)
    assert all(limiter.allow("/r", "h") for _ in range(60))
    assert not limiter.allow("/r", "h")
    now = 0.999
    assert not limiter.allow("/r", "h")
    now = 1.0
    assert limiter.allow("/r", "h")
    assert not limiter.allow("/r", "h")


def test_a_zero_limit_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        support.TokenBucketLimiter(0)
