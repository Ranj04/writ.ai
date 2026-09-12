from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from writai.domain import Artifact, ArtifactKind, Edge, EdgeKind
from writai.graph.base import GraphStore
from writai.graph.memory import MemoryGraphStore
from writai.graph.neo4j_store import Neo4jGraphStore

_ENABLE_ENV = "WRITAI_RUN_NEO4J_TESTS"
_CONNECTION_ENV = (
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "NEO4J_DATABASE",
)


@pytest.fixture(
    params=[
        pytest.param("memory", id="memory"),
        pytest.param("neo4j", id="neo4j", marks=pytest.mark.neo4j),
    ]
)
def graph_store(request: pytest.FixtureRequest) -> Iterator[GraphStore]:
    if request.param == "memory":
        store: GraphStore = MemoryGraphStore()
        store.reset(version=17, artifacts=[], edges=[])
        yield store
        return

    if os.getenv(_ENABLE_ENV) != "1":
        pytest.skip(
            f"Set {_ENABLE_ENV}=1 and provide a disposable Neo4j database to run parity tests."
        )

    missing = [name for name in _CONNECTION_ENV if not os.getenv(name)]
    if missing:
        pytest.fail(
            "Neo4j integration tests require these environment variables: " + ", ".join(missing)
        )

    neo4j_store = Neo4jGraphStore(
        uri=os.environ["NEO4J_URI"],
        username=os.environ["NEO4J_USERNAME"],
        password=os.environ["NEO4J_PASSWORD"],
        database=os.environ["NEO4J_DATABASE"],
    )
    neo4j_store.reset(version=17, artifacts=[], edges=[])
    try:
        yield neo4j_store
    finally:
        neo4j_store.close()


def _artifact(artifact_id: str) -> Artifact:
    return Artifact(
        id=artifact_id,
        kind=ArtifactKind.DECISION,
        title=artifact_id,
        scopes=set(),
    )


def test_add_artifact_rejects_a_duplicate_id(graph_store: GraphStore) -> None:
    graph_store.add_artifact(_artifact("A-1"))

    with pytest.raises(ValueError, match="^Artifact already exists: A-1$"):
        graph_store.add_artifact(_artifact("A-1"))


def test_add_edge_rejects_a_missing_endpoint(graph_store: GraphStore) -> None:
    graph_store.add_artifact(_artifact("A-1"))

    with pytest.raises(KeyError, match="Both edge endpoints must exist: MISSING -> A-1"):
        graph_store.add_edge(Edge(source_id="MISSING", target_id="A-1", kind=EdgeKind.BASIS_FOR))
    with pytest.raises(KeyError, match="Both edge endpoints must exist: A-1 -> MISSING"):
        graph_store.add_edge(Edge(source_id="A-1", target_id="MISSING", kind=EdgeKind.BASIS_FOR))


def test_list_artifacts_is_ordered_by_id(graph_store: GraphStore) -> None:
    for artifact_id in ["C-3", "A-1", "B-2"]:
        graph_store.add_artifact(_artifact(artifact_id))

    assert [artifact.id for artifact in graph_store.list_artifacts()] == ["A-1", "B-2", "C-3"]


def test_outgoing_edges_is_ordered_by_target_then_kind(graph_store: GraphStore) -> None:
    for artifact_id in ["SOURCE", "B-2", "A-1"]:
        graph_store.add_artifact(_artifact(artifact_id))
    # Inserted in reverse of the expected order on both keys, so a backend that
    # returned insertion order could not pass by accident.
    for edge in [
        Edge(source_id="SOURCE", target_id="B-2", kind=EdgeKind.CREATES),
        Edge(source_id="SOURCE", target_id="A-1", kind=EdgeKind.DECOMPOSES_TO),
        Edge(source_id="SOURCE", target_id="A-1", kind=EdgeKind.CREATES),
    ]:
        graph_store.add_edge(edge)

    assert [(edge.target_id, edge.kind) for edge in graph_store.outgoing_edges("SOURCE")] == [
        ("A-1", EdgeKind.CREATES),
        ("A-1", EdgeKind.DECOMPOSES_TO),
        ("B-2", EdgeKind.CREATES),
    ]


def test_increment_version_returns_the_new_label(graph_store: GraphStore) -> None:
    assert graph_store.increment_version() == "graph-v18"
    assert graph_store.version_label == "graph-v18"


def test_transaction_rolls_back_on_exception(graph_store: GraphStore) -> None:
    original_version = graph_store.version_label

    with pytest.raises(RuntimeError, match="boom"):
        with graph_store.transaction():
            graph_store.add_artifact(_artifact("ROLLBACK"))
            graph_store.increment_version()
            raise RuntimeError("boom")

    with pytest.raises(KeyError):
        graph_store.get_artifact("ROLLBACK")
    assert graph_store.version_label == original_version


def test_a_nested_transaction_joins_the_outer_one(graph_store: GraphStore) -> None:
    # RC-1 probe: an inner block has no transaction of its own to roll back, so a
    # failure the outer block swallows must not undo the inner block's writes.
    with graph_store.transaction():
        graph_store.add_artifact(_artifact("A"))
        try:
            with graph_store.transaction():
                graph_store.add_artifact(_artifact("B"))
                raise RuntimeError("inner")
        except RuntimeError:
            pass
        graph_store.add_artifact(_artifact("C"))

    assert [artifact.id for artifact in graph_store.list_artifacts()] == ["A", "B", "C"]


def test_downstream_subgraph_is_ordered_and_kind_filtered(graph_store: GraphStore) -> None:
    for artifact_id in ["ROOT", "B-2", "A-1", "EVIDENCE-A"]:
        graph_store.add_artifact(_artifact(artifact_id))
    for edge in [
        Edge(source_id="ROOT", target_id="B-2", kind=EdgeKind.CREATES),
        Edge(source_id="ROOT", target_id="A-1", kind=EdgeKind.BASIS_FOR),
        Edge(source_id="ROOT", target_id="EVIDENCE-A", kind=EdgeKind.SUPPORTED_BY),
    ]:
        graph_store.add_edge(edge)

    artifacts, edges = graph_store.downstream_subgraph(
        "ROOT", {EdgeKind.BASIS_FOR, EdgeKind.CREATES}
    )

    assert [artifact.id for artifact in artifacts] == ["A-1", "B-2", "ROOT"]
    assert [(edge.source_id, edge.kind, edge.target_id) for edge in edges] == [
        ("ROOT", EdgeKind.BASIS_FOR, "A-1"),
        ("ROOT", EdgeKind.CREATES, "B-2"),
    ]


def test_downstream_subgraph_rejects_an_unknown_root(graph_store: GraphStore) -> None:
    with pytest.raises(KeyError, match="Unknown artifact: NOPE"):
        graph_store.downstream_subgraph("NOPE", {EdgeKind.CREATES})


def test_downstream_subgraph_preserves_duplicate_edges(graph_store: GraphStore) -> None:
    graph_store.add_artifact(_artifact("ROOT"))
    graph_store.add_artifact(_artifact("CHILD"))
    edge = Edge(source_id="ROOT", target_id="CHILD", kind=EdgeKind.CREATES)
    graph_store.add_edge(edge)
    graph_store.add_edge(edge)

    _, edges = graph_store.downstream_subgraph("ROOT", {EdgeKind.CREATES})

    assert len(edges) == 2
    assert edges == graph_store.outgoing_edges("ROOT", {EdgeKind.CREATES})
