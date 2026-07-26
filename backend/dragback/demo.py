from __future__ import annotations

from dragback.authority.engine import IntentAuthority
from dragback.config import settings
from dragback.fixtures import load_decision_v18, load_graph_fixture
from dragback.grants import GrantSigner
from dragback.graph.memory import MemoryGraphStore
from dragback.loop.workflow import AgentLoopController


def line(title: str) -> None:
    print(f"\n{'=' * 10} {title} {'=' * 10}")


def main() -> None:
    version, artifacts, edges, run = load_graph_fixture()
    graph = MemoryGraphStore()
    graph.reset(version=version, artifacts=artifacts, edges=edges)
    authority = IntentAuthority(
        graph=graph,
        signer=GrantSigner(settings.grant_secret, settings.grant_ttl_seconds),
        authority_threshold=settings.authority_threshold,
    )
    controller = AgentLoopController(authority=authority, run=run)

    line("1. Initial authorization")
    initial = controller.start()
    assert initial.grant is not None
    old_token = initial.grant.token
    old_plan = controller.run.plan.model_copy(deep=True)
    print(f"Graph: {graph.version_label}")
    print(f"Verdict: {initial.verdict.value}")
    print(f"Grant: {initial.grant.payload.authorization_id}")

    line("2. Agent acts and tests pass")
    controller.mark_tests_passed()
    print("Implementation complete. Tests: PASS")

    line("3. Approved upstream decision arrives")
    mutation = authority.apply_decision_change(load_decision_v18())
    assert mutation.report is not None
    print(f"Graph: {mutation.graph_version}")
    print("Affected:", ", ".join(mutation.report.affected_artifact_ids))
    print("Preserved:", ", ".join(mutation.report.preserved_artifact_ids))
    task_path = next(
        path.node_ids for path in mutation.report.paths if path.artifact_id == "TASK-102"
    )
    print("Invalidation path:", " -> ".join(task_path))
    print("TASK-101:", graph.get_artifact("TASK-101").validity.value)
    print("TASK-102:", graph.get_artifact("TASK-102").validity.value)

    line("4. Executor rejects stale grant")
    stale = authority.verify_grant(
        token=old_token,
        run_id=controller.run.run_id,
        task_id=controller.run.ticket_id,
        plan=old_plan,
    )
    print(f"Valid: {stale.valid}")
    print(f"Reason: {stale.reason}")

    line("5. Loop rechecks and replans")
    recheck = controller.recheck()
    print(f"Verdict: {recheck.verdict.value}")
    print("Affected scopes:", ", ".join(sorted(recheck.affected_scopes)))
    corrected = controller.replan()
    assert corrected.grant is not None
    print(f"Corrected verdict: {corrected.verdict.value}")
    for action in controller.run.plan.actions:
        print(f"- {action.description}: {action.attributes}")

    line("6. Executor accepts current grant")
    fresh = authority.verify_grant(
        token=corrected.grant.token,
        run_id=controller.run.run_id,
        task_id=controller.run.ticket_id,
        plan=controller.run.plan,
    )
    print(f"Valid: {fresh.valid}")
    print(f"Reason: {fresh.reason}")

    line("Dragback proof complete")
    print("Tests prove the code works. Dragback proves the work is still wanted.")


if __name__ == "__main__":
    main()
