"""Adversarial review tests for Track A (authority & graph), round 1.

Each test states in its docstring whether it is expected to FAIL today (a finding) or
to PASS today (a pin on an invariant the build prompt stated in prose). Neo4j-marked
parameters reuse the exact opt-in convention from ``test_neo4j_integration.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from writai.authority.engine import IntentAuthority
from writai.domain import Artifact, ArtifactKind, Edge, EdgeKind
from writai.fixtures import load_decision_v18, load_graph_fixture
from writai.grants import GrantSigner
from writai.graph.base import GraphStore
from writai.graph.memory import MemoryGraphStore
from writai.graph.neo4j_store import Neo4jGraphStore
from writai.services import authority_api

_ENABLE_ENV = "WRITAI_RUN_NEO4J_TESTS"
_CONNECTION_ENV = ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE")


def _neo4j_store_or_skip() -> Neo4jGraphStore:
    if os.getenv(_ENABLE_ENV) != "1":
        pytest.skip(
            f"Set {_ENABLE_ENV}=1 and provide a disposable Neo4j database to run parity tests."
        )
    missing = [name for name in _CONNECTION_ENV if not os.getenv(name)]
    if missing:
        pytest.fail(
            "Neo4j integration tests require these environment variables: " + ", ".join(missing)
        )
    return Neo4jGraphStore(
        uri=os.environ["NEO4J_URI"],
        username=os.environ["NEO4J_USERNAME"],
        password=os.environ["NEO4J_PASSWORD"],
        database=os.environ["NEO4J_DATABASE"],
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
    neo4j_store = _neo4j_store_or_skip()
    neo4j_store.reset(version=17, artifacts=[], edges=[])
    try:
        yield neo4j_store
    finally:
        neo4j_store.close()


@pytest.fixture
def neo4j_store() -> Iterator[Neo4jGraphStore]:
    store = _neo4j_store_or_skip()
    store.reset(version=17, artifacts=[], edges=[])
    try:
        yield store
    finally:
        store.close()


def _artifact(artifact_id: str) -> Artifact:
    return Artifact(id=artifact_id, kind=ArtifactKind.DECISION, title=artifact_id, scopes=set())


def _canonical(store: GraphStore) -> dict[str, object]:
    artifacts = [item.model_dump(mode="json") for item in store.list_artifacts()]
    artifacts.sort(key=lambda item: item["id"])
    for item in artifacts:
        item["scopes"] = sorted(item["scopes"])
        item["invalidated_scopes"] = sorted(item["invalidated_scopes"])
    edges = [edge.model_dump(mode="json") for edge in store.list_edges()]
    edges.sort(key=lambda item: (item["source_id"], item["kind"], item["target_id"]))
    for item in edges:
        item["scopes"] = sorted(item["scopes"])
    return {"graph_version": store.version_label, "artifacts": artifacts, "edges": edges}


# --------------------------------------------------------------------------------------
# A4 contract: downstream_subgraph parity between the two backends
# --------------------------------------------------------------------------------------


def test_downstream_subgraph_rejects_an_unknown_root(graph_store: GraphStore) -> None:
    """FAILS today on neo4j: memory raises KeyError, neo4j returns ([], [])."""
    graph_store.add_artifact(_artifact("A-1"))

    with pytest.raises(KeyError, match="Unknown artifact: NOPE"):
        graph_store.downstream_subgraph("NOPE", {EdgeKind.CREATES})


def test_downstream_subgraph_reports_duplicate_edges_like_outgoing_edges(
    graph_store: GraphStore,
) -> None:
    """FAILS today on neo4j: both backends accept a duplicate edge and outgoing_edges
    returns both, but neo4j's downstream_subgraph collapses them to one."""
    graph_store.add_artifact(_artifact("R"))
    graph_store.add_artifact(_artifact("C"))
    graph_store.add_edge(Edge(source_id="R", target_id="C", kind=EdgeKind.CREATES))
    graph_store.add_edge(Edge(source_id="R", target_id="C", kind=EdgeKind.CREATES))

    _, edges = graph_store.downstream_subgraph("R", {EdgeKind.CREATES})

    assert len(edges) == len(graph_store.outgoing_edges("R", {EdgeKind.CREATES})) == 2


# --------------------------------------------------------------------------------------
# A3 contract: transaction() semantics
# --------------------------------------------------------------------------------------


def test_version_label_inside_a_transaction_sees_the_uncommitted_increment(
    graph_store: GraphStore,
) -> None:
    """FAILS today on neo4j: `version` is not routed through `_run`, so inside a
    transaction it opens a second session and reads the committed (old) value."""
    with graph_store.transaction():
        assert graph_store.increment_version() == "graph-v18"
        assert graph_store.version_label == "graph-v18"
    assert graph_store.version_label == "graph-v18"


def test_nested_transactions_roll_back_together(graph_store: GraphStore) -> None:
    """FAILS today on neo4j: the inner context clears `_tx` in its `finally`, so the
    outer transaction's later writes autocommit and survive the outer rollback."""
    with pytest.raises(RuntimeError, match="boom"):
        with graph_store.transaction():
            graph_store.add_artifact(_artifact("OUTER"))
            with graph_store.transaction():
                graph_store.add_artifact(_artifact("INNER"))
            graph_store.add_artifact(_artifact("OUTER-2"))
            raise RuntimeError("boom")

    assert [artifact.id for artifact in graph_store.list_artifacts()] == []
    assert graph_store.version_label == "graph-v17"


def test_transaction_rolls_back_on_base_exception(graph_store: GraphStore) -> None:
    """FAILS today on memory: `except Exception` does not catch BaseException, so a
    KeyboardInterrupt mid-transaction leaves the half-applied state in place.
    Neo4j passes because closing the session rolls the transaction back."""
    with pytest.raises(KeyboardInterrupt):
        with graph_store.transaction():
            graph_store.add_artifact(_artifact("PARTIAL"))
            graph_store.increment_version()
            raise KeyboardInterrupt()

    with pytest.raises(KeyError):
        graph_store.get_artifact("PARTIAL")
    assert graph_store.version_label == "graph-v17"


@pytest.mark.neo4j
def test_transaction_commit_failure_surfaces_the_real_error(
    neo4j_store: Neo4jGraphStore,
) -> None:
    """FAILS today: when commit() raises, the except branch calls rollback() on an
    already-closed transaction, which raises TransactionError("Transaction closed")
    and masks the real cause (here: Neo.ClientError.Transaction.Terminated)."""
    from neo4j.exceptions import ClientError

    with pytest.raises(ClientError, match="Terminated"):
        with neo4j_store.transaction():
            neo4j_store.add_artifact(_artifact("X"))
            with neo4j_store._driver.session(database=neo4j_store._database) as other:
                rows = list(
                    other.run(
                        "SHOW TRANSACTIONS YIELD transactionId, currentQuery "
                        "RETURN transactionId, currentQuery"
                    )
                )
                victims = [
                    row["transactionId"]
                    for row in rows
                    if not (row["currentQuery"] or "").startswith("SHOW TRANSACTIONS")
                ]
                assert victims, "expected the store's open transaction to be listed"
                other.run("TERMINATE TRANSACTIONS $ids", ids=victims).consume()

    assert neo4j_store._tx is None
    with pytest.raises(KeyError):
        neo4j_store.get_artifact("X")


# --------------------------------------------------------------------------------------
# A2: reset() must recover a database written by the pre-A2 store
# --------------------------------------------------------------------------------------


@pytest.mark.neo4j
def test_reset_recovers_a_database_that_holds_duplicate_artifact_ids(
    neo4j_store: Neo4jGraphStore,
) -> None:
    """FAILS today: reset() creates the uniqueness constraint BEFORE `DETACH DELETE`,
    so a database populated by the pre-A2 store (which permitted duplicate ids) makes
    reset() raise ConstraintCreationFailed and the store is unusable until cleaned by
    hand. A1's own red run against Neo4j is exactly such a database."""
    with neo4j_store._driver.session(database=neo4j_store._database) as session:
        session.run("DROP CONSTRAINT artifact_id_unique IF EXISTS").consume()
        session.run("CREATE (:Artifact {id: 'DUP'}), (:Artifact {id: 'DUP'})").consume()
    try:
        neo4j_store.reset(version=17, artifacts=[_artifact("A-1")], edges=[])
        assert [artifact.id for artifact in neo4j_store.list_artifacts()] == ["A-1"]
        with pytest.raises(ValueError, match="^Artifact already exists: A-1$"):
            neo4j_store.add_artifact(_artifact("A-1"))
    finally:
        with neo4j_store._driver.session(database=neo4j_store._database) as session:
            session.run("MATCH (n) DETACH DELETE n").consume()
        neo4j_store.reset(version=17, artifacts=[], edges=[])


# --------------------------------------------------------------------------------------
# A3 + A4 pins through the engine, on both backends
# --------------------------------------------------------------------------------------


def _fixture_store(store: GraphStore) -> GraphStore:
    version, artifacts, edges, _ = load_graph_fixture()
    store.reset(version=version, artifacts=artifacts, edges=edges)
    return store


class _FailOnIncrement:
    """Wraps a store so `increment_version` raises after add_artifact and add_edge ran."""

    def __init__(self, inner: GraphStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def increment_version(self) -> str:
        raise RuntimeError("injected before increment")


def test_a_failure_after_the_edge_is_written_leaves_the_whole_graph_identical(
    graph_store: GraphStore,
) -> None:
    """PASSES today (pin): the transaction wrapper must roll back the decision node AND
    the SUPERSEDES edge, not only artifact validity, on both backends."""
    _fixture_store(graph_store)
    before = _canonical(graph_store)
    authority = IntentAuthority(graph=_FailOnIncrement(graph_store), signer=GrantSigner("s"))

    with pytest.raises(RuntimeError, match="injected before increment"):
        authority.apply_decision_change(load_decision_v18())

    assert _canonical(graph_store) == before
    assert authority.last_report is None


@pytest.mark.neo4j
def test_batched_traversal_report_is_field_for_field_identical_across_backends(
    neo4j_store: Neo4jGraphStore,
) -> None:
    """PASSES today (pin): the InvalidationReport from the batched read is identical on
    memory and Neo4j for the demo fixture, compared with a full model_dump()."""
    memory = _fixture_store(MemoryGraphStore())
    _fixture_store(neo4j_store)

    on_memory = IntentAuthority(graph=memory, signer=GrantSigner("s"))
    on_neo4j = IntentAuthority(graph=neo4j_store, signer=GrantSigner("s"))
    memory_result = on_memory.apply_decision_change(load_decision_v18())
    neo4j_result = on_neo4j.apply_decision_change(load_decision_v18())

    assert memory_result.model_dump(mode="json") == neo4j_result.model_dump(mode="json")
    assert _canonical(memory) == _canonical(neo4j_store)


# --------------------------------------------------------------------------------------
# A5 pin: the bleed test's positive control is not vacuous
# --------------------------------------------------------------------------------------


def test_authorize_bleed_guard_positive_control_actually_applied_the_change() -> None:
    """PASSES today (pin): the guard in test_authority_guards.py asserts only a 200 on
    ingest; this pins that the change really applied and last_report was populated, so
    the empty provenance on the unrelated /authorize is a real non-bleed."""
    client = TestClient(authority_api.app)
    assert client.post("/demo/reset").status_code == 200
    ingest = client.post("/decisions/ingest", json=load_decision_v18().model_dump(mode="json"))
    assert ingest.status_code == 200
    assert ingest.json()["applied"] is True
    assert authority_api.runtime.authority.last_report is not None
    assert authority_api.runtime.authority.last_report.affected_artifact_ids

    _, _, _, run = load_graph_fixture()
    plan = run.plan.model_copy(deep=True)
    plan.id = "PLAN-UNRELATED"
    plan.ticket_id = "TASK-101"
    plan.actions = [action for action in plan.actions if action.scopes == {"export.generation"}]
    response = client.post(
        "/authorize",
        json={
            "run_id": "RUN-UNRELATED",
            "task_id": "TASK-101",
            "plan": plan.model_dump(mode="json"),
        },
    )
    assert response.status_code == 200
    assert response.json()["invalidated_artifact_ids"] == []
    assert response.json()["evidence_refs"] == []


def test_authorize_still_explains_a_replan_verdict_for_the_affected_plan() -> None:
    """FAILS today: after the v18 change, POST /authorize for the affected plan returns
    verdict REPLAN with invalidated_artifact_ids, preserved_artifact_ids, evidence_refs
    and invalidation_path all empty. Before this diff the same call returned the path
    DEC-018 -> DEC-004 -> SPEC-009 -> TICKET-100 -> TASK-102 -> PLAN-027. The bleed fix
    (A5) removed the explanation for the related request too, not only for unrelated
    ones. The call site is authority_api.py:1698 (Track B's file) - see findings."""
    client = TestClient(authority_api.app)
    assert client.post("/demo/reset").status_code == 200
    ingest = client.post("/decisions/ingest", json=load_decision_v18().model_dump(mode="json"))
    assert ingest.status_code == 200 and ingest.json()["applied"] is True

    _, _, _, run = load_graph_fixture()
    response = client.post(
        "/authorize",
        json={
            "run_id": run.run_id,
            "task_id": run.ticket_id,
            "plan": run.plan.model_dump(mode="json"),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "REPLAN"
    assert body["invalidation_path"] == [
        "DEC-018",
        "DEC-004",
        "SPEC-009",
        "TICKET-100",
        "TASK-102",
        "PLAN-027",
    ]
    assert "TASK-102" in body["invalidated_artifact_ids"]
    assert body["preserved_artifact_ids"] == ["TASK-101"]
