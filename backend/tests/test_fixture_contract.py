from dragback.fixtures import load_decision_v18, load_graph_fixture


def test_presentation_fixture_ids_and_story_are_frozen() -> None:
    version, artifacts, edges, run = load_graph_fixture()
    decision = load_decision_v18()

    assert version == 17
    assert [artifact.id for artifact in artifacts] == [
        "DEC-004",
        "SPEC-009",
        "TICKET-100",
        "TASK-101",
        "TASK-102",
        "PLAN-027",
    ]
    assert [
        (edge.source_id, edge.kind.value, edge.target_id)
        for edge in edges
    ] == [
        ("DEC-004", "BASIS_FOR", "SPEC-009"),
        ("SPEC-009", "CREATES", "TICKET-100"),
        ("TICKET-100", "DECOMPOSES_TO", "TASK-101"),
        ("TICKET-100", "DECOMPOSES_TO", "TASK-102"),
        ("TASK-101", "CURRENTLY_DRIVES", "PLAN-027"),
        ("TASK-102", "CURRENTLY_DRIVES", "PLAN-027"),
    ]
    assert run.run_id == "RUN-27"
    assert run.ticket_id == "TICKET-100"
    assert run.plan.id == "PLAN-027"
    assert [action.id for action in run.plan.actions] == ["ACTION-1", "ACTION-2"]
    assert decision.decision.id == "DEC-018"
    assert decision.supersedes_id == "DEC-004"
    assert decision.affected_scopes == {"export.authorization"}
    assert "TICKET-100" not in decision.decision.text
