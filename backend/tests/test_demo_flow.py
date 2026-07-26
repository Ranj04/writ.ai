from dragback.authority.engine import IntentAuthority
from dragback.domain import Verdict
from dragback.fixtures import load_decision_v18, load_graph_fixture
from dragback.grants import GrantSigner
from dragback.graph.memory import MemoryGraphStore
from dragback.loop.workflow import AgentLoopController


def test_complete_reauthorization_flow() -> None:
    version, artifacts, edges, run = load_graph_fixture()
    graph = MemoryGraphStore()
    graph.reset(version=version, artifacts=artifacts, edges=edges)
    authority = IntentAuthority(graph=graph, signer=GrantSigner("test-secret"))
    controller = AgentLoopController(authority=authority, run=run)

    initial = controller.start()
    assert initial.verdict is Verdict.ALLOW
    assert initial.grant is not None

    controller.mark_tests_passed()
    authority.apply_decision_change(load_decision_v18())

    recheck = controller.recheck()
    assert recheck.verdict is Verdict.REPLAN
    assert recheck.affected_scopes == {"export.authorization"}
    assert "slack://compliance/decision-018" in recheck.evidence_refs
    assert "agent://run/RUN-27/plan/PLAN-027" in recheck.evidence_refs

    corrected = controller.replan()
    assert corrected.verdict is Verdict.ALLOW
    assert corrected.graph_version == "graph-v18"
    assert corrected.grant is not None
    authorization_action = next(
        action for action in controller.run.plan.actions if "export.authorization" in action.scopes
    )
    assert authorization_action.attributes["audience"] == "admin_only"

    verification = authority.verify_grant(
        token=corrected.grant.token,
        run_id=controller.run.run_id,
        task_id=controller.run.ticket_id,
        plan=controller.run.plan,
    )
    assert verification.valid is True
