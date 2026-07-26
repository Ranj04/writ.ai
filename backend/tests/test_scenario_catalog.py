from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from dragback.authority.engine import IntentAuthority
from dragback.config import settings
from dragback.domain import (
    ApprovalStatus,
    ArtifactKind,
    EdgeKind,
    ValidityStatus,
    Verdict,
)
from dragback.grants import GrantSigner
from dragback.graph.memory import MemoryGraphStore
from dragback.scenarios import (
    SCENARIO_AUTHORITY_POLICY,
    ScenarioDefinition,
    get_scenario,
    list_scenarios,
)
from dragback.scenarios.validation import SCENARIO_AUTHORITY_THRESHOLD
from pydantic import ValidationError

EXPECTED_SCENARIO_IDS = {
    "csv-exports-admin-only",
    "payment-provider-unapproved",
    "us-data-residency",
    "public-launch-canceled",
    "api-read-only",
    "pii-safe-logging",
    "human-approval-required",
    "internal-model-only",
    "pdf-uploads-only",
    "reversible-database-migration",
    "agent-staging-only",
    "delete-derived-user-data",
}


def _payload() -> dict[str, Any]:
    return get_scenario("csv-exports-admin-only").model_dump(mode="python")


def test_catalog_contains_all_requested_scenarios_and_returns_deep_copies() -> None:
    first = list_scenarios()
    second = list_scenarios()

    assert len(first) == 12
    assert {scenario.metadata.id for scenario in first} == EXPECTED_SCENARIO_IDS
    assert len({scenario.metadata.name for scenario in first}) == 12
    assert first[0] is not second[0]
    assert first[0].graph_seed.artifacts[0] is not second[0].graph_seed.artifacts[0]

    first[0].graph_seed.artifacts[0].title = "mutated caller copy"
    assert get_scenario(first[0].metadata.id).graph_seed.artifacts[0].title != (
        "mutated caller copy"
    )

    with pytest.raises(KeyError, match="Unknown scenario: missing"):
        get_scenario("missing")


@pytest.mark.parametrize("scenario_id", sorted(EXPECTED_SCENARIO_IDS))
def test_every_scenario_executes_through_real_authority_logic(scenario_id: str) -> None:
    scenario = get_scenario(scenario_id)
    graph = MemoryGraphStore()
    graph.reset(
        version=scenario.graph_seed.version,
        artifacts=scenario.graph_seed.artifacts,
        edges=scenario.graph_seed.edges,
    )
    authority = IntentAuthority(
        graph=graph,
        signer=GrantSigner(f"scenario-test-secret-{scenario_id}"),
        authority_policy=scenario.authority_policy,
    )
    run = scenario.initial_run

    initial = authority.evaluate_plan(
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=run.plan,
    )
    assert initial.verdict is Verdict.ALLOW
    assert initial.grant is not None

    mutation = authority.apply_decision_change(scenario.mutation)
    assert mutation.applied is True
    assert mutation.graph_version == "graph-v18"
    assert mutation.report is not None

    expected = scenario.expectations
    for task_id in expected.preserved_task_ids:
        assert graph.get_artifact(task_id).validity is ValidityStatus.VALID
        assert task_id in mutation.report.preserved_artifact_ids
    for task_id in expected.invalidated_task_ids:
        assert graph.get_artifact(task_id).validity is ValidityStatus.INVALIDATED
        assert task_id in mutation.report.affected_artifact_ids
    for artifact_id in expected.needs_review_artifact_ids:
        assert graph.get_artifact(artifact_id).validity is ValidityStatus.NEEDS_REVIEW

    old_verification = authority.verify_grant(
        token=initial.grant.token,
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=run.plan,
    )
    assert old_verification.valid is not expected.old_grant_should_be_rejected
    assert "stale" in old_verification.reason.casefold()

    conflict = authority.evaluate_plan(
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=run.plan,
    )
    assert conflict.verdict is expected.conflict_verdict

    corrected = authority.evaluate_plan(
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=scenario.corrected_plan,
    )
    assert (corrected.verdict is Verdict.ALLOW) is expected.corrected_plan_should_be_authorized
    assert corrected.grant is not None
    replacement_verification = authority.verify_grant(
        token=corrected.grant.token,
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=scenario.corrected_plan,
    )
    assert replacement_verification.valid is expected.replacement_grant_should_execute


def test_catalog_policy_covers_every_scenario_mutation() -> None:
    for scenario in list_scenarios():
        role = scenario.mutation.decision.authority_role
        assert role is not None
        for scope in scenario.mutation.affected_scopes:
            assert role in SCENARIO_AUTHORITY_POLICY[scope]
            assert role in scenario.authority_policy[scope]


def test_every_graph_v17_requirement_has_authoritative_decision_provenance() -> None:
    assert SCENARIO_AUTHORITY_THRESHOLD == settings.authority_threshold
    for scenario in list_scenarios():
        specification = next(
            artifact
            for artifact in scenario.graph_seed.artifacts
            if artifact.kind is ArtifactKind.SPECIFICATION
        )
        basis_edges = {
            edge.source_id: edge
            for edge in scenario.graph_seed.edges
            if edge.kind is EdgeKind.BASIS_FOR
            and edge.target_id == specification.id
        }
        requirements_by_scope: dict[str, dict[str, Any]] = {}
        decisions = [
            artifact
            for artifact in scenario.graph_seed.artifacts
            if artifact.kind is ArtifactKind.DECISION
        ]

        for decision in decisions:
            assert decision.approval_status is ApprovalStatus.APPROVED
            assert decision.confidence >= SCENARIO_AUTHORITY_THRESHOLD
            assert decision.authority_role is not None
            requirements = decision.attributes["requirements"]
            assert set(requirements) == decision.scopes
            assert decision.id in basis_edges
            assert basis_edges[decision.id].scopes == decision.scopes
            for scope, requirement in requirements.items():
                assert scope not in requirements_by_scope
                assert decision.authority_role in scenario.authority_policy[scope]
                requirements_by_scope[scope] = requirement

        assert set(requirements_by_scope) == scenario.initial_run.plan.scopes
        superseded = next(
            decision
            for decision in decisions
            if decision.id == scenario.mutation.supersedes_id
        )
        assert superseded.scopes == scenario.mutation.affected_scopes


def test_semantically_sensitive_scenarios_use_truthful_scopes_and_copy() -> None:
    human = get_scenario("human-approval-required")
    assert human.mutation.affected_scopes == {"agent.release_approval"}
    assert "merge or deployment" in human.mutation.decision.text.casefold()
    human_task_scopes = {
        artifact.title: artifact.scopes
        for artifact in human.graph_seed.artifacts
        if artifact.kind is ArtifactKind.TASK
    }
    human_task_ids = {
        artifact.title: artifact.id
        for artifact in human.graph_seed.artifacts
        if artifact.kind is ArtifactKind.TASK
    }
    assert human_task_ids["Merge changes automatically"] == "APPROVAL-TASK-004"
    assert human_task_ids["Deploy automatically"] == "APPROVAL-TASK-005"
    assert human_task_scopes["Merge changes automatically"] == {
        "agent.release_approval"
    }
    assert human_task_scopes["Deploy automatically"] == {"agent.release_approval"}
    assert {
        action.id: action.scopes
        for action in human.corrected_plan.actions
        if action.id == "APPROVAL-ACTION-006"
    } == {"APPROVAL-ACTION-006": {"agent.release_approval"}}

    staging = get_scenario("agent-staging-only")
    assert staging.mutation.affected_scopes == {
        "agent.environment_access.production"
    }
    staging_task_scopes = {
        artifact.title: artifact.scopes
        for artifact in staging.graph_seed.artifacts
        if artifact.kind is ArtifactKind.TASK
    }
    staging_task_ids = {
        artifact.title: artifact.id
        for artifact in staging.graph_seed.artifacts
        if artifact.kind is ArtifactKind.TASK
    }
    assert staging_task_ids["Deploy to staging"] == "STAGING-TASK-003"
    assert staging_task_ids["Deploy to production"] == "STAGING-TASK-004"
    assert staging_task_ids["Read production logs"] == "STAGING-TASK-005"
    assert staging_task_ids["Access production secrets"] == "STAGING-TASK-006"
    assert staging_task_scopes["Deploy to staging"] == {
        "agent.environment_access.staging"
    }
    assert staging_task_scopes["Deploy to production"] == {
        "agent.environment_access.production"
    }
    assert {
        action.id: action.scopes
        for action in staging.corrected_plan.actions
        if action.id == "STAGING-ACTION-007"
    } == {"STAGING-ACTION-007": {"agent.environment_access.production"}}

    residency = get_scenario("us-data-residency")
    residency_tasks = {
        artifact.title: artifact
        for artifact in residency.graph_seed.artifacts
        if artifact.kind is ArtifactKind.TASK
    }
    backup_controls = residency_tasks["Define backup encryption and key controls"]
    assert backup_controls.id == "RESIDENCY-TASK-003"
    assert backup_controls.scopes == {"data.backup.crypto"}
    assert "location" in backup_controls.text.casefold()
    assert residency.corrected_plan.actions[-2].id == "RESIDENCY-ACTION-006"
    assert "backup" in residency.corrected_plan.actions[-2].description.casefold()

    migration = get_scenario("reversible-database-migration")
    migration_tasks = {
        artifact.title: artifact
        for artifact in migration.graph_seed.artifacts
        if artifact.kind is ArtifactKind.TASK
    }
    mapping = migration_tasks["Specify the source-to-target data mapping"]
    assert mapping.id == "MIGRATION-TASK-002"
    assert mapping.scopes == {"migration.mapping"}
    assert "one-way production execution" in mapping.text
    assert migration.corrected_plan.actions[-2].id == "MIGRATION-ACTION-005"
    assert "backfill" in migration.corrected_plan.actions[-2].description.casefold()


def test_plan_cannot_reintroduce_a_graph_invalidated_task() -> None:
    scenario = get_scenario("csv-exports-admin-only")
    graph = MemoryGraphStore()
    graph.reset(
        version=scenario.graph_seed.version,
        artifacts=scenario.graph_seed.artifacts,
        edges=scenario.graph_seed.edges,
    )
    authority = IntentAuthority(
        graph=graph,
        signer=GrantSigner("invalidated-task-reference-test"),
        authority_policy=scenario.authority_policy,
    )
    mutation = authority.apply_decision_change(scenario.mutation)
    assert mutation.applied

    invalidated_task_id = sorted(scenario.expectations.invalidated_task_ids)[0]
    unsafe_action = next(
        action.model_copy(deep=True)
        for action in scenario.initial_run.plan.actions
        if action.attributes.get("task_id") == invalidated_task_id
    )
    unsafe_action.id = "ACTION-UNSAFE-REINTRODUCTION"
    for scope in unsafe_action.scopes:
        requirement = authority.current_requirements().get(scope)
        if requirement:
            unsafe_action.attributes.update(requirement)
    unsafe_plan = scenario.corrected_plan.model_copy(deep=True)
    unsafe_plan.actions.append(unsafe_action)

    result = authority.evaluate_plan(
        run_id=scenario.initial_run.run_id,
        task_id=scenario.initial_run.ticket_id,
        plan=unsafe_plan,
    )

    assert result.verdict is Verdict.REPLAN
    assert result.grant is None
    assert any(
        mismatch.actual.get("validity") == ValidityStatus.INVALIDATED.value
        for mismatch in result.mismatches
    )


def test_expectations_are_task_only_and_separate_from_presentation_copy() -> None:
    for scenario in list_scenarios():
        by_id = {artifact.id: artifact for artifact in scenario.graph_seed.artifacts}
        expected = scenario.expectations
        for task_id in expected.preserved_task_ids | expected.invalidated_task_ids:
            assert by_id[task_id].kind is ArtifactKind.TASK
        assert len(scenario.presentation.preserved_work) == len(expected.preserved_task_ids)
        assert len(scenario.presentation.invalidated_work) == len(expected.invalidated_task_ids)
        assert len(scenario.presentation.newly_required_work) == len(
            expected.newly_required_action_ids
        )


def _duplicate_artifact(payload: dict[str, Any]) -> None:
    payload["graph_seed"]["artifacts"].append(payload["graph_seed"]["artifacts"][0])


def _break_edge_endpoint(payload: dict[str, Any]) -> None:
    payload["graph_seed"]["edges"][0]["target_id"] = "MISSING"


def _remove_specification(payload: dict[str, Any]) -> None:
    specification_ids = {
        artifact["id"]
        for artifact in payload["graph_seed"]["artifacts"]
        if artifact["kind"] is ArtifactKind.SPECIFICATION
    }
    payload["graph_seed"]["artifacts"] = [
        artifact
        for artifact in payload["graph_seed"]["artifacts"]
        if artifact["id"] not in specification_ids
    ]
    payload["graph_seed"]["edges"] = [
        edge
        for edge in payload["graph_seed"]["edges"]
        if edge["source_id"] not in specification_ids
        and edge["target_id"] not in specification_ids
    ]


def _break_plan_ticket(payload: dict[str, Any]) -> None:
    payload["corrected_plan"]["ticket_id"] = "OTHER-TICKET"


def _make_expectation_non_task(payload: dict[str, Any]) -> None:
    payload["expectations"]["preserved_task_ids"] = {
        *payload["expectations"]["preserved_task_ids"],
        payload["initial_run"]["ticket_id"],
    }


def _break_scope_path(payload: dict[str, Any]) -> None:
    invalidated_id = next(iter(payload["expectations"]["invalidated_task_ids"]))
    payload["graph_seed"]["edges"] = [
        edge for edge in payload["graph_seed"]["edges"] if edge["target_id"] != invalidated_id
    ]


def _remove_policy_authority(payload: dict[str, Any]) -> None:
    role = payload["mutation"]["decision"]["authority_role"]
    for scope in payload["mutation"]["affected_scopes"]:
        payload["authority_policy"][scope].discard(role)


def _mention_downstream_id(payload: dict[str, Any]) -> None:
    payload["mutation"]["decision"]["text"] += f" {payload['initial_run']['ticket_id']}"


def _break_mutation_scope(payload: dict[str, Any]) -> None:
    payload["mutation"]["decision"]["scopes"] = set()


def _break_corrected_requirement(payload: dict[str, Any]) -> None:
    changed_requirements = payload["mutation"]["decision"]["attributes"]["requirements"]
    changed_scope = next(iter(changed_requirements))
    requirement_key = next(iter(changed_requirements[changed_scope]))
    action = next(
        item for item in payload["corrected_plan"]["actions"] if changed_scope in item["scopes"]
    )
    action["attributes"][requirement_key] = "wrong"


def _weaken_safety_expectations(payload: dict[str, Any]) -> None:
    payload["expectations"]["old_grant_should_be_rejected"] = False


def _make_baseline_unapproved(payload: dict[str, Any]) -> None:
    payload["graph_seed"]["artifacts"][0]["approval_status"] = ApprovalStatus.PROPOSAL


def _lower_baseline_confidence(payload: dict[str, Any]) -> None:
    payload["graph_seed"]["artifacts"][0]["confidence"] = (
        SCENARIO_AUTHORITY_THRESHOLD - 0.01
    )


def _remove_baseline_role(payload: dict[str, Any]) -> None:
    payload["graph_seed"]["artifacts"][0]["authority_role"] = None


def _make_baseline_unauthorized(payload: dict[str, Any]) -> None:
    companion = next(
        artifact
        for artifact in payload["graph_seed"]["artifacts"]
        if artifact["kind"] is ArtifactKind.DECISION
        and "export.audit" in artifact["scopes"]
    )
    companion["authority_role"] = "product"


def _mismatch_baseline_scopes(payload: dict[str, Any]) -> None:
    payload["graph_seed"]["artifacts"][0]["scopes"].add("export.audit")


def _duplicate_baseline_requirement(payload: dict[str, Any]) -> None:
    original = payload["graph_seed"]["artifacts"][0]
    scope = next(iter(original["scopes"]))
    companion = next(
        artifact
        for artifact in payload["graph_seed"]["artifacts"][1:]
        if artifact["kind"] is ArtifactKind.DECISION
        and original["authority_role"] == artifact["authority_role"]
    )
    companion["scopes"].add(scope)
    companion["attributes"]["requirements"][scope] = original["attributes"][
        "requirements"
    ][scope]


def _remove_baseline_basis_edge(payload: dict[str, Any]) -> None:
    original_id = payload["graph_seed"]["artifacts"][0]["id"]
    payload["graph_seed"]["edges"] = [
        edge
        for edge in payload["graph_seed"]["edges"]
        if not (
            edge["source_id"] == original_id
            and edge["kind"] is EdgeKind.BASIS_FOR
        )
    ]


def _mismatch_baseline_basis_edge_scopes(payload: dict[str, Any]) -> None:
    original_id = payload["graph_seed"]["artifacts"][0]["id"]
    edge = next(
        item
        for item in payload["graph_seed"]["edges"]
        if item["source_id"] == original_id
        and item["kind"] is EdgeKind.BASIS_FOR
    )
    edge["scopes"] = set()


def _remove_baseline_requirement_owner(payload: dict[str, Any]) -> None:
    companion = next(
        artifact
        for artifact in payload["graph_seed"]["artifacts"][1:]
        if artifact["kind"] is ArtifactKind.DECISION
    )
    payload["graph_seed"]["artifacts"].remove(companion)
    payload["graph_seed"]["edges"] = [
        edge
        for edge in payload["graph_seed"]["edges"]
        if edge["source_id"] != companion["id"]
        and edge["target_id"] != companion["id"]
    ]


def _break_initial_companion_requirement(payload: dict[str, Any]) -> None:
    companion = next(
        artifact
        for artifact in payload["graph_seed"]["artifacts"][1:]
        if artifact["kind"] is ArtifactKind.DECISION
    )
    scope = next(iter(companion["scopes"]))
    requirement_key = next(iter(companion["attributes"]["requirements"][scope]))
    action = next(
        item for item in payload["initial_run"]["plan"]["actions"] if scope in item["scopes"]
    )
    action["attributes"][requirement_key] = "wrong"


@pytest.mark.parametrize(
    "corrupt",
    [
        _duplicate_artifact,
        _break_edge_endpoint,
        _remove_specification,
        _break_plan_ticket,
        _make_expectation_non_task,
        _break_scope_path,
        _remove_policy_authority,
        _mention_downstream_id,
        _break_mutation_scope,
        _break_corrected_requirement,
        _weaken_safety_expectations,
        _make_baseline_unapproved,
        _lower_baseline_confidence,
        _remove_baseline_role,
        _make_baseline_unauthorized,
        _mismatch_baseline_scopes,
        _duplicate_baseline_requirement,
        _remove_baseline_basis_edge,
        _mismatch_baseline_basis_edge_scopes,
        _remove_baseline_requirement_owner,
        _break_initial_companion_requirement,
    ],
    ids=[
        "unique-artifact-ids",
        "edge-endpoints",
        "single-specification",
        "plan-ticket",
        "task-only-expectations",
        "scope-continuous-path",
        "role-policy",
        "no-direct-id-mention",
        "mutation-scope-coverage",
        "corrected-plan-requirements",
        "mandatory-safety-expectations",
        "baseline-approved",
        "baseline-confidence",
        "baseline-role",
        "baseline-authority",
        "baseline-scope-requirements",
        "unique-baseline-requirement",
        "baseline-provenance",
        "baseline-provenance-scopes",
        "baseline-requirement-owner",
        "initial-plan-all-baseline-requirements",
    ],
)
def test_definition_validator_rejects_broken_contracts(
    corrupt: Callable[[dict[str, Any]], None],
) -> None:
    payload = _payload()
    corrupt(payload)

    with pytest.raises((ValueError, ValidationError)):
        ScenarioDefinition.model_validate(payload)
