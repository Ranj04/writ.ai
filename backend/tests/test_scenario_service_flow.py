from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urlparse

import dragback.services.agent_api as agent_api
import dragback.services.authority_api as authority_api
import dragback.services.executor_api as executor_api
import dragback.services.support as support
import httpx
import pytest
from dragback.scenarios.authority_contexts import ScenarioAuthorityContextRegistry
from dragback.scenarios.runner import ScenarioRunner
from fastapi.testclient import TestClient

CSV_SCENARIO_ID = "csv-exports-admin-only"


@dataclass
class ScenarioServiceHarness:
    agent: TestClient
    authority: TestClient
    executor: TestClient
    fail_context_creation_for: set[str] = field(default_factory=set)


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> ScenarioServiceHarness:
    authority = TestClient(authority_api.app)
    agent = TestClient(agent_api.app)
    executor = TestClient(executor_api.app)
    harness = ScenarioServiceHarness(
        agent=agent,
        authority=authority,
        executor=executor,
    )

    monkeypatch.setattr(
        authority_api,
        "scenario_contexts",
        ScenarioAuthorityContextRegistry(
            grant_secret="scenario-service-flow-secret",
            grant_ttl_seconds=3600,
            authority_threshold=0.75,
        ),
    )
    monkeypatch.setattr(agent_api, "scenario_runner", ScenarioRunner())

    def route_post(url: str, **kwargs: object) -> httpx.Response:
        parsed = urlparse(url)
        headers = cast(dict[str, str], kwargs.get("headers", {}))
        body = cast(dict[str, object] | None, kwargs.get("json"))
        if (
            parsed.port == 8001
            and parsed.path == "/scenario-lab/authority/contexts"
            and body is not None
            and body.get("scenario_id") in harness.fail_context_creation_for
        ):
            return httpx.Response(503, json={"error": {"message": "injected failure"}})
        if parsed.port == 8001:
            return authority.post(parsed.path, json=body, headers=headers)
        if parsed.port == 8003:
            return executor.post(parsed.path, json=body, headers=headers)
        raise httpx.ConnectError(
            f"Unexpected service URL: {url}",
            request=httpx.Request("POST", url),
        )

    def route_delete(url: str, **kwargs: object) -> httpx.Response:
        parsed = urlparse(url)
        if parsed.port == 8001:
            return authority.delete(
                parsed.path,
                headers=cast(dict[str, str], kwargs.get("headers", {})),
            )
        raise httpx.ConnectError(
            f"Unexpected service URL: {url}",
            request=httpx.Request("DELETE", url),
        )

    monkeypatch.setattr(support.httpx, "post", route_post)
    monkeypatch.setattr(support.httpx, "delete", route_delete)
    return harness


def _assert_no_token(response_body: dict[str, object]) -> None:
    assert "token" not in json.dumps(response_body).casefold()


def test_csv_scenario_crosses_agent_authority_and_executor_services(
    services: ScenarioServiceHarness,
) -> None:
    catalog_response = services.agent.get("/scenario-lab/scenarios")
    assert catalog_response.status_code == 200
    catalog = catalog_response.json()
    assert len(catalog["scenarios"]) == 12
    assert any(item["id"] == CSV_SCENARIO_ID for item in catalog["scenarios"])
    csv_catalog_item = next(
        item for item in catalog["scenarios"] if item["id"] == CSV_SCENARIO_ID
    )
    assert csv_catalog_item["specification"]["id"] == "SPEC-009"
    assert csv_catalog_item["ticket"]["id"] == "TICKET-100"
    assert csv_catalog_item["initial_plan"]["id"] == "PLAN-027"
    assert all(
        action["source"] == "fixture"
        and action["representation"] == "plan-action"
        and action["graph_artifact_id"] is None
        and action["persisted_as_graph_artifact"] is False
        for action in csv_catalog_item["corrective_actions"]
    )
    assert {task["expected_status"] for task in csv_catalog_item["tasks"]} == {
        "preserved",
        "invalidated",
    }
    assert "signed_token" not in csv_catalog_item

    start_response = services.agent.post(
        "/scenario-lab/runs",
        json={"scenario_id": CSV_SCENARIO_ID},
    )
    assert start_response.status_code == 201
    started = start_response.json()
    _assert_no_token(started)
    assert started["active_stage"] == "authorized"
    assert started["graph_version"] == "graph-v17"
    assert started["original_authorization"]["verdict"] == "ALLOW"
    run_id = started["run_id"]
    context_id = started["context_id"]

    changed_response = services.agent.post(f"/scenario-lab/runs/{run_id}/advance")
    assert changed_response.status_code == 200
    changed = changed_response.json()
    _assert_no_token(changed)
    assert changed["active_stage"] == "decision-changed"
    assert changed["graph_version"] == "graph-v18"
    assert changed["invalidation_report"]["changed_decision_id"] == "DEC-018"
    assert changed["invalidation_report"]["invalidated_task_ids"] == [
        "TASK-104",
        "TASK-105",
    ]
    assert changed["outcome_summary"]["needs_review_artifact_ids"] == ["PLAN-027"]
    assert changed["outcome_summary"]["original_plan_status"] == "NEEDS_REVIEW"

    stopped_response = services.agent.post(f"/scenario-lab/runs/{run_id}/advance")
    assert stopped_response.status_code == 200
    stopped = stopped_response.json()
    _assert_no_token(stopped)
    assert stopped["active_stage"] == "work-stopped"
    assert stopped["old_execution"]["applied"] is False
    assert stopped["old_execution"]["verification_code"] == "STALE_SNAPSHOT"
    assert stopped["conflict_authorization"]["verdict"] == "REPLAN"
    assert stopped["agent_loop_state"] == "REPLAN"
    assert (
        stopped["outcome_summary"]["old_grant_verification_code"]
        == "STALE_SNAPSHOT"
    )

    completed_response = services.agent.post(f"/scenario-lab/runs/{run_id}/advance")
    assert completed_response.status_code == 200
    completed = completed_response.json()
    _assert_no_token(completed)
    assert completed["active_stage"] == "reauthorized"
    assert completed["status"] == "passed"
    assert completed["corrected_authorization"]["verdict"] == "ALLOW"
    assert completed["new_execution"]["applied"] is True
    assert completed["new_execution"]["verification_code"] == "VALID"
    assert completed["agent_loop_state"] == "COMPLETE"
    assert completed["evaluation"]["status"] == "passed"
    assert all(check["passed"] for check in completed["evaluation"]["checks"])
    assert completed["outcome_summary"]["replacement_authorization_verdict"] == "ALLOW"
    assert completed["outcome_summary"]["replacement_grant_verification_code"] == "VALID"
    assert completed["outcome_summary"]["may_continue"] is True

    persisted = services.agent.get(f"/scenario-lab/runs/{run_id}")
    assert persisted.status_code == 200
    _assert_no_token(persisted.json())
    results = services.agent.get("/scenario-lab/results").json()["runs"]
    saved = next(item for item in results if item["run_id"] == run_id)
    assert saved["status"] == "passed"
    assert saved["plan_status"] == "NEEDS_REVIEW"
    assert saved["needs_review_artifact_ids"] == ["PLAN-027"]
    assert saved["old_grant_verification_code"] == "STALE_SNAPSHOT"
    assert saved["replacement_authorization_verdict"] == "ALLOW"
    assert saved["replacement_grant_verification_code"] == "VALID"
    assert saved["history_scope"] == "session"

    # Completed runs retain a token-free result view while their authority context is removed.
    cleaned_context = services.authority.get(f"/scenario-lab/authority/contexts/{context_id}")
    assert cleaned_context.status_code == 404


def test_run_all_returns_twelve_service_verified_results(
    services: ScenarioServiceHarness,
) -> None:
    response = services.agent.post("/scenario-lab/run-all", json={})

    assert response.status_code == 200
    report = response.json()
    _assert_no_token(report)
    assert report["completed"] == 12
    assert report["passed"] == 12
    assert report["failed"] == 0
    assert len(report["runs"]) == 12
    assert all(item["old_grant_rejected"] for item in report["runs"])
    assert all(item["reauthorization_succeeded"] for item in report["runs"])
    assert all(item["plan_status"] == "NEEDS_REVIEW" for item in report["runs"])
    assert all(
        item["old_grant_verification_code"] == "STALE_SNAPSHOT"
        for item in report["runs"]
    )
    assert all(
        item["replacement_grant_verification_code"] == "VALID"
        for item in report["runs"]
    )


def test_run_all_continues_after_one_context_creation_failure(
    services: ScenarioServiceHarness,
) -> None:
    failed_id = "payment-provider-unapproved"
    successful_id = "api-read-only"
    services.fail_context_creation_for.add(failed_id)

    response = services.agent.post(
        "/scenario-lab/run-all",
        json={"scenario_ids": [failed_id, successful_id]},
    )

    assert response.status_code == 200
    report = response.json()
    assert report["completed"] == 2
    assert report["failed"] == 1
    assert report["passed"] == 1
    by_scenario = {item["scenario_id"]: item for item in report["runs"]}
    assert by_scenario[failed_id]["status"] == "failed"
    assert by_scenario[failed_id]["inspectable"] is False
    assert by_scenario[failed_id]["plan_status"] is None
    assert by_scenario[failed_id]["history_scope"] == "session"
    assert by_scenario[successful_id]["status"] == "passed"
    assert by_scenario[successful_id]["inspectable"] is True
    assert by_scenario[successful_id]["old_grant_rejected"] is True
    assert by_scenario[successful_id]["reauthorization_succeeded"] is True
