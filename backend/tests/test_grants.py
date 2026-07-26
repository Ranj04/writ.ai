from dragback.authority.engine import IntentAuthority
from dragback.domain import AgentRun, Verdict
from dragback.fixtures import load_decision_v18, load_graph_fixture
from dragback.grants import GrantSigner
from dragback.graph.memory import MemoryGraphStore
from dragback.hashing import stable_hash


def make_authority(*, ttl_seconds: int = 300) -> tuple[IntentAuthority, AgentRun]:
    version, artifacts, edges, run = load_graph_fixture()
    graph = MemoryGraphStore()
    graph.reset(version=version, artifacts=artifacts, edges=edges)
    authority = IntentAuthority(
        graph=graph,
        signer=GrantSigner("test-secret", ttl_seconds=ttl_seconds),
    )
    return authority, run


def test_same_snapshot_grant_is_valid() -> None:
    authority, raw_run = make_authority()
    run = raw_run

    initial = authority.evaluate_plan(run_id=run.run_id, task_id=run.ticket_id, plan=run.plan)
    assert initial.grant is not None

    verification = authority.verify_grant(
        token=initial.grant.token,
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=run.plan,
    )

    assert verification.valid is True


def test_old_grant_fails_after_graph_change() -> None:
    authority, raw_run = make_authority()
    run = raw_run

    initial = authority.evaluate_plan(run_id=run.run_id, task_id=run.ticket_id, plan=run.plan)
    assert initial.grant is not None
    assert initial.grant.payload.plan_hash == stable_hash(run.plan)

    authority.apply_decision_change(load_decision_v18())
    verification = authority.verify_grant(
        token=initial.grant.token,
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=run.plan,
    )
    assert verification.valid is False
    assert "stale" in verification.reason.lower()


def test_grant_rejects_a_modified_plan() -> None:
    authority, raw_run = make_authority()
    run = raw_run
    initial = authority.evaluate_plan(run_id=run.run_id, task_id=run.ticket_id, plan=run.plan)
    assert initial.grant is not None
    modified = run.plan.model_copy(deep=True)
    modified.actions[0].description = "A different action after authorization"

    verification = authority.verify_grant(
        token=initial.grant.token,
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=modified,
    )

    assert verification.valid is False
    assert "plan hash" in verification.reason.lower()


def test_expired_grant_is_rejected() -> None:
    authority, raw_run = make_authority(ttl_seconds=-1)
    run = raw_run
    initial = authority.evaluate_plan(run_id=run.run_id, task_id=run.ticket_id, plan=run.plan)
    assert initial.grant is not None

    verification = authority.verify_grant(
        token=initial.grant.token,
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=run.plan,
    )

    assert verification.valid is False
    assert "expired" in verification.reason.lower()


def test_signed_non_allow_payload_is_never_executable() -> None:
    authority, raw_run = make_authority()
    run = raw_run
    token = authority.signer.issue(
        run_id=run.run_id,
        task_id=run.ticket_id,
        decision_snapshot=authority.graph.version_label,
        plan_hash=stable_hash(run.plan),
        verdict=Verdict.REPLAN,
    ).token

    verification = authority.verify_grant(
        token=token,
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=run.plan,
    )

    assert verification.valid is False
    assert "not ALLOW" in verification.reason
