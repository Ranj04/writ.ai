from dragback.authority.engine import IntentAuthority
from dragback.domain import Artifact, ArtifactKind, Edge, EdgeKind, ValidityStatus
from dragback.fixtures import load_decision_v18, load_graph_fixture
from dragback.grants import GrantSigner
from dragback.graph.memory import MemoryGraphStore


def test_selective_multi_hop_invalidation() -> None:
    version, artifacts, edges, _ = load_graph_fixture()
    graph = MemoryGraphStore()
    graph.reset(version=version, artifacts=artifacts, edges=edges)
    authority = IntentAuthority(graph=graph, signer=GrantSigner("test-secret"))

    result = authority.apply_decision_change(load_decision_v18())

    assert result.applied is True
    assert result.graph_version == "graph-v18"
    assert result.report is not None
    assert graph.get_artifact("TASK-101").validity is ValidityStatus.VALID
    assert graph.get_artifact("TASK-102").validity is ValidityStatus.INVALIDATED
    assert graph.get_artifact("PLAN-027").validity is ValidityStatus.NEEDS_REVIEW
    assert "TASK-101" in result.report.preserved_artifact_ids
    assert "TASK-102" in result.report.affected_artifact_ids
    assert result.report.upstream_chain_artifact_ids == [
        "DEC-018",
        "DEC-004",
        "SPEC-009",
        "TICKET-100",
    ]
    assert result.report.preserved_task_ids == ["TASK-101"]
    assert result.report.invalidated_task_ids == ["TASK-102"]
    assert result.report.needs_review_artifact_ids == [
        "DEC-004",
        "SPEC-009",
        "TICKET-100",
        "PLAN-027",
    ]
    assert result.report.stopped_work_artifact_ids == ["TASK-102", "PLAN-027"]
    assert result.report.directly_mentioned_artifact_ids == []
    task_path = next(
        path.node_ids for path in result.report.paths if path.artifact_id == "TASK-102"
    )
    assert task_path == ["DEC-018", "DEC-004", "SPEC-009", "TICKET-100", "TASK-102"]

    plan_path = next(
        path.node_ids for path in result.report.paths if path.artifact_id == "PLAN-027"
    )
    assert plan_path == [
        "DEC-018",
        "DEC-004",
        "SPEC-009",
        "TICKET-100",
        "TASK-102",
        "PLAN-027",
    ]
    assert result.report.evidence_refs == [
        "slack://compliance/decision-018",
        "slack://product/decision-004",
        "notion://specs/export-009",
        "linear://ticket/TICKET-100",
        "linear://ticket/TICKET-100#task-101",
        "linear://ticket/TICKET-100#task-102",
        "agent://run/RUN-27/plan/PLAN-027",
    ]


def test_upstream_chain_is_one_real_path_when_the_graph_branches() -> None:
    version, artifacts, edges, _ = load_graph_fixture()
    artifacts.extend(
        [
            Artifact(
                id="SPEC-B",
                kind=ArtifactKind.SPECIFICATION,
                title="Second export specification",
                scopes={"export.authorization"},
            ),
            Artifact(
                id="TICKET-B",
                kind=ArtifactKind.TICKET,
                title="Second export ticket",
                scopes={"export.authorization"},
            ),
            Artifact(
                id="TASK-B",
                kind=ArtifactKind.TASK,
                title="Second export task",
                scopes={"export.authorization"},
            ),
        ]
    )
    edges.extend(
        [
            Edge(
                source_id="DEC-004",
                target_id="SPEC-B",
                kind=EdgeKind.BASIS_FOR,
            ),
            Edge(
                source_id="SPEC-B",
                target_id="TICKET-B",
                kind=EdgeKind.CREATES,
            ),
            Edge(
                source_id="TICKET-B",
                target_id="TASK-B",
                kind=EdgeKind.DECOMPOSES_TO,
            ),
        ]
    )
    graph = MemoryGraphStore()
    graph.reset(version=version, artifacts=artifacts, edges=edges)
    authority = IntentAuthority(graph=graph, signer=GrantSigner("test-secret"))

    result = authority.apply_decision_change(load_decision_v18())

    assert result.report is not None
    assert result.report.upstream_chain_artifact_ids == [
        "DEC-018",
        "DEC-004",
        "SPEC-009",
        "TICKET-100",
    ]
    assert "SPEC-B" not in result.report.upstream_chain_artifact_ids
    assert "TICKET-B" not in result.report.upstream_chain_artifact_ids
    assert "TASK-B" in result.report.stopped_work_artifact_ids
    assert "TASK-B" in result.report.invalidated_task_ids


def test_equal_depth_primary_path_is_stable_when_edge_order_changes() -> None:
    def run_with_edge_order(*, reverse: bool):
        version, artifacts, edges, _ = load_graph_fixture()
        artifacts.extend(
            [
                Artifact(
                    id="SPEC-B",
                    kind=ArtifactKind.SPECIFICATION,
                    title="Equal-depth specification",
                    scopes={"export.authorization"},
                ),
                Artifact(
                    id="TICKET-B",
                    kind=ArtifactKind.TICKET,
                    title="Equal-depth ticket",
                    scopes={"export.authorization"},
                ),
                Artifact(
                    id="TASK-B",
                    kind=ArtifactKind.TASK,
                    title="Equal-depth task",
                    scopes={"export.authorization"},
                ),
                Artifact(
                    id="PLAN-B",
                    kind=ArtifactKind.AGENT_PLAN,
                    title="Equal-depth plan",
                    scopes={"export.authorization", "export.generation"},
                ),
                Artifact(
                    id="EVIDENCE-A",
                    kind=ArtifactKind.EVIDENCE,
                    title="Non-downstream evidence",
                    scopes={"export.authorization"},
                ),
            ]
        )
        edges.extend(
            [
                Edge(
                    source_id="DEC-004",
                    target_id="SPEC-B",
                    kind=EdgeKind.BASIS_FOR,
                ),
                Edge(
                    source_id="SPEC-B",
                    target_id="TICKET-B",
                    kind=EdgeKind.CREATES,
                ),
                Edge(
                    source_id="TICKET-B",
                    target_id="TASK-B",
                    kind=EdgeKind.DECOMPOSES_TO,
                ),
                Edge(
                    source_id="TASK-B",
                    target_id="PLAN-B",
                    kind=EdgeKind.CURRENTLY_DRIVES,
                ),
                Edge(
                    source_id="DEC-004",
                    target_id="EVIDENCE-A",
                    kind=EdgeKind.SUPPORTED_BY,
                ),
            ]
        )
        if reverse:
            edges.reverse()
        graph = MemoryGraphStore()
        graph.reset(version=version, artifacts=artifacts, edges=edges)
        authority = IntentAuthority(graph=graph, signer=GrantSigner("stable-path-secret"))
        result = authority.apply_decision_change(load_decision_v18())
        assert result.report is not None
        return result.report

    forward = run_with_edge_order(reverse=False)
    reversed_order = run_with_edge_order(reverse=True)

    assert forward.paths == reversed_order.paths
    assert forward.upstream_chain_artifact_ids == reversed_order.upstream_chain_artifact_ids
    assert forward.upstream_chain_artifact_ids == [
        "DEC-018",
        "DEC-004",
        "SPEC-009",
        "TICKET-100",
    ]
    assert "EVIDENCE-A" not in forward.affected_artifact_ids
    assert all("EVIDENCE-A" not in path.node_ids for path in forward.paths)
