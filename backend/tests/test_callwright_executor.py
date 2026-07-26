from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event, Lock
from typing import Any

import httpx
import pytest
from dragback.domain import (
    AgentPlan,
    GrantPayload,
    GrantVerificationResult,
    PlanAction,
    Verdict,
    VerificationCode,
    utc_now,
)
from dragback.hashing import stable_hash
from dragback.integrations.callwright import (
    CallReceipt,
    CallRequest,
    CallStatus,
    CallwrightConfigurationError,
    CallwrightError,
    CallwrightPlanError,
    FileCallwrightAttemptStore,
    FixtureCallwrightClient,
    InMemoryCallwrightAttemptStore,
    LiveCallwrightClient,
    build_call_request,
)
from dragback.services import executor_api
from fastapi.testclient import TestClient


class SpyCallwrightClient:
    def __init__(
        self,
        *,
        failure: str | None = None,
        provider: str = "voyagr-callwright-fixture",
    ) -> None:
        self.requests: list[CallRequest] = []
        self.failure = failure
        self.provider = provider

    def create_call(self, request: CallRequest) -> CallReceipt:
        self.requests.append(request.model_copy(deep=True))
        if self.failure is not None:
            raise CallwrightError(self.failure)
        return CallReceipt(
            provider=self.provider,
            call_id="CALL-SPY-001",
            status="queued",
            evidence_ref="callwright://calls/CALL-SPY-001",
        )

    def get_call(self, call_id: str) -> CallStatus:
        return CallStatus(
            provider=self.provider,
            call_id=call_id,
            status="queued",
            evidence_ref=f"callwright://calls/{call_id}",
        )


def make_call_plan(**attribute_updates: Any) -> AgentPlan:
    attributes: dict[str, Any] = {
        "provider": "voyagr-callwright",
        "phone_number_ref": "demo-venue",
        "objective": "Request the approved reservation without a paid commitment.",
        "requested_time": "2026-07-26T20:30:00-07:00",
        "party_size": 4,
        "max_deposit_usd": 0,
        "instructions": [
            "Ask whether the requested time is available.",
            "Politely end the call after receiving the answer.",
        ],
        "allowed_commitments": ["request_reservation"],
        "language": "en",
    }
    attributes.update(attribute_updates)
    return AgentPlan(
        id="PLAN-CALL-018",
        ticket_id="TICKET-CALL-100",
        objective="Request the approved reservation",
        actions=[
            PlanAction(
                id="ACTION-CALL-001",
                description="Call the venue with the approved reservation instructions",
                scopes={"reservation.time", "reservation.party_size"},
                attributes=attributes,
            )
        ],
    )


def make_grant(plan: AgentPlan) -> GrantPayload:
    now = utc_now()
    return GrantPayload(
        authorization_id="AUTH-CALL-001",
        run_id="RUN-CALL-018",
        task_id=plan.ticket_id,
        decision_snapshot="graph-v18",
        plan_hash=stable_hash(plan),
        verdict=Verdict.ALLOW,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )


def verification(
    *,
    plan: AgentPlan,
    valid: bool,
    code: VerificationCode,
) -> GrantVerificationResult:
    return GrantVerificationResult(
        valid=valid,
        code=code,
        reason="Grant is valid." if valid else f"Grant rejected: {code.value}.",
        payload=make_grant(plan) if valid else None,
    )


def execute_payload(plan: AgentPlan) -> dict[str, object]:
    return {
        "token": "signed-grant-token-must-not-leak",
        "run_id": "RUN-CALL-018",
        "task_id": plan.ticket_id,
        "plan": plan.model_dump(mode="json"),
    }


def make_live_request(
    *,
    plan: AgentPlan | None = None,
    allowed_phone: str = "+12025550100",
) -> CallRequest:
    selected_plan = plan or make_call_plan()
    return build_call_request(
        action=selected_plan.actions[0],
        verified_grant=make_grant(selected_plan),
        allowed_targets={"demo-venue": allowed_phone},
    )


@pytest.mark.parametrize(
    "code",
    [
        VerificationCode.STALE_SNAPSHOT,
        VerificationCode.PLAN_HASH_MISMATCH,
        VerificationCode.EXPIRED,
    ],
)
def test_rejected_grants_never_invoke_callwright(
    monkeypatch: pytest.MonkeyPatch,
    code: VerificationCode,
) -> None:
    plan = make_call_plan()
    spy = SpyCallwrightClient()
    monkeypatch.setattr(executor_api, "callwright_client", spy)
    monkeypatch.setattr(
        executor_api,
        "post_model",
        lambda **_kwargs: verification(plan=plan, valid=False, code=code),
    )

    response = TestClient(executor_api.app).post("/execute", json=execute_payload(plan))

    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert response.json()["verification_code"] == code.value
    assert spy.requests == []


def test_valid_grant_builds_call_from_verified_payload_and_submits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_call_plan()
    verified = verification(plan=plan, valid=True, code=VerificationCode.VALID)
    assert verified.payload is not None
    spy = SpyCallwrightClient()
    monkeypatch.setattr(executor_api, "callwright_client", spy)
    monkeypatch.setattr(
        executor_api,
        "settings",
        replace(
            executor_api.settings,
            execution_provider="fixture",
            callwright_api_key="secret-api-key",
            callwright_demo_phone_number="+12025550100",
        ),
    )
    monkeypatch.setattr(executor_api, "post_model", lambda **_kwargs: verified)

    response = TestClient(executor_api.app).post("/execute", json=execute_payload(plan))

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["verification_code"] == VerificationCode.VALID.value
    assert body["execution_mode"] == "simulated"
    assert body["call_receipt"] == {
        "provider": "voyagr-callwright-fixture",
        "call_id": "CALL-SPY-001",
        "status": "queued",
        "evidence_ref": "callwright://calls/CALL-SPY-001",
    }
    assert len(spy.requests) == 1
    submitted = spy.requests[0]
    assert submitted.authorization_id == verified.payload.authorization_id
    assert submitted.run_id == verified.payload.run_id
    assert submitted.task_id == verified.payload.task_id
    assert submitted.decision_snapshot == verified.payload.decision_snapshot
    assert submitted.plan_hash == verified.payload.plan_hash
    assert "Requested time: 2026-07-26T20:30:00-07:00." in submitted.brief
    assert "Party size: 4." in submitted.brief
    assert "Do not agree to any deposit or payment." in submitted.brief
    assert submitted.language == "en"
    assert "+12025550100" not in response.text
    assert "secret-api-key" not in response.text
    assert "signed-grant-token-must-not-leak" not in response.text


def test_fixture_client_is_idempotent_per_authorization() -> None:
    request = make_live_request()
    client = FixtureCallwrightClient()

    first = client.create_call(request)
    second = client.create_call(request)

    assert first == second
    assert client.submission_count == 1


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("requested_time", "2026-07-26T21:00:00-07:00"),
        ("party_size", 6),
        ("max_deposit_usd", 25),
        ("objective", "Request a quote only."),
        ("instructions", ["Ask for 9 PM."]),
        ("allowed_commitments", ["request_quote"]),
        ("phone_number_ref", "backup-venue"),
        ("language", "es"),
    ],
)
def test_every_call_instruction_change_changes_plan_hash(
    attribute: str,
    value: object,
) -> None:
    initial = make_call_plan()
    changed = make_call_plan(**{attribute: value})

    assert stable_hash(changed) != stable_hash(initial)


def test_unapproved_phone_reference_is_rejected_without_exposing_a_number() -> None:
    plan = make_call_plan(phone_number_ref="unapproved-target")

    with pytest.raises(
        CallwrightPlanError,
        match="target is not configured",
    ) as error:
        build_call_request(
            action=plan.actions[0],
            verified_grant=make_grant(plan),
            allowed_targets={"demo-venue": "+12025550100"},
        )

    assert "+12025550100" not in str(error.value)


def test_unknown_call_action_fields_are_not_silently_ignored() -> None:
    plan = make_call_plan(brief="Contradictory freeform instructions")

    with pytest.raises(CallwrightPlanError, match="invalid instructions"):
        build_call_request(
            action=plan.actions[0],
            verified_grant=make_grant(plan),
            allowed_targets={"demo-venue": "+12025550100"},
        )


def test_callwright_failure_does_not_change_the_valid_authority_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_call_plan()
    spy = SpyCallwrightClient(failure="Sponsor service is temporarily unavailable.")
    monkeypatch.setattr(executor_api, "callwright_client", spy)
    monkeypatch.setattr(
        executor_api,
        "settings",
        replace(executor_api.settings, execution_provider="fixture"),
    )
    monkeypatch.setattr(
        executor_api,
        "post_model",
        lambda **_kwargs: verification(
            plan=plan,
            valid=True,
            code=VerificationCode.VALID,
        ),
    )

    response = TestClient(executor_api.app).post("/execute", json=execute_payload(plan))

    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert response.json()["verification_code"] == VerificationCode.VALID.value
    assert len(spy.requests) == 1


def test_live_mode_submits_only_after_valid_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_call_plan()
    spy = SpyCallwrightClient(provider="voyagr-callwright")
    monkeypatch.setattr(executor_api, "callwright_client", spy)
    monkeypatch.setattr(
        executor_api,
        "settings",
        replace(
            executor_api.settings,
            execution_provider="callwright",
            callwright_live_calls_enabled=True,
            callwright_api_key="secret-api-key",
            callwright_base_url="https://api.voygr.tech",
            callwright_demo_phone_number="+12025550100",
        ),
    )
    monkeypatch.setattr(
        executor_api,
        "post_model",
        lambda **_kwargs: verification(
            plan=plan,
            valid=True,
            code=VerificationCode.VALID,
        ),
    )

    response = TestClient(executor_api.app).post("/execute", json=execute_payload(plan))

    assert response.status_code == 200
    assert response.json()["applied"] is True
    assert response.json()["execution_mode"] == "live"
    assert len(spy.requests) == 1
    assert "secret-api-key" not in response.text
    assert "+12025550100" not in response.text


def test_live_mode_disabled_makes_zero_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_call_plan()
    spy = SpyCallwrightClient(provider="voyagr-callwright")
    monkeypatch.setattr(executor_api, "callwright_client", spy)
    monkeypatch.setattr(
        executor_api,
        "settings",
        replace(
            executor_api.settings,
            execution_provider="callwright",
            callwright_live_calls_enabled=False,
        ),
    )
    monkeypatch.setattr(
        executor_api,
        "post_model",
        lambda **_kwargs: verification(
            plan=plan,
            valid=True,
            code=VerificationCode.VALID,
        ),
    )

    response = TestClient(executor_api.app).post("/execute", json=execute_payload(plan))

    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert "disabled" in response.json()["reason"]
    assert spy.requests == []


def test_valid_verification_without_payload_is_an_authority_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_call_plan()
    monkeypatch.setattr(
        executor_api,
        "post_model",
        lambda **_kwargs: GrantVerificationResult(
            valid=True,
            code=VerificationCode.VALID,
            reason="Malformed upstream response.",
            payload=None,
        ),
    )

    response = TestClient(executor_api.app).post("/execute", json=execute_payload(plan))

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "AUTHORITY_INVALID_RESPONSE"


def test_live_client_sends_only_confirmed_vendor_fields_and_is_idempotent() -> None:
    outbound: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        outbound.append(request)
        return httpx.Response(
            200,
            json={"call_id": "call_123", "status": "queued"},
        )

    client = LiveCallwrightClient(
        api_key="pk_live_secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        attempt_store=InMemoryCallwrightAttemptStore(),
    )
    request = make_live_request()

    first = client.create_call(request)
    second = client.create_call(request)

    assert first == second
    assert client._base_url == "https://api.voygr.tech"
    assert len(outbound) == 1
    sent = outbound[0]
    assert sent.method == "POST"
    assert str(sent.url) == "https://api.voygr.tech/calls"
    assert sent.headers["X-API-Key"] == "pk_live_secret"
    assert json.loads(sent.content) == {
        "target_phone": "+12025550100",
        "brief": request.brief,
        "language": "en",
    }
    sent_text = sent.content.decode("utf-8")
    assert request.authorization_id not in sent_text
    assert request.plan_hash not in sent_text


def test_live_client_timeout_is_never_automatically_replayed() -> None:
    outbound_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal outbound_count
        outbound_count += 1
        raise httpx.ReadTimeout("private timeout details", request=request)

    client = LiveCallwrightClient(
        api_key="pk_live_secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        attempt_store=InMemoryCallwrightAttemptStore(),
    )
    request = make_live_request()

    with pytest.raises(CallwrightError, match="automatic replay is disabled"):
        client.create_call(request)
    with pytest.raises(CallwrightError, match="refusing to place another call"):
        client.create_call(request)

    assert outbound_count == 1


def test_live_client_ambiguous_attempt_survives_client_recreation(
    tmp_path: Path,
) -> None:
    attempt_path = tmp_path / "attempts.json"
    outbound_count = 0

    def timeout(request: httpx.Request) -> httpx.Response:
        nonlocal outbound_count
        outbound_count += 1
        raise httpx.ReadTimeout("private timeout details", request=request)

    first_client = LiveCallwrightClient(
        api_key="pk_live_secret",
        http_client=httpx.Client(transport=httpx.MockTransport(timeout)),
        attempt_store=FileCallwrightAttemptStore(attempt_path),
    )
    request = make_live_request()
    with pytest.raises(CallwrightError):
        first_client.create_call(request)

    def must_not_run(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("A persisted ambiguous call must not be replayed")

    recreated = LiveCallwrightClient(
        api_key="pk_live_secret",
        http_client=httpx.Client(transport=httpx.MockTransport(must_not_run)),
        attempt_store=FileCallwrightAttemptStore(attempt_path),
    )
    with pytest.raises(CallwrightError, match="refusing to place another call"):
        recreated.create_call(request)

    assert outbound_count == 1
    assert "+12025550100" not in attempt_path.read_text(encoding="utf-8")
    assert request.brief not in attempt_path.read_text(encoding="utf-8")


def test_live_client_successful_receipt_survives_client_recreation(
    tmp_path: Path,
) -> None:
    attempt_path = tmp_path / "attempts.json"
    request = make_live_request()
    first_client = LiveCallwrightClient(
        api_key="pk_live_secret",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"call_id": "call_123", "status": "queued"},
                )
            )
        ),
        attempt_store=FileCallwrightAttemptStore(attempt_path),
    )
    first = first_client.create_call(request)

    def must_not_run(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("A persisted successful call must not be submitted again")

    recreated = LiveCallwrightClient(
        api_key="pk_live_secret",
        http_client=httpx.Client(transport=httpx.MockTransport(must_not_run)),
        attempt_store=FileCallwrightAttemptStore(attempt_path),
    )

    assert recreated.create_call(request) == first


def test_concurrent_duplicate_live_calls_produce_one_post() -> None:
    entered_handler = Event()
    release_handler = Event()
    counter_lock = Lock()
    outbound_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal outbound_count
        with counter_lock:
            outbound_count += 1
        entered_handler.set()
        assert release_handler.wait(timeout=5)
        return httpx.Response(
            200,
            json={"call_id": "call_123", "status": "queued"},
        )

    client = LiveCallwrightClient(
        api_key="pk_live_secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        attempt_store=InMemoryCallwrightAttemptStore(),
    )
    request = make_live_request()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.create_call, request)
        assert entered_handler.wait(timeout=5)
        second = pool.submit(client.create_call, request)
        with pytest.raises(CallwrightError, match="refusing to place another call"):
            second.result(timeout=5)
        release_handler.set()
        receipt = first.result(timeout=5)

    assert receipt.call_id == "call_123"
    assert outbound_count == 1


@pytest.mark.parametrize(
    ("status_code", "expected_message"),
    [
        (302, "unexpected redirect"),
        (401, "rejected the configured API key"),
        (402, "insufficient credits"),
        (429, "rate-limited"),
        (503, "HTTP 503"),
    ],
)
def test_live_client_errors_are_sanitized(
    status_code: int,
    expected_message: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            text="private-upstream-body pk_live_secret +12025550100",
            headers={"Location": "https://attacker.example/collect"},
        )

    client = LiveCallwrightClient(
        api_key="pk_live_secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        attempt_store=InMemoryCallwrightAttemptStore(),
    )

    with pytest.raises(CallwrightError, match=expected_message) as error:
        client.create_call(make_live_request())

    assert "private-upstream-body" not in str(error.value)
    assert "pk_live_secret" not in str(error.value)
    assert "+12025550100" not in str(error.value)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.voygr.tech",
        "https://attacker.example",
        "https://user:pass@api.voygr.tech",
        "https://api.voygr.tech/other",
        "https://api.voygr.tech?redirect=attacker",
    ],
)
def test_live_client_pins_the_official_api_origin(base_url: str) -> None:
    with pytest.raises(
        CallwrightConfigurationError,
        match="https://api.voygr.tech",
    ):
        LiveCallwrightClient(
            api_key="pk_live_secret",
            base_url=base_url,
            attempt_store=InMemoryCallwrightAttemptStore(),
        )


def test_invalid_phone_number_fails_before_network_access() -> None:
    outbound_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal outbound_count
        outbound_count += 1
        return httpx.Response(500)

    client = LiveCallwrightClient(
        api_key="pk_live_secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        attempt_store=InMemoryCallwrightAttemptStore(),
    )

    with pytest.raises(CallwrightPlanError, match="E.164"):
        client.create_call(make_live_request(allowed_phone="555-0100"))

    assert outbound_count == 0


def test_get_call_returns_redacted_status_without_transcript() -> None:
    outbound: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        outbound.append(request)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "outcome_type": "reservation_available",
                "summary": "The requested time is available.",
                "transcript_full": "Sensitive full transcript must be discarded.",
            },
        )

    client = LiveCallwrightClient(
        api_key="pk_live_secret",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        attempt_store=InMemoryCallwrightAttemptStore(),
    )

    status = client.get_call("call_123")

    assert status.status == "completed"
    assert status.outcome_type == "reservation_available"
    assert status.summary == "The requested time is available."
    assert "transcript" not in status.model_dump()
    assert len(outbound) == 1
    assert outbound[0].method == "GET"
    assert str(outbound[0].url) == "https://api.voygr.tech/calls/call_123"
