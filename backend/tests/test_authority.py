import pytest
from writai.authority.engine import IntentAuthority
from writai.domain import Artifact, ValidityStatus, Verdict
from writai.fixtures import load_decision_v18, load_graph_fixture, load_ignored_proposal
from writai.grants import GrantSigner
from writai.graph.memory import MemoryGraphStore


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


class _FailingMemoryGraphStore(MemoryGraphStore):
    def __init__(self) -> None:
        super().__init__()
        self.update_calls = 0

    def update_artifact(self, artifact: Artifact) -> None:
        self.update_calls += 1
        if self.update_calls == 3:
            raise RuntimeError("injected")
        super().update_artifact(artifact)


def test_a_failed_invalidation_leaves_the_graph_untouched() -> None:
    version, artifacts, edges, _ = load_graph_fixture()
    graph = _FailingMemoryGraphStore()
    graph.reset(version=version, artifacts=artifacts, edges=edges)
    authority = IntentAuthority(graph=graph, signer=GrantSigner("test-secret"))

    with pytest.raises(RuntimeError, match="injected"):
        authority.apply_decision_change(load_decision_v18())

    assert graph.version_label == "graph-v17"
    with pytest.raises(KeyError):
        graph.get_artifact("DEC-018")
    assert graph.get_artifact("TASK-102").validity is ValidityStatus.VALID


def test_a_successful_change_still_commits_every_artifact() -> None:
    authority = make_authority()

    result = authority.apply_decision_change(load_decision_v18())

    assert result.graph_version == "graph-v18"
    assert authority.graph.get_artifact("TASK-102").validity is ValidityStatus.INVALIDATED
    assert authority.graph.get_artifact("TASK-101").validity is ValidityStatus.VALID
