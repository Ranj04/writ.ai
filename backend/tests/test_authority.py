from dragback.authority.engine import IntentAuthority
from dragback.domain import Verdict
from dragback.fixtures import load_graph_fixture, load_ignored_proposal
from dragback.grants import GrantSigner
from dragback.graph.memory import MemoryGraphStore


def make_authority() -> IntentAuthority:
    version, artifacts, edges, _ = load_graph_fixture()
    graph = MemoryGraphStore()
    graph.reset(version=version, artifacts=artifacts, edges=edges)
    return IntentAuthority(graph=graph, signer=GrantSigner("test-secret"))


def test_proposal_does_not_mutate_graph() -> None:
    authority = make_authority()
    result = authority.apply_decision_change(load_ignored_proposal())
    assert result.applied is False
    assert result.verdict is Verdict.HUMAN_REVIEW
    assert authority.graph.version_label == "graph-v17"
    assert all(item.id != "DEC-PROPOSAL-1" for item in authority.graph.list_artifacts())
