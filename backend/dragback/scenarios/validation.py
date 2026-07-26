from __future__ import annotations

from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any

from dragback.config import settings
from dragback.domain import (
    AgentPlan,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    EdgeKind,
    Verdict,
)
from dragback.provenance import AUTHORITY_DOWNSTREAM_EDGE_KINDS

if TYPE_CHECKING:
    from dragback.scenarios.models import ScenarioDefinition


SCENARIO_AUTHORITY_THRESHOLD = settings.authority_threshold


def _requirements(artifact: Artifact, *, label: str) -> dict[str, dict[str, Any]]:
    raw = artifact.attributes.get("requirements")
    if not isinstance(raw, dict) or any(
        not isinstance(scope, str) or not isinstance(value, dict) for scope, value in raw.items()
    ):
        raise ValueError(f"{label} must contain requirements keyed by scope")
    return raw


def _validate_authoritative_decision(
    decision: Artifact,
    *,
    authority_policy: dict[str, set[str]],
    label: str,
) -> dict[str, dict[str, Any]]:
    if decision.kind is not ArtifactKind.DECISION:
        raise ValueError(f"{label} must be a Decision artifact")
    if decision.approval_status is not ApprovalStatus.APPROVED:
        raise ValueError(f"{label} must be approved")
    if decision.confidence < SCENARIO_AUTHORITY_THRESHOLD:
        raise ValueError(f"{label} confidence must meet the authority threshold")
    if not decision.scopes:
        raise ValueError(f"{label} must declare scopes")
    requirements = _requirements(decision, label=label)
    if set(requirements) != decision.scopes:
        raise ValueError(f"{label} requirements must exactly match its scopes")
    role = decision.authority_role
    if role is None:
        raise ValueError(f"{label} must declare an authority role")
    unauthorized = {
        scope
        for scope in decision.scopes
        if role not in authority_policy.get(scope, set())
    }
    if unauthorized:
        raise ValueError(
            f"{label} authority role {role!r} is not allowed for scopes: "
            f"{sorted(unauthorized)}"
        )
    return requirements


def _validate_plan_against_requirements(
    plan: AgentPlan,
    *,
    ticket: Artifact,
    requirements: dict[str, dict[str, Any]],
    label: str,
) -> None:
    required_scopes = set(requirements) & ticket.scopes
    missing_scopes = required_scopes - plan.scopes
    if missing_scopes:
        raise ValueError(f"{label} is missing required scopes: {sorted(missing_scopes)}")

    for action in plan.actions:
        for scope in action.scopes & set(requirements):
            expected = requirements[scope]
            actual = {key: action.attributes.get(key) for key in expected}
            if actual != expected:
                raise ValueError(
                    f"{label} action {action.id} does not satisfy {scope}: "
                    f"expected {expected}, got {actual}"
                )


def _has_scope_continuous_path(
    *,
    source_id: str,
    target_id: str,
    artifacts: dict[str, Artifact],
    outgoing: dict[str, list[str]],
    affected_scopes: set[str],
    allow_disjoint_target: bool,
) -> bool:
    queue: deque[str] = deque([source_id])
    visited = {source_id}
    while queue:
        current = queue.popleft()
        for child_id in outgoing[current]:
            if child_id in visited:
                continue
            child = artifacts[child_id]
            intersects = bool(child.scopes & affected_scopes)
            if child_id == target_id and (intersects or allow_disjoint_target):
                return True
            if not intersects:
                continue
            visited.add(child_id)
            queue.append(child_id)
    return False


def validate_scenario_definition(definition: ScenarioDefinition) -> None:
    """Validate invariants needed to execute a definition through the real engine."""

    artifacts = definition.graph_seed.artifacts
    artifact_ids = [artifact.id for artifact in artifacts]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise ValueError("scenario graph artifact IDs must be unique")
    by_id = {artifact.id: artifact for artifact in artifacts}
    specifications = [
        artifact for artifact in artifacts if artifact.kind is ArtifactKind.SPECIFICATION
    ]
    if len(specifications) != 1:
        raise ValueError("scenario graph must contain exactly one Specification artifact")
    baseline_decisions = [
        artifact for artifact in artifacts if artifact.kind is ArtifactKind.DECISION
    ]
    if not baseline_decisions:
        raise ValueError("scenario graph must contain authoritative baseline Decisions")
    baseline_requirements: dict[str, dict[str, Any]] = {}
    for decision in baseline_decisions:
        requirements = _validate_authoritative_decision(
            decision,
            authority_policy=definition.authority_policy,
            label=f"baseline decision {decision.id}",
        )
        duplicate_scopes = set(requirements) & set(baseline_requirements)
        if duplicate_scopes:
            raise ValueError(
                "graph-v17 must contain exactly one active requirement per scope: "
                f"{sorted(duplicate_scopes)}"
            )
        baseline_requirements.update(requirements)

    edge_keys = [
        (edge.source_id, edge.kind, edge.target_id) for edge in definition.graph_seed.edges
    ]
    if len(edge_keys) != len(set(edge_keys)):
        raise ValueError("scenario graph edges must be unique")
    for edge in definition.graph_seed.edges:
        if edge.source_id not in by_id or edge.target_id not in by_id:
            raise ValueError(
                f"edge endpoints must exist in the seed: {edge.source_id} -> {edge.target_id}"
            )
    specification_id = specifications[0].id
    basis_edges = {
        edge.source_id: edge
        for edge in definition.graph_seed.edges
        if edge.kind is EdgeKind.BASIS_FOR and edge.target_id == specification_id
    }
    missing_baseline_paths = {
        decision.id for decision in baseline_decisions if decision.id not in basis_edges
    }
    if missing_baseline_paths:
        raise ValueError(
            "every baseline Decision must directly support the Specification: "
            f"{sorted(missing_baseline_paths)}"
        )
    mismatched_basis_scopes = {
        decision.id
        for decision in baseline_decisions
        if basis_edges[decision.id].scopes != decision.scopes
    }
    if mismatched_basis_scopes:
        raise ValueError(
            "baseline BASIS_FOR edge scopes must match their Decision scopes: "
            f"{sorted(mismatched_basis_scopes)}"
        )

    run = definition.initial_run
    ticket = by_id.get(run.ticket_id)
    if ticket is None or ticket.kind is not ArtifactKind.TICKET:
        raise ValueError("initial run must reference a Ticket artifact")
    if set(baseline_requirements) != ticket.scopes:
        raise ValueError(
            "graph-v17 must have exactly one authoritative requirement for every ticket scope"
        )
    if run.plan.ticket_id != run.ticket_id:
        raise ValueError("initial plan and run must reference the same ticket")
    if definition.corrected_plan.ticket_id != run.ticket_id:
        raise ValueError("corrected plan must reference the initial run ticket")
    if definition.corrected_plan.id == run.plan.id:
        raise ValueError("corrected plan must have a new plan ID")

    plan_artifact = by_id.get(run.plan.id)
    if plan_artifact is None or plan_artifact.kind is not ArtifactKind.AGENT_PLAN:
        raise ValueError("initial plan must have a matching AgentPlan graph artifact")
    if plan_artifact.scopes != run.plan.scopes:
        raise ValueError("initial plan artifact scopes must match the executable plan")

    for label, plan in (("initial plan", run.plan), ("corrected plan", definition.corrected_plan)):
        action_ids = [action.id for action in plan.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError(f"{label} action IDs must be unique")
        if any(not action.scopes for action in plan.actions):
            raise ValueError(f"{label} actions must declare scopes")

    mutation = definition.mutation
    decision = mutation.decision
    if decision.id in by_id:
        raise ValueError("new decision ID must not already exist in the seed")
    if decision.kind is not ArtifactKind.DECISION:
        raise ValueError("scenario mutation must contain a Decision")
    if decision.approval_status is not ApprovalStatus.APPROVED:
        raise ValueError("scenario mutation decision must be approved")
    if decision.confidence < SCENARIO_AUTHORITY_THRESHOLD:
        raise ValueError("scenario mutation confidence must meet the authority threshold")
    if not mutation.affected_scopes:
        raise ValueError("scenario mutation must declare affected scopes")
    if decision.scopes != mutation.affected_scopes:
        raise ValueError("new decision scopes must equal affected scopes")

    superseded = by_id.get(mutation.supersedes_id)
    if superseded is None or superseded.kind is not ArtifactKind.DECISION:
        raise ValueError("supersession target must be a seeded Decision")
    if mutation.affected_scopes != superseded.scopes:
        raise ValueError(
            "the superseded baseline Decision must contain exactly the affected scopes"
        )

    old_requirements = _requirements(superseded, label="superseded decision")
    new_requirements = _requirements(decision, label="new decision")
    if set(new_requirements) != mutation.affected_scopes:
        raise ValueError("new decision must define one requirement for every affected scope")
    if not set(old_requirements) >= mutation.affected_scopes:
        raise ValueError("superseded decision must define every changed requirement")

    _validate_authoritative_decision(
        decision,
        authority_policy=definition.authority_policy,
        label="new decision",
    )

    downstream_ids = [
        artifact.id
        for artifact in artifacts
        if artifact.kind in {ArtifactKind.TICKET, ArtifactKind.TASK}
    ]
    normalized_decision = f"{decision.title}\n{decision.text}".casefold()
    mentioned_ids = [
        artifact_id
        for artifact_id in downstream_ids
        if artifact_id.casefold() in normalized_decision
    ]
    if mentioned_ids:
        raise ValueError(
            "new decision must not directly mention downstream ticket/task IDs: "
            f"{sorted(mentioned_ids)}"
        )

    expected = definition.expectations
    if (
        expected.conflict_verdict is not Verdict.REPLAN
        or not expected.old_grant_should_be_rejected
        or not expected.corrected_plan_should_be_authorized
        or not expected.replacement_grant_should_execute
    ):
        raise ValueError(
            "Scenario Lab expectations must require REPLAN, stale-grant rejection, "
            "corrected authorization, and replacement execution"
        )
    expected_task_ids = expected.preserved_task_ids | expected.invalidated_task_ids
    if expected.preserved_task_ids & expected.invalidated_task_ids:
        raise ValueError("preserved and invalidated task expectations must not overlap")
    for artifact_id in expected_task_ids:
        artifact = by_id.get(artifact_id)
        if artifact is None or artifact.kind is not ArtifactKind.TASK:
            raise ValueError(f"expected task ID must reference a Task artifact: {artifact_id}")
    for artifact_id in expected.needs_review_artifact_ids:
        if artifact_id not in by_id:
            raise ValueError(f"needs-review expectation is not seeded: {artifact_id}")

    seeded_task_ids = {artifact.id for artifact in artifacts if artifact.kind is ArtifactKind.TASK}
    if expected_task_ids != seeded_task_ids:
        raise ValueError("scenario expectations must classify every seeded Task")

    affected_scopes = mutation.affected_scopes
    for task_id in expected.preserved_task_ids:
        if by_id[task_id].scopes & affected_scopes:
            raise ValueError(f"preserved task intersects changed scopes: {task_id}")
    for task_id in expected.invalidated_task_ids:
        task = by_id[task_id]
        if not task.scopes or not task.scopes <= affected_scopes:
            raise ValueError(f"invalidated task must be fully covered by changed scopes: {task_id}")
    for artifact_id in expected.needs_review_artifact_ids:
        artifact = by_id[artifact_id]
        intersection = artifact.scopes & affected_scopes
        if not intersection or intersection >= artifact.scopes:
            raise ValueError(
                f"needs-review artifact must have partial scope overlap: {artifact_id}"
            )

    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in definition.graph_seed.edges:
        if edge.kind in AUTHORITY_DOWNSTREAM_EDGE_KINDS:
            outgoing[edge.source_id].append(edge.target_id)
    for task_id in expected.invalidated_task_ids:
        if not _has_scope_continuous_path(
            source_id=superseded.id,
            target_id=task_id,
            artifacts=by_id,
            outgoing=outgoing,
            affected_scopes=affected_scopes,
            allow_disjoint_target=False,
        ):
            raise ValueError(f"no scope-continuous invalidation path reaches {task_id}")
    for task_id in expected.preserved_task_ids:
        if not _has_scope_continuous_path(
            source_id=superseded.id,
            target_id=task_id,
            artifacts=by_id,
            outgoing=outgoing,
            affected_scopes=affected_scopes,
            allow_disjoint_target=True,
        ):
            raise ValueError(f"no provenance path reaches preserved task {task_id}")

    initial_action_ids = {action.id for action in run.plan.actions}
    corrected_action_ids = {action.id for action in definition.corrected_plan.actions}
    if not expected.newly_required_action_ids <= corrected_action_ids - initial_action_ids:
        raise ValueError("newly required action IDs must be corrected-plan-only actions")

    initial_task_refs = {
        str(action.attributes["task_id"])
        for action in run.plan.actions
        if "task_id" in action.attributes
    }
    corrected_task_refs = {
        str(action.attributes["task_id"])
        for action in definition.corrected_plan.actions
        if "task_id" in action.attributes
    }
    if not expected_task_ids <= initial_task_refs:
        raise ValueError("every expected task must be represented in the initial plan")
    if not expected.preserved_task_ids <= corrected_task_refs:
        raise ValueError("corrected plan must retain every preserved task")
    if expected.invalidated_task_ids & corrected_task_refs:
        raise ValueError("corrected plan must not retain invalidated task actions")

    _validate_plan_against_requirements(
        run.plan,
        ticket=ticket,
        requirements=baseline_requirements,
        label="initial plan",
    )
    current_requirements = {
        scope: requirement
        for scope, requirement in baseline_requirements.items()
        if scope not in affected_scopes
    }
    current_requirements.update(new_requirements)
    _validate_plan_against_requirements(
        definition.corrected_plan,
        ticket=ticket,
        requirements=current_requirements,
        label="corrected plan",
    )
