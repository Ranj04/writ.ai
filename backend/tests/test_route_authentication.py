"""The trust boundary, as an enforced inventory rather than a prose claim.

Every mutating route (POST/PUT/PATCH/DELETE) on the three services is listed
below against the guard mechanism that is actually in use on it. The README's
"Where the trust boundary is" section quotes the counts this file produces, and
``test_readme_quotes_the_counts_this_inventory_produces`` reads the README back,
so the two cannot drift apart silently.

Adding a mutating route means choosing a tier here on purpose. ``open`` is a
legitimate tier for the disclosed browser-facing surface, but it has to be
chosen, not inherited.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from writai.fixtures import load_graph_fixture
from writai.services import agent_api, authority_api, executor_api, support

README = Path(__file__).resolve().parents[2] / "README.md"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class Tier(StrEnum):
    INTERNAL_SERVICE = "internal_service"  # require_internal_service HMAC capability
    HUMAN_IDENTITY = "human_identity"  # Hexclave access token resolved to a user
    SIGNATURE = "signature"  # Svix (Hexclave) or Composio (Slack) webhook signature
    SIGNED_LINK = "signed_link"  # single-use signed approval link redeemed once
    BEARER = "bearer"  # CrustData bearer token
    DEMO_GATE = "demo_gate"  # refused unless WRITAI_DEMO_RESET_ENABLED
    OPEN = "open"  # no guard: the disclosed tier


# Frozen: (service, method, path) -> tier. Change it consciously.
ROUTE_TIERS: dict[tuple[str, str, str], Tier] = {
    # --- authority (port 8001) ---
    ("authority", "POST", "/graph/reset"): Tier.DEMO_GATE,
    ("authority", "POST", "/demo/reset"): Tier.DEMO_GATE,
    ("authority", "POST", "/decisions/ingest"): Tier.DEMO_GATE,
    ("authority", "POST", "/webhooks/hexclave"): Tier.SIGNATURE,
    (
        "authority",
        "POST",
        "/internal/live-workspaces/{workspace_id}/decisions/{decision_id}/notifications/email",
    ): Tier.INTERNAL_SERVICE,
    (
        "authority",
        "POST",
        "/internal/live-workspaces/{workspace_id}/decisions/{decision_id}/notifications/push",
    ): Tier.INTERNAL_SERVICE,
    ("authority", "POST", "/approvals/email/redeem"): Tier.SIGNED_LINK,
    ("authority", "POST", "/approvals/push/redeem"): Tier.SIGNED_LINK,
    ("authority", "POST", "/intake/slack/{workspace_id}"): Tier.SIGNATURE,
    ("authority", "POST", "/intake/slack/{workspace_id}/reaction"): Tier.SIGNATURE,
    ("authority", "POST", "/authorize"): Tier.OPEN,
    ("authority", "POST", "/grants/verify"): Tier.OPEN,
    ("authority", "POST", "/scenario-lab/authority/contexts"): Tier.OPEN,
    ("authority", "DELETE", "/scenario-lab/authority/contexts/{context_id}"): Tier.OPEN,
    ("authority", "POST", "/scenario-lab/authority/contexts/{context_id}/mutation"): Tier.OPEN,
    ("authority", "POST", "/scenario-lab/authority/contexts/{context_id}/authorize"): Tier.OPEN,
    (
        "authority",
        "POST",
        "/scenario-lab/authority/contexts/{context_id}/grants/verify",
    ): Tier.OPEN,
    ("authority", "POST", "/live-workspaces/authority/contexts"): Tier.INTERNAL_SERVICE,
    (
        "authority",
        "DELETE",
        "/live-workspaces/authority/contexts/{context_id}",
    ): Tier.INTERNAL_SERVICE,
    (
        "authority",
        "POST",
        "/live-workspaces/authority/contexts/{context_id}/baseline/approve",
    ): Tier.INTERNAL_SERVICE,
    (
        "authority",
        "POST",
        "/live-workspaces/authority/contexts/{context_id}/mutations/approve",
    ): Tier.INTERNAL_SERVICE,
    (
        "authority",
        "POST",
        "/live-workspaces/authority/contexts/{context_id}/authorize",
    ): Tier.INTERNAL_SERVICE,
    (
        "authority",
        "POST",
        "/live-workspaces/authority/contexts/{context_id}/grants/verify",
    ): Tier.INTERNAL_SERVICE,
    # --- agent (port 8002) ---
    ("agent", "POST", "/internal/hexclave/permission-cache/invalidate"): Tier.INTERNAL_SERVICE,
    ("agent", "POST", "/demo/reset"): Tier.DEMO_GATE,
    ("agent", "POST", "/demo/reset-all"): Tier.DEMO_GATE,
    ("agent", "POST", "/demo/start"): Tier.OPEN,
    ("agent", "POST", "/demo/tests-pass"): Tier.OPEN,
    ("agent", "POST", "/demo/recheck"): Tier.OPEN,
    ("agent", "POST", "/demo/replan"): Tier.OPEN,
    ("agent", "POST", "/intake/crustdata/person/capture"): Tier.BEARER,
    ("agent", "POST", "/intake/crustdata/person/replay"): Tier.BEARER,
    ("agent", "POST", "/scenario-lab/runs"): Tier.OPEN,
    ("agent", "POST", "/live-workspaces/import"): Tier.OPEN,
    ("agent", "POST", "/live-workspaces/{workspace_id}/baseline/approve"): Tier.HUMAN_IDENTITY,
    ("agent", "POST", "/live-workspaces/{workspace_id}/authorize"): Tier.OPEN,
    ("agent", "POST", "/live-workspaces/{workspace_id}/decisions/propose"): Tier.OPEN,
    (
        "agent",
        "POST",
        "/internal/live-workspaces/{workspace_id}/decisions/{decision_id}/approve-from-slack",
    ): Tier.INTERNAL_SERVICE,
    (
        "agent",
        "POST",
        "/internal/live-workspaces/{workspace_id}/decisions/{decision_id}"
        "/approve-from-notification",
    ): Tier.INTERNAL_SERVICE,
    (
        "agent",
        "POST",
        "/live-workspaces/{workspace_id}/decisions/{decision_id}/approve",
    ): Tier.HUMAN_IDENTITY,
    ("agent", "DELETE", "/live-workspaces/{workspace_id}/decisions/pending"): Tier.OPEN,
    ("agent", "POST", "/live-workspaces/{workspace_id}/grants/initial/verify"): Tier.OPEN,
    ("agent", "PUT", "/live-workspaces/{workspace_id}/plan"): Tier.OPEN,
    ("agent", "POST", "/live-workspaces/{workspace_id}/reauthorize"): Tier.OPEN,
    ("agent", "POST", "/live-workspaces/{workspace_id}/grants/replacement/verify"): Tier.OPEN,
    ("agent", "POST", "/scenario-lab/runs/{run_id}/advance"): Tier.OPEN,
    ("agent", "POST", "/scenario-lab/scenarios/{scenario_id}/reset"): Tier.OPEN,
    ("agent", "POST", "/scenario-lab/run-all"): Tier.OPEN,
    # --- executor (port 8003) ---
    ("executor", "POST", "/execute"): Tier.INTERNAL_SERVICE,
}

# The numbers the README quotes. If these move, the README section moves with them.
EXPECTED_TOTAL = 49
EXPECTED_TIER_COUNTS: dict[Tier, int] = {
    Tier.INTERNAL_SERVICE: 12,
    Tier.HUMAN_IDENTITY: 2,
    Tier.SIGNATURE: 3,
    Tier.SIGNED_LINK: 2,
    Tier.BEARER: 2,
    Tier.DEMO_GATE: 5,
    Tier.OPEN: 23,
}
EXPECTED_OPEN_BY_SERVICE = {"authority": 7, "agent": 16, "executor": 0}

SERVICES = {
    "authority": authority_api.app,
    "agent": agent_api.app,
    "executor": executor_api.app,
}


def _mutating_routes() -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for service, app in SERVICES.items():
        for route in app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            if not isinstance(path, str):
                continue
            for method in methods & MUTATING_METHODS:
                found.add((service, method, path))
    return found


def test_every_mutating_route_has_a_declared_tier() -> None:
    actual = _mutating_routes()
    declared = set(ROUTE_TIERS)

    undeclared = sorted(actual - declared)
    assert not undeclared, (
        "Mutating routes with no declared trust tier: "
        + ", ".join(f"{s} {m} {p}" for s, m, p in undeclared)
        + ". Adding a route means choosing a tier in ROUTE_TIERS "
        "(backend/tests/test_route_authentication.py) and updating the README "
        "section 'Where the trust boundary is'."
    )
    vanished = sorted(declared - actual)
    assert not vanished, (
        "Declared routes that no longer exist: "
        + ", ".join(f"{s} {m} {p}" for s, m, p in vanished)
        + ". Remove them from ROUTE_TIERS and update the README counts."
    )


def test_tier_counts_are_the_ones_the_readme_quotes() -> None:
    counts = Counter(ROUTE_TIERS.values())
    assert dict(counts) == EXPECTED_TIER_COUNTS, (
        "Tier counts changed; update EXPECTED_TIER_COUNTS and the README section "
        "'Where the trust boundary is' together."
    )
    assert sum(counts.values()) == EXPECTED_TOTAL
    open_by_service = Counter(
        service for (service, _m, _p), tier in ROUTE_TIERS.items() if tier is Tier.OPEN
    )
    assert {
        service: open_by_service.get(service, 0) for service in EXPECTED_OPEN_BY_SERVICE
    } == EXPECTED_OPEN_BY_SERVICE


def test_readme_quotes_the_counts_this_inventory_produces() -> None:
    text = README.read_text(encoding="utf-8")
    assert "## Where the trust boundary is" in text
    section = text.split("## Where the trust boundary is", 1)[1].split("\n## ", 1)[0]
    guarded = EXPECTED_TOTAL - EXPECTED_TIER_COUNTS[Tier.OPEN]
    for fragment in (
        f"{EXPECTED_TOTAL} mutating routes",
        f"{guarded} are authenticated",
        f"{EXPECTED_TIER_COUNTS[Tier.OPEN]} are open",
        f"{EXPECTED_OPEN_BY_SERVICE['authority']} on the authority service",
        f"{EXPECTED_OPEN_BY_SERVICE['agent']} on the agent service",
    ):
        assert fragment in section, (
            f"README 'Where the trust boundary is' no longer says {fragment!r}; "
            "the section and this inventory must quote the same numbers."
        )


# --- positive controls: the three routes guarded in this change refuse anonymous callers ---


def _plan_body() -> dict[str, Any]:
    _, _, _, run = load_graph_fixture()
    return {
        "run_id": run.run_id,
        "task_id": run.ticket_id,
        "plan": run.plan.model_dump(mode="json"),
    }


def _capability() -> str:
    return support.internal_service_token(authority_api.settings.grant_secret)


GUARDED_IN_THIS_CHANGE = [
    pytest.param(
        "authority",
        "/live-workspaces/authority/contexts/ctx-does-not-exist/authorize",
        lambda: _plan_body(),
        id="workspace-authorize",
    ),
    pytest.param(
        "authority",
        "/live-workspaces/authority/contexts/ctx-does-not-exist/grants/verify",
        lambda: {"token": "not-a-grant", **_plan_body()},
        id="workspace-grants-verify",
    ),
    pytest.param(
        "executor",
        "/execute",
        lambda: {"token": "not-a-grant", **_plan_body()},
        id="execute",
    ),
]


@pytest.mark.parametrize(("service", "path", "body"), GUARDED_IN_THIS_CHANGE)
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-header"),
        pytest.param({support.INTERNAL_SERVICE_AUTH_HEADER: "forged"}, id="wrong-token"),
    ],
)
def test_guarded_routes_refuse_callers_without_the_capability(
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    path: str,
    body: Any,
    headers: dict[str, str],
) -> None:
    def never(*_args: object, **_kwargs: object) -> httpx.Response:
        raise AssertionError("a refused request must not reach another service")

    monkeypatch.setattr(support.httpx, "post", never)

    response = TestClient(SERVICES[service]).post(path, json=body(), headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INTERNAL_SERVICE_AUTH_REQUIRED"


@pytest.mark.parametrize(("service", "path", "body"), GUARDED_IN_THIS_CHANGE)
def test_guarded_routes_admit_the_capability(
    monkeypatch: pytest.MonkeyPatch, service: str, path: str, body: Any
) -> None:
    forwarded: dict[str, Any] = {}

    def capture(**kwargs: Any) -> BaseModel:
        forwarded.update(kwargs)
        raise support.ApiError(status_code=503, code="STOP_HERE", message="captured")

    monkeypatch.setattr(executor_api, "post_model", capture)

    response = TestClient(SERVICES[service]).post(
        path, json=body(), headers={support.INTERNAL_SERVICE_AUTH_HEADER: _capability()}
    )

    assert response.status_code != 403
    if service == "executor":
        # The executor passed the guard and reached for grants/verify carrying the
        # same capability, which the now-guarded workspace route demands.
        assert response.json()["error"]["code"] == "STOP_HERE"
        assert forwarded["internal_secret"] == executor_api.settings.grant_secret
    else:
        # Guard first, then lookup: an unknown context is a 404, never a 403.
        assert response.json()["error"]["code"] == "WORKSPACE_CONTEXT_NOT_FOUND"


class _Echo(BaseModel):
    ok: bool


@pytest.mark.parametrize(
    ("internal_secret", "expect_header"),
    [pytest.param(None, False, id="default-omits"), pytest.param("s3cret", True, id="attaches")],
)
def test_post_model_attaches_the_capability_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, internal_secret: str | None, expect_header: bool
) -> None:
    sent: dict[str, Any] = {}

    def fake_post(_url: str, **kwargs: Any) -> httpx.Response:
        sent.update(cast(dict[str, Any], kwargs["headers"]))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(support.httpx, "post", fake_post)

    result = support.post_model(
        url="http://authority.invalid/authorize",
        payload=_Echo(ok=True),
        response_model=_Echo,
        upstream_name="Intent authority",
        upstream_code="AUTHORITY",
        timeout_seconds=1.0,
        internal_secret=internal_secret,
    )

    assert result.ok is True
    assert support.CORRELATION_ID_HEADER in sent
    if expect_header:
        assert sent[support.INTERNAL_SERVICE_AUTH_HEADER] == support.internal_service_token(
            "s3cret"
        )
    else:
        assert support.INTERNAL_SERVICE_AUTH_HEADER not in sent
