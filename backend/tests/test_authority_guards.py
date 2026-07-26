from __future__ import annotations

import pytest
from dragback.authority.engine import IntentAuthority
from dragback.domain import Verdict
from dragback.fixtures import load_decision_v18, load_graph_fixture
from dragback.grants import GrantSigner
from dragback.graph.memory import MemoryGraphStore


def make_authority() -> IntentAuthority:
    version, artifacts, edges, _ = load_graph_fixture()
    graph = MemoryGraphStore()
    graph.reset(version=version, artifacts=artifacts, edges=edges)
    return IntentAuthority(graph=graph, signer=GrantSigner("test-secret"))


def test_empty_affected_scopes_do_not_mutate_or_version_the_graph() -> None:
    authority = make_authority()
    mutation = load_decision_v18()
    mutation.affected_scopes = set()

    result = authority.apply_decision_change(mutation)

    assert result.applied is False
    assert result.verdict is Verdict.HUMAN_REVIEW
    assert authority.graph.version_label == "graph-v17"
    assert all(item.id != "DEC-018" for item in authority.graph.list_artifacts())


@pytest.mark.parametrize(
    "mismatch_source",
    ["decision_extra", "decision_missing", "requirements"],
)
def test_scopes_outside_the_declared_change_are_rejected(mismatch_source: str) -> None:
    authority = make_authority()
    mutation = load_decision_v18()
    if mismatch_source == "decision_extra":
        mutation.decision.scopes.add("export.generation")
    elif mismatch_source == "decision_missing":
        mutation.decision.scopes.clear()
    else:
        mutation.decision.attributes["requirements"]["export.generation"] = {
            "format": "pdf"
        }

    result = authority.apply_decision_change(mutation)

    assert result.applied is False
    assert result.verdict is Verdict.HUMAN_REVIEW
    assert authority.graph.version_label == "graph-v17"
    assert all(item.id != "DEC-018" for item in authority.graph.list_artifacts())


def test_missing_requirement_scope_is_rejected_before_any_graph_mutation() -> None:
    authority = make_authority()
    mutation = load_decision_v18()
    mutation.decision.attributes["requirements"].pop("export.authorization")
    original_artifacts = authority.graph.list_artifacts()
    original_edges = authority.graph.list_edges()

    result = authority.apply_decision_change(mutation)

    assert result.applied is False
    assert result.verdict is Verdict.HUMAN_REVIEW
    assert "exactly match" in result.reason
    assert authority.graph.version_label == "graph-v17"
    assert authority.graph.list_artifacts() == original_artifacts
    assert authority.graph.list_edges() == original_edges
    assert authority.last_report is None


def test_only_a_decision_can_be_a_supersession_target() -> None:
    authority = make_authority()
    mutation = load_decision_v18().model_copy(update={"supersedes_id": "TICKET-100"})

    result = authority.apply_decision_change(mutation)

    assert result.applied is False
    assert result.verdict is Verdict.HUMAN_REVIEW
    assert authority.graph.version_label == "graph-v17"
    assert all(item.id != "DEC-018" for item in authority.graph.list_artifacts())


def test_duplicate_decision_is_idempotently_rejected_without_a_version_bump() -> None:
    authority = make_authority()
    mutation = load_decision_v18()

    first = authority.apply_decision_change(mutation)
    second = authority.apply_decision_change(mutation)

    assert first.applied is True
    assert second.applied is False
    assert second.verdict is Verdict.HUMAN_REVIEW
    assert authority.graph.version_label == "graph-v18"
    assert [item.id for item in authority.graph.list_artifacts()].count("DEC-018") == 1
    assert len(
        [edge for edge in authority.graph.list_edges() if edge.source_id == "DEC-018"]
    ) == 1


def test_omitting_a_required_task_scope_cannot_receive_a_grant() -> None:
    authority = make_authority()
    _, _, _, run = load_graph_fixture()
    incomplete_plan = run.plan.model_copy(deep=True)
    incomplete_plan.actions = [
        action
        for action in incomplete_plan.actions
        if "export.authorization" not in action.scopes
    ]

    result = authority.evaluate_plan(
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=incomplete_plan,
    )

    assert result.verdict is Verdict.REPLAN
    assert result.affected_scopes == {"export.authorization"}
    assert result.grant is None
    assert result.mismatches[0].scope == "export.authorization"


def test_unknown_task_and_mismatched_ticket_are_blocked() -> None:
    authority = make_authority()
    _, _, _, run = load_graph_fixture()

    unknown = authority.evaluate_plan(
        run_id=run.run_id,
        task_id="MISSING",
        plan=run.plan,
    )
    mismatched_plan = run.plan.model_copy(deep=True)
    mismatched_plan.ticket_id = "TASK-102"
    mismatched = authority.evaluate_plan(
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=mismatched_plan,
    )

    assert unknown.verdict is Verdict.BLOCK
    assert unknown.grant is None
    assert mismatched.verdict is Verdict.BLOCK
    assert mismatched.grant is None
