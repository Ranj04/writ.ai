from __future__ import annotations

import json

import pytest
from dragback.domain import Verdict, VerificationCode
from dragback.scenarios import get_scenario
from dragback.scenarios.authority_contexts import ScenarioAuthorityContextRegistry
from dragback.services import authority_api
from fastapi.testclient import TestClient

SCENARIO_ID = "csv-exports-admin-only"
CONTEXT_A = "context-isolation-a"
CONTEXT_B = "context-isolation-b"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    registry = ScenarioAuthorityContextRegistry(
        grant_secret="scenario-context-test-secret",
        grant_ttl_seconds=300,
        authority_threshold=0.75,
    )
    monkeypatch.setattr(authority_api, "scenario_contexts", registry)
    return TestClient(authority_api.app)


def create_context(client: TestClient, context_id: str) -> dict[str, object]:
    response = client.post(
        "/scenario-lab/authority/contexts",
        json={"context_id": context_id, "scenario_id": SCENARIO_ID},
    )
    assert response.status_code == 201
    return response.json()


def authorization_body() -> dict[str, object]:
    run = get_scenario(SCENARIO_ID).initial_run
    return {
        "run_id": run.run_id,
        "task_id": run.ticket_id,
        "plan": run.plan.model_dump(mode="json"),
    }


def test_context_mutation_is_isolated_from_other_contexts_and_canonical_graph(
    client: TestClient,
) -> None:
    scenario = get_scenario(SCENARIO_ID)
    canonical_version = authority_api.runtime.graph.version_label
    initial_version = f"graph-v{scenario.graph_seed.version}"
    changed_version = f"graph-v{scenario.graph_seed.version + 1}"
    state_a = create_context(client, CONTEXT_A)
    state_b = create_context(client, CONTEXT_B)

    assert state_a["graph_version"] == initial_version
    assert state_b["graph_version"] == initial_version
    assert state_a["last_report"] is None
    assert state_b["last_report"] is None

    mutation = client.post(
        f"/scenario-lab/authority/contexts/{CONTEXT_A}/mutation",
        json={},
    )
    assert mutation.status_code == 200
    assert mutation.json()["applied"] is True

    changed_a = client.get(
        f"/scenario-lab/authority/contexts/{CONTEXT_A}"
    ).json()
    unchanged_b = client.get(
        f"/scenario-lab/authority/contexts/{CONTEXT_B}"
    ).json()
    assert changed_a["graph_version"] == changed_version
    assert changed_a["last_report"]["changed_decision_id"] == scenario.mutation.decision.id
    assert changed_a["last_report"]["preserved_task_ids"] == [
        "TASK-101",
        "TASK-102",
        "TASK-103",
    ]
    assert changed_a["last_report"]["invalidated_task_ids"] == [
        "TASK-104",
        "TASK-105",
    ]
    assert changed_a["last_report"]["needs_review_artifact_ids"] == [
        "SPEC-009",
        "TICKET-100",
        "PLAN-027",
    ]
    assert unchanged_b["graph_version"] == initial_version
    assert unchanged_b["last_report"] is None
    assert authority_api.runtime.graph.version_label == canonical_version


def test_grants_are_context_bound_without_token_storage(client: TestClient) -> None:
    create_context(client, CONTEXT_A)
    create_context(client, CONTEXT_B)
    authorization = client.post(
        f"/scenario-lab/authority/contexts/{CONTEXT_A}/authorize",
        json=authorization_body(),
    )
    assert authorization.status_code == 200
    assert authorization.json()["verdict"] == Verdict.ALLOW.value
    token = authorization.json()["grant"]["token"]

    cross_context = client.post(
        f"/scenario-lab/authority/contexts/{CONTEXT_B}/grants/verify",
        json={**authorization_body(), "token": token},
    )
    assert cross_context.status_code == 200
    assert cross_context.json()["valid"] is False
    assert cross_context.json()["code"] == VerificationCode.INVALID_TOKEN.value

    mutation = client.post(
        f"/scenario-lab/authority/contexts/{CONTEXT_A}/mutation",
        json={},
    )
    assert mutation.status_code == 200
    stale = client.post(
        f"/scenario-lab/authority/contexts/{CONTEXT_A}/grants/verify",
        json={**authorization_body(), "token": token},
    )
    assert stale.status_code == 200
    assert stale.json()["valid"] is False
    assert stale.json()["code"] == VerificationCode.STALE_SNAPSHOT.value

    stored_state = client.get(
        f"/scenario-lab/authority/contexts/{CONTEXT_A}"
    ).json()
    assert token not in json.dumps(stored_state)
    assert "grant_token" not in stored_state


def test_duplicate_mutation_and_context_creation_return_safe_conflicts(
    client: TestClient,
) -> None:
    create_context(client, CONTEXT_A)

    duplicate_context = client.post(
        "/scenario-lab/authority/contexts",
        json={"context_id": CONTEXT_A, "scenario_id": SCENARIO_ID},
    )
    assert duplicate_context.status_code == 409
    assert duplicate_context.json()["error"]["code"] == "SCENARIO_CONTEXT_CONFLICT"

    first_mutation = client.post(
        f"/scenario-lab/authority/contexts/{CONTEXT_A}/mutation",
        json={},
    )
    assert first_mutation.status_code == 200
    duplicate_mutation = client.post(
        f"/scenario-lab/authority/contexts/{CONTEXT_A}/mutation",
        json={},
    )
    assert duplicate_mutation.status_code == 409
    assert duplicate_mutation.json()["error"]["code"] == "SCENARIO_CONTEXT_CONFLICT"


def test_context_cleanup_and_unknown_ids_return_safe_not_found(
    client: TestClient,
) -> None:
    create_context(client, CONTEXT_A)

    deleted = client.delete(
        f"/scenario-lab/authority/contexts/{CONTEXT_A}"
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    missing_context = client.get(
        f"/scenario-lab/authority/contexts/{CONTEXT_A}"
    )
    assert missing_context.status_code == 404
    assert missing_context.json()["error"]["code"] == "SCENARIO_CONTEXT_NOT_FOUND"

    missing_scenario = client.post(
        "/scenario-lab/authority/contexts",
        json={
            "context_id": "context-unknown-scenario",
            "scenario_id": "not-a-real-scenario",
        },
    )
    assert missing_scenario.status_code == 404
    assert missing_scenario.json()["error"]["code"] == "SCENARIO_NOT_FOUND"
