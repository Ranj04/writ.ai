from dragback.authority.engine import IntentAuthority
from dragback.domain import AgentRun, Verdict, VerificationCode
from dragback.fixtures import load_decision_v18, load_graph_fixture
from dragback.grants import GrantSigner
from dragback.graph.memory import MemoryGraphStore
from dragback.hashing import stable_hash


def make_authority(*, ttl_seconds: int = 300) -> tuple[IntentAuthority, AgentRun]:
    version, artifacts, edges, run = load_graph_fixture()
    graph = MemoryGraphStore()
    graph.reset(version=version, artifacts=artifacts, edges=edges)
    return (
        IntentAuthority(
            graph=graph,
            signer=GrantSigner("test-secret", ttl_seconds=ttl_seconds),
        ),
        run,
    )


def issue_initial_grant(authority: IntentAuthority, run: AgentRun) -> str:
    result = authority.evaluate_plan(
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=run.plan,
    )
    assert result.grant is not None
    return result.grant.token


def verify_initial_grant(
    authority: IntentAuthority,
    run: AgentRun,
    token: str,
):
    return authority.verify_grant(
        token=token,
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=run.plan,
    )


def test_valid_grant_has_stable_code() -> None:
    authority, run = make_authority()

    result = verify_initial_grant(authority, run, issue_initial_grant(authority, run))

    assert result.valid is True
    assert result.code is VerificationCode.VALID
    assert result.reason == "Grant is valid."


def test_invalid_token_has_stable_code() -> None:
    authority, run = make_authority()

    result = verify_initial_grant(authority, run, "not-a-signed-grant")

    assert result.valid is False
    assert result.code is VerificationCode.INVALID_TOKEN
    assert result.payload is None


def test_non_allow_verdict_has_stable_code() -> None:
    authority, run = make_authority()
    token = authority.signer.issue(
        run_id=run.run_id,
        task_id=run.ticket_id,
        decision_snapshot=authority.graph.version_label,
        plan_hash=stable_hash(run.plan),
        verdict=Verdict.REPLAN,
    ).token

    result = verify_initial_grant(authority, run, token)

    assert result.valid is False
    assert result.code is VerificationCode.NON_ALLOW_VERDICT


def test_expired_grant_has_stable_code() -> None:
    authority, run = make_authority(ttl_seconds=-1)

    result = verify_initial_grant(authority, run, issue_initial_grant(authority, run))

    assert result.valid is False
    assert result.code is VerificationCode.EXPIRED


def test_binding_mismatch_has_stable_code() -> None:
    authority, run = make_authority()
    token = issue_initial_grant(authority, run)

    result = authority.verify_grant(
        token=token,
        run_id="RUN-OTHER",
        task_id=run.ticket_id,
        plan=run.plan,
    )

    assert result.valid is False
    assert result.code is VerificationCode.BINDING_MISMATCH


def test_plan_hash_mismatch_has_stable_code() -> None:
    authority, run = make_authority()
    token = issue_initial_grant(authority, run)
    changed_plan = run.plan.model_copy(deep=True)
    changed_plan.actions[0].description = "Changed after authorization"

    result = authority.verify_grant(
        token=token,
        run_id=run.run_id,
        task_id=run.ticket_id,
        plan=changed_plan,
    )

    assert result.valid is False
    assert result.code is VerificationCode.PLAN_HASH_MISMATCH


def test_stale_snapshot_has_stable_code() -> None:
    authority, run = make_authority()
    token = issue_initial_grant(authority, run)
    mutation = authority.apply_decision_change(load_decision_v18())
    assert mutation.applied is True

    result = verify_initial_grant(authority, run, token)

    assert result.valid is False
    assert result.code is VerificationCode.STALE_SNAPSHOT


def test_current_plan_rejection_has_stable_code() -> None:
    authority, run = make_authority()
    mutation = authority.apply_decision_change(load_decision_v18())
    assert mutation.applied is True
    token = authority.signer.issue(
        run_id=run.run_id,
        task_id=run.ticket_id,
        decision_snapshot=authority.graph.version_label,
        plan_hash=stable_hash(run.plan),
    ).token

    result = verify_initial_grant(authority, run, token)

    assert result.valid is False
    assert result.code is VerificationCode.CURRENT_PLAN_REJECTED
