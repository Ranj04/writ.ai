from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from dragback.authority.engine import IntentAuthority
from dragback.domain import Artifact, Edge, Verdict
from dragback.fixtures import load_decision_v18, load_graph_fixture
from dragback.grants import GrantSigner
from dragback.graph.base import GraphStore
from dragback.graph.memory import MemoryGraphStore
from dragback.graph.neo4j_store import Neo4jGraphStore

pytestmark = pytest.mark.neo4j

_ENABLE_ENV = "DRAGBACK_RUN_NEO4J_TESTS"
_CONNECTION_ENV = (
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "NEO4J_DATABASE",
)


def _artifact_payload(artifact: Artifact) -> dict[str, Any]:
    payload = artifact.model_dump(mode="json")
    payload["scopes"] = sorted(artifact.scopes)
    payload["invalidated_scopes"] = sorted(artifact.invalidated_scopes)
    return payload


def _edge_payload(edge: Edge) -> dict[str, Any]:
    payload = edge.model_dump(mode="json")
    payload["scopes"] = sorted(edge.scopes)
    return payload


def _canonical_graph(store: GraphStore) -> dict[str, object]:
    artifacts = sorted(
        (_artifact_payload(artifact) for artifact in store.list_artifacts()),
        key=lambda artifact: str(artifact["id"]),
    )
    edges = sorted(
        (_edge_payload(edge) for edge in store.list_edges()),
        key=lambda edge: (
            str(edge["source_id"]),
            str(edge["kind"]),
            str(edge["target_id"]),
        ),
    )
    return {
        "graph_version": store.version_label,
        "artifacts": artifacts,
        "edges": edges,
    }


@pytest.fixture(scope="module")
def neo4j_graph() -> Iterator[Neo4jGraphStore]:
    if os.getenv(_ENABLE_ENV) != "1":
        pytest.skip(
            f"Set {_ENABLE_ENV}=1 and provide a disposable Neo4j database to run parity tests."
        )

    missing = [name for name in _CONNECTION_ENV if not os.getenv(name)]
    if missing:
        pytest.fail(
            "Neo4j integration tests require these environment variables: "
            + ", ".join(missing)
        )

    graph = Neo4jGraphStore(
        uri=os.environ["NEO4J_URI"],
        username=os.environ["NEO4J_USERNAME"],
        password=os.environ["NEO4J_PASSWORD"],
        database=os.environ["NEO4J_DATABASE"],
    )
    try:
        yield graph
    finally:
        version, artifacts, edges, _ = load_graph_fixture()
        graph.reset(version=version, artifacts=artifacts, edges=edges)
        graph.close()


def test_seed_reset_is_deterministic_and_matches_memory(
    neo4j_graph: Neo4jGraphStore,
) -> None:
    version, artifacts, edges, _ = load_graph_fixture()
    memory = MemoryGraphStore()
    memory.reset(version=version, artifacts=artifacts, edges=edges)

    neo4j_graph.reset(version=version, artifacts=artifacts, edges=edges)
    first_seed = _canonical_graph(neo4j_graph)
    assert first_seed == _canonical_graph(memory)

    neo4j_authority = IntentAuthority(
        graph=neo4j_graph,
        signer=GrantSigner("neo4j-parity-test-secret"),
    )
    assert neo4j_authority.apply_decision_change(load_decision_v18()).applied is True
    assert neo4j_graph.version_label == "graph-v18"

    neo4j_graph.reset(version=version, artifacts=artifacts, edges=edges)
    assert _canonical_graph(neo4j_graph) == first_seed

    neo4j_graph.reset(version=version, artifacts=artifacts, edges=edges)
    assert _canonical_graph(neo4j_graph) == first_seed


def test_neo4j_authority_report_matches_memory(
    neo4j_graph: Neo4jGraphStore,
) -> None:
    version, artifacts, edges, run = load_graph_fixture()
    memory = MemoryGraphStore()
    memory.reset(version=version, artifacts=artifacts, edges=edges)
    neo4j_graph.reset(version=version, artifacts=artifacts, edges=edges)

    memory_authority = IntentAuthority(
        graph=memory,
        signer=GrantSigner("memory-parity-test-secret"),
    )
    neo4j_authority = IntentAuthority(
        graph=neo4j_graph,
        signer=GrantSigner("neo4j-parity-test-secret"),
    )

    memory_initial = memory_authority.evaluate_plan(
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=run.plan,
    )
    neo4j_initial = neo4j_authority.evaluate_plan(
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=run.plan,
    )
    assert memory_initial.verdict is Verdict.ALLOW
    assert neo4j_initial.verdict is memory_initial.verdict
    assert neo4j_initial.current_requirements == memory_initial.current_requirements

    memory_result = memory_authority.apply_decision_change(load_decision_v18())
    neo4j_result = neo4j_authority.apply_decision_change(load_decision_v18())

    assert neo4j_result == memory_result
    assert _canonical_graph(neo4j_graph) == _canonical_graph(memory)

    memory_recheck = memory_authority.evaluate_plan(
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=run.plan,
    )
    neo4j_recheck = neo4j_authority.evaluate_plan(
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=run.plan,
    )
    assert memory_recheck.verdict is Verdict.REPLAN
    assert neo4j_recheck == memory_recheck
