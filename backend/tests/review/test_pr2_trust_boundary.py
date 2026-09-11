"""The trust-boundary sentence, enforced from both sides of the demo gate.

Sol's round-1 finding: ``POST /decisions/ingest`` accepts a caller-asserted APPROVED
``DecisionMutation`` whenever ``demo_reset_enabled`` is true, and the open ``/authorize``
route then signs any plan that matches the requirement the caller just chose. The
seam is deliberate and stays; what changed is the README sentence that claimed the
open routes cannot create authority. These two tests run the same two-step chain
against both states of the gate and read the README back, so the section and the
boundary it describes cannot disagree silently.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from writai import config
from writai.fixtures import load_decision_v18, load_graph_fixture
from writai.services import authority_api

README = Path(__file__).resolve().parents[3] / "README.md"

# The README sentences these tests enforce, quoted from "Where the trust boundary is".
README_CLAIM_WHEN_CLOSED = (
    "On a deployment where demo reset is disabled they cannot create authority "
    "a human did not grant."
)
README_DISCLOSURE_WHEN_OPEN = "On the default development configuration they can:"
README_GATE_OPEN_BY_DEFAULT = "The gate is open by default in development"
README_INGEST_IS_AUTHORITY_CREATING = "The ingest route is authority-creating"


def _trust_boundary_section() -> str:
    """The section with its hard wraps folded, so a sentence can be quoted whole."""
    text = README.read_text(encoding="utf-8")
    assert "## Where the trust boundary is" in text
    section = text.split("## Where the trust boundary is", 1)[1].split("\n## ", 1)[0]
    return " ".join(section.split())


@pytest.fixture
def clean_authority_runtime() -> Iterator[None]:
    """Start from the fixture graph and leave no attacker decision behind for other tests."""
    authority_api.runtime.reset()
    yield
    authority_api.runtime.reset()


def _attacker_chain(client: TestClient) -> tuple[Any, Any]:
    """Sol's attack: ingest a self-approved requirement, then authorize a plan that matches it.

    Step 2 is what a casual probe misses. A plan that does not satisfy the ingested
    requirement gets ``REPLAN`` and no grant; the plan has to match.
    """
    mutation = load_decision_v18().model_copy(deep=True)
    mutation.decision.attributes["requirements"]["export.authorization"] = {
        "audience": "attacker_chosen"
    }
    ingest = client.post("/decisions/ingest", json=mutation.model_dump(mode="json"))

    _, _, _, run = load_graph_fixture()
    plan = run.plan.model_copy(deep=True)
    authorization_action = next(
        action for action in plan.actions if "export.authorization" in action.scopes
    )
    authorization_action.attributes["audience"] = "attacker_chosen"
    authorize = client.post(
        "/authorize",
        json={
            "run_id": "RUN-ATTACKER",
            "task_id": run.ticket_id,
            "plan": plan.model_dump(mode="json"),
        },
    )
    return ingest, authorize


def test_anonymous_demo_ingest_cannot_mint_authority_when_demo_ingest_is_disabled(
    clean_authority_runtime: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented open surface must not mint a grant from caller-asserted approval.

    This is the property the README claims, and it holds only where demo reset is
    disabled: ``WRITAI_ENV=production`` or any environment outside the demo set.
    """
    section = _trust_boundary_section()
    assert README_CLAIM_WHEN_CLOSED in section, (
        "README 'Where the trust boundary is' no longer states the closed-gate claim "
        "this test enforces; the section and this test must move together."
    )
    assert "production" not in config.DEMO_ENVIRONMENTS
    assert config._default_demo_reset_enabled("production", "memory") is False
    monkeypatch.setattr(
        authority_api,
        "settings",
        replace(authority_api.settings, demo_reset_enabled=False),
    )
    client = TestClient(authority_api.app)
    graph_version = authority_api.runtime.graph.version_label

    ingest, authorize = _attacker_chain(client)

    assert ingest.status_code == 403, (
        "An anonymous caller supplied its own APPROVED decision and the demo gate "
        "accepted it as authority."
    )
    assert ingest.json()["error"]["code"] == "FIXTURE_INGEST_DISABLED"
    assert authority_api.runtime.graph.version_label == graph_version
    assert authorize.status_code == 200
    assert authorize.json()["verdict"] != "ALLOW"
    assert authorize.json().get("grant") is None, (
        "The caller-asserted decision produced a signed grant for the "
        "attacker-chosen audience."
    )


def test_anonymous_demo_ingest_can_mint_authority_while_demo_ingest_is_open(
    clean_authority_runtime: None,
) -> None:
    """Pin the disclosed exposure so it cannot widen, and so closing it updates the README.

    On the default configuration (``WRITAI_ENV=development``, memory backend) the gate
    is open, and the chain returns a real signed grant. If this test goes red because
    someone closed the seam, the README sentence quoted below must change with it.
    """
    section = _trust_boundary_section()
    for fragment in (
        README_DISCLOSURE_WHEN_OPEN,
        README_GATE_OPEN_BY_DEFAULT,
        README_INGEST_IS_AUTHORITY_CREATING,
    ):
        assert fragment in section, (
            f"README 'Where the trust boundary is' no longer says {fragment!r}; "
            "the disclosed exposure and this test must move together."
        )
    assert config._default_demo_reset_enabled("development", "memory") is True
    assert authority_api.settings.demo_reset_enabled is True, (
        "This test pins the default development configuration; run it without "
        "WRITAI_ENV or WRITAI_DEMO_RESET_ENABLED overrides."
    )
    client = TestClient(authority_api.app)
    assert client.post("/demo/reset").status_code == 200

    ingest, authorize = _attacker_chain(client)

    assert ingest.status_code == 200
    assert ingest.json()["applied"] is True
    assert authorize.status_code == 200
    body = authorize.json()
    assert body["verdict"] == "ALLOW"
    grant = body.get("grant")
    assert grant is not None, (
        "The demo seam no longer mints a grant from a caller-asserted decision. "
        "That is a deliberate change: update 'Where the trust boundary is' and "
        "outputs/OPEN-ITEMS-REGISTER.md P2-2 in the same commit."
    )
    assert grant["payload"]["run_id"] == "RUN-ATTACKER"
    assert grant["payload"]["verdict"] == "ALLOW"
    assert grant["token"]
