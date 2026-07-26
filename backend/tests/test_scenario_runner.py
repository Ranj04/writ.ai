from __future__ import annotations

import pytest
from writai.domain import (
    GrantVerificationRequest,
    LoopState,
    ValidityStatus,
    Verdict,
    VerificationCode,
)
from writai.scenarios import get_scenario
from writai.scenarios.authority_contexts import (
    ScenarioAuthorityContextNotFound,
    ScenarioAuthorityContextRegistry,
)
from writai.scenarios.run_models import (
    ScenarioCorrectiveActionLifecycle,
    ScenarioExecutionResult,
    ScenarioResultStatus,
    ScenarioStage,
    ScenarioTaskExpectedStatus,
)
from writai.scenarios.runner import ScenarioRunner


class InProcessScenarioTransport:
    def __init__(self) -> None:
        self.registry = ScenarioAuthorityContextRegistry(
            grant_secret="scenario-runner-test",
            grant_ttl_seconds=3600,
            authority_threshold=0.75,
        )

    def create_context(self, request):
        return self.registry.create(request)

    def delete_context(self, context_id: str) -> None:
        self.registry.delete(context_id)

    def apply_mutation(self, context_id: str):
        return self.registry.apply_mutation(context_id)

    def authorize(self, context_id: str, request):
        return self.registry.authorize(context_id, request)

    def execute(
        self,
        *,
        context_id: str,
        token: str,
        run_id: str,
        task_id: str,
        plan,
    ) -> ScenarioExecutionResult:
        verification = self.registry.verify_grant(
            context_id,
            GrantVerificationRequest(
                token=token,
                run_id=run_id,
                task_id=task_id,
                plan=plan,
            ),
        )
        return ScenarioExecutionResult(
            applied=verification.valid,
            reason=verification.reason,
            verification_code=verification.code,
            pull_request_url=(
                "https://example.invalid/writai/scenario-pr"
                if verification.valid
                else None
            ),
        )


class FailingInitialAuthorizationTransport(InProcessScenarioTransport):
    def __init__(self) -> None:
        super().__init__()
        self.deleted_context_ids: list[str] = []

    def authorize(self, context_id: str, request):
        raise RuntimeError("injected initial authorization failure")

    def delete_context(self, context_id: str) -> None:
        self.deleted_context_ids.append(context_id)
        super().delete_context(context_id)


class FailingMutationTransport(InProcessScenarioTransport):
    def apply_mutation(self, context_id: str):
        raise RuntimeError("injected mutation failure")


class UnsafeExecutorTransport(InProcessScenarioTransport):
    def execute(
        self,
        *,
        context_id: str,
        token: str,
        run_id: str,
        task_id: str,
        plan,
    ) -> ScenarioExecutionResult:
        return ScenarioExecutionResult(
            applied=True,
            reason="injected unsafe acceptance",
            verification_code=VerificationCode.VALID,
        )


class FailingScenarioRepository:
    def save(self, summary) -> None:
        raise RuntimeError("injected repository failure")

    def list_latest(self):
        return []


def build_runner() -> ScenarioRunner:
    return ScenarioRunner(transport=InProcessScenarioTransport())


def test_catalog_exposes_all_validated_scenarios() -> None:
    catalog = build_runner().catalog()

    assert len(catalog.scenarios) == 12
    assert catalog.scenarios[0].id == "csv-exports-admin-only"
    assert all(item.provenance_node_count >= 5 for item in catalog.scenarios)
    assert all(item.last_result is ScenarioResultStatus.NOT_RUN for item in catalog.scenarios)

    csv = catalog.scenarios[0]
    definition = get_scenario(csv.id)
    assert csv.specification.id == "SPEC-009"
    assert csv.specification.description
    assert csv.specification.scopes
    assert csv.ticket.id == "TICKET-100"
    assert csv.ticket.description == definition.metadata.description
    assert csv.ticket.scopes
    assert csv.initial_plan == definition.initial_run.plan
    assert {task.id: task.expected_status for task in csv.tasks} == {
        "TASK-101": ScenarioTaskExpectedStatus.PRESERVED,
        "TASK-102": ScenarioTaskExpectedStatus.PRESERVED,
        "TASK-103": ScenarioTaskExpectedStatus.PRESERVED,
        "TASK-104": ScenarioTaskExpectedStatus.INVALIDATED,
        "TASK-105": ScenarioTaskExpectedStatus.INVALIDATED,
    }
    assert all(task.title and task.description and task.scopes for task in csv.tasks)
    assert csv.corrective_actions
    assert all(action.source == "fixture" for action in csv.corrective_actions)
    assert all(
        action.representation == "plan-action" for action in csv.corrective_actions
    )
    assert all(
        action.graph_artifact_id is None
        and action.persisted_as_graph_artifact is False
        and action.lifecycle is ScenarioCorrectiveActionLifecycle.FIXTURE_PREVIEW
        for action in csv.corrective_actions
    )
    assert {action.id for action in csv.corrective_actions}.isdisjoint(
        {artifact.id for artifact in definition.graph_seed.artifacts}
    )
    assert "signed_token" not in csv.model_dump_json()


def test_csv_scenario_advances_through_real_service_boundaries() -> None:
    runner = build_runner()

    authorized = runner.start("csv-exports-admin-only")
    changed = runner.advance(authorized.run_id)
    stopped = runner.advance(authorized.run_id)
    completed = runner.advance(authorized.run_id)

    assert authorized.active_stage is ScenarioStage.AUTHORIZED
    assert authorized.graph_version == "graph-v17"
    assert changed.active_stage is ScenarioStage.DECISION_CHANGED
    assert changed.graph_version == "graph-v18"
    assert stopped.active_stage is ScenarioStage.WORK_STOPPED
    assert stopped.old_execution is not None
    assert stopped.old_execution.verification_code is VerificationCode.STALE_SNAPSHOT
    assert stopped.agent_loop_state is LoopState.REPLAN
    assert completed.active_stage is ScenarioStage.REAUTHORIZED
    assert completed.status is ScenarioResultStatus.PASSED
    assert completed.agent_loop_state is LoopState.COMPLETE
    assert completed.new_execution is not None and completed.new_execution.applied
    assert completed.evaluation is not None
    assert completed.evaluation.actual_preserved_task_ids == [
        "TASK-101",
        "TASK-102",
        "TASK-103",
    ]
    assert completed.evaluation.actual_invalidated_task_ids == [
        "TASK-104",
        "TASK-105",
    ]
    assert len(completed.events) >= 13
    assert "token" not in completed.model_dump_json().lower()
    runtime = runner._runs[authorized.run_id]  # noqa: SLF001
    assert runtime.original_grant_token == ""
    assert runtime.agent_run.grant_token is None
    assert runtime.initial_authorization.grant is not None
    assert runtime.initial_authorization.grant.token == ""
    assert runtime.corrected_authorization is not None
    assert runtime.corrected_authorization.grant is not None
    assert runtime.corrected_authorization.grant.token == ""


def test_outcome_contract_separates_task_invalidation_from_plan_review() -> None:
    runner = build_runner()

    authorized = runner.start("csv-exports-admin-only")
    assert authorized.outcome_summary.preserved_task_ids == []
    assert authorized.outcome_summary.invalidated_task_ids == []
    assert authorized.outcome_summary.needs_review_artifact_ids == []
    assert authorized.outcome_summary.original_plan_status is ValidityStatus.VALID
    assert authorized.outcome_summary.old_grant_verification_code is None
    assert authorized.outcome_summary.replacement_authorization_verdict is None
    assert authorized.outcome_summary.replacement_grant_verification_code is None
    assert authorized.outcome_summary.may_continue is True
    assert authorized.outcome_summary.primary_provenance_path == []
    assert authorized.outcome_summary.history_scope == "session"
    assert all(
        action.lifecycle is ScenarioCorrectiveActionLifecycle.FIXTURE_PREVIEW
        for action in authorized.outcome_summary.corrective_actions
    )

    changed = runner.advance(authorized.run_id)
    report = changed.invalidation_report
    assert report is not None
    assert report.preserved_task_ids == ["TASK-101", "TASK-102", "TASK-103"]
    assert report.invalidated_task_ids == ["TASK-104", "TASK-105"]
    assert report.needs_review_artifact_ids == [
        "SPEC-009",
        "TICKET-100",
        "PLAN-027",
    ]
    assert report.stopped_work_artifact_ids == [
        "TASK-104",
        "TASK-105",
        "PLAN-027",
    ]
    assert changed.outcome_summary.preserved_task_ids == [
        "TASK-101",
        "TASK-102",
        "TASK-103",
    ]
    assert changed.outcome_summary.invalidated_task_ids == [
        "TASK-104",
        "TASK-105",
    ]
    assert changed.outcome_summary.needs_review_artifact_ids == ["PLAN-027"]
    assert (
        changed.outcome_summary.original_plan_status
        is ValidityStatus.NEEDS_REVIEW
    )
    assert changed.outcome_summary.may_continue is False
    assert changed.outcome_summary.primary_provenance_path[0] == "DEC-018"
    assert changed.outcome_summary.primary_provenance_path[-1] == "PLAN-027"

    stopped = runner.advance(changed.run_id)
    assert (
        stopped.outcome_summary.old_grant_verification_code
        is VerificationCode.STALE_SNAPSHOT
    )
    assert stopped.outcome_summary.replacement_authorization_verdict is None
    assert stopped.outcome_summary.may_continue is False

    completed = runner.advance(stopped.run_id)
    assert (
        completed.outcome_summary.replacement_authorization_verdict
        is Verdict.ALLOW
    )
    assert (
        completed.outcome_summary.replacement_grant_verification_code
        is VerificationCode.VALID
    )
    assert completed.outcome_summary.may_continue is True
    assert all(
        action.lifecycle
        is ScenarioCorrectiveActionLifecycle.AUTHORIZED_PLAN_ACTION
        and action.graph_artifact_id is None
        and action.persisted_as_graph_artifact is False
        for action in completed.outcome_summary.corrective_actions
    )

    summary = runner.latest_runs()[0]
    assert summary.plan_status is ValidityStatus.NEEDS_REVIEW
    assert summary.needs_review_artifact_ids == ["PLAN-027"]
    assert summary.old_grant_verification_code is VerificationCode.STALE_SNAPSHOT
    assert summary.replacement_authorization_verdict is Verdict.ALLOW
    assert summary.replacement_grant_verification_code is VerificationCode.VALID
    assert summary.history_scope == "session"


def test_actual_outcomes_use_authority_report_typed_fields() -> None:
    runner = build_runner()
    authorized = runner.start("csv-exports-admin-only")
    runner.advance(authorized.run_id)
    runtime = runner._runs[authorized.run_id]  # noqa: SLF001
    assert runtime.mutation_result is not None
    assert runtime.mutation_result.report is not None
    report = runtime.mutation_result.report

    # If evaluation reconstructed impact from seed scopes or the legacy broad lists,
    # these changes would erase or corrupt the measured task sets.
    report.preserved_artifact_ids = []
    report.affected_artifact_ids = []
    runtime.definition.mutation.affected_scopes = set()

    preserved, invalidated, needs_review, newly_required = runner._actual_outcomes(  # noqa: SLF001
        runtime,
        runtime.definition.corrected_plan,
    )

    assert preserved == {"TASK-101", "TASK-102", "TASK-103"}
    assert invalidated == {"TASK-104", "TASK-105"}
    assert needs_review == {"PLAN-027"}
    assert newly_required == {"ACTION-6", "ACTION-7"}


def test_advance_retry_is_bound_to_the_expected_stage() -> None:
    runner = build_runner()
    authorized = runner.start("csv-exports-admin-only")

    changed = runner.advance(
        authorized.run_id,
        expected_stage=ScenarioStage.AUTHORIZED,
    )
    reconciled = runner.advance(
        authorized.run_id,
        expected_stage=ScenarioStage.AUTHORIZED,
    )

    assert changed.active_stage is ScenarioStage.DECISION_CHANGED
    assert reconciled.active_stage is ScenarioStage.DECISION_CHANGED
    assert reconciled.status is ScenarioResultStatus.RUNNING
    assert any(edge.kind.value == "SUPERSEDES" for edge in reconciled.edges)


def test_failed_stage_does_not_advance_and_remains_inspectable() -> None:
    transport = FailingMutationTransport()
    runner = ScenarioRunner(transport=transport)
    authorized = runner.start("csv-exports-admin-only")

    failed = runner.advance(
        authorized.run_id,
        expected_stage=ScenarioStage.AUTHORIZED,
    )

    assert failed.active_stage is ScenarioStage.AUTHORIZED
    assert failed.status is ScenarioResultStatus.FAILED
    assert failed.agent_loop_state is LoopState.BLOCKED
    assert failed.evaluation is not None
    assert failed.evaluation.failure_reasons == ["injected mutation failure"]
    assert failed.outcome_summary.original_plan_status is ValidityStatus.VALID
    assert failed.outcome_summary.needs_review_artifact_ids == []
    assert failed.outcome_summary.old_grant_verification_code is None
    assert failed.outcome_summary.replacement_authorization_verdict is None
    assert failed.outcome_summary.replacement_grant_verification_code is None
    assert failed.outcome_summary.may_continue is False
    assert all(
        action.lifecycle is ScenarioCorrectiveActionLifecycle.FIXTURE_PREVIEW
        for action in failed.outcome_summary.corrective_actions
    )
    assert runner.get(authorized.run_id).status is ScenarioResultStatus.FAILED
    summary = runner.latest_runs()[0]
    assert summary.run_id == authorized.run_id
    assert summary.inspectable is True
    assert summary.plan_status is ValidityStatus.VALID
    assert summary.needs_review_artifact_ids == []
    assert summary.old_grant_verification_code is None
    assert summary.replacement_authorization_verdict is None
    assert summary.replacement_grant_verification_code is None
    with pytest.raises(ScenarioAuthorityContextNotFound):
        transport.registry.state(authorized.context_id)


def test_initial_authorization_failure_deletes_created_context() -> None:
    transport = FailingInitialAuthorizationTransport()
    runner = ScenarioRunner(transport=transport)

    with pytest.raises(RuntimeError, match="injected initial authorization failure"):
        runner.start("csv-exports-admin-only")

    assert len(transport.deleted_context_ids) == 1
    with pytest.raises(ScenarioAuthorityContextNotFound):
        transport.registry.state(transport.deleted_context_ids[0])


def test_unsafe_old_grant_acceptance_never_claims_work_was_stopped() -> None:
    runner = ScenarioRunner(transport=UnsafeExecutorTransport())
    authorized = runner.start("csv-exports-admin-only")
    changed = runner.advance(authorized.run_id)

    failed = runner.advance(changed.run_id)

    assert failed.active_stage is ScenarioStage.DECISION_CHANGED
    assert failed.status is ScenarioResultStatus.FAILED
    assert failed.old_execution is not None and failed.old_execution.applied
    assert all(event.event_type != "executor.rejected" for event in failed.events)
    assert failed.evaluation is not None
    assert "did not reject the old grant" in failed.evaluation.failure_reasons[0]
    assert failed.agent_loop_state is LoopState.BLOCKED
    assert (
        failed.outcome_summary.original_plan_status
        is ValidityStatus.NEEDS_REVIEW
    )
    assert failed.outcome_summary.needs_review_artifact_ids == ["PLAN-027"]
    assert (
        failed.outcome_summary.old_grant_verification_code
        is VerificationCode.VALID
    )
    assert failed.outcome_summary.replacement_authorization_verdict is None
    assert failed.outcome_summary.replacement_grant_verification_code is None
    assert failed.outcome_summary.may_continue is False


def test_repository_failure_still_clears_tokens_and_authority_context() -> None:
    transport = InProcessScenarioTransport()
    runner = ScenarioRunner(
        transport=transport,
        repository=FailingScenarioRepository(),
    )
    authorized = runner.start("csv-exports-admin-only")
    changed = runner.advance(authorized.run_id)
    stopped = runner.advance(changed.run_id)

    with pytest.raises(RuntimeError, match="injected repository failure"):
        runner.advance(stopped.run_id)

    runtime = runner._runs[authorized.run_id]  # noqa: SLF001
    assert runtime.cleaned_up is True
    assert runtime.original_grant_token == ""
    assert runtime.agent_run.grant_token is None
    assert runtime.initial_authorization.grant is not None
    assert runtime.initial_authorization.grant.token == ""
    assert runtime.corrected_authorization is not None
    assert runtime.corrected_authorization.grant is not None
    assert runtime.corrected_authorization.grant.token == ""
    with pytest.raises(ScenarioAuthorityContextNotFound):
        transport.registry.state(authorized.context_id)


def test_runs_are_isolated_and_receive_unique_bindings() -> None:
    runner = build_runner()

    first = runner.start("csv-exports-admin-only")
    second = runner.start("csv-exports-admin-only")
    changed = runner.advance(first.run_id)
    untouched = runner.get(second.run_id)

    assert first.run_id != second.run_id
    assert first.context_id != second.context_id
    assert changed.graph_version == "graph-v18"
    assert untouched.graph_version == "graph-v17"


def test_run_all_executes_every_scenario_and_stores_real_results() -> None:
    runner = build_runner()

    report = runner.run_all()

    assert report.completed == 12
    assert report.passed == 12
    assert report.failed == 0
    assert all(item.old_grant_rejected for item in report.runs)
    assert all(item.reauthorization_succeeded for item in report.runs)
    assert all(item.status is ScenarioResultStatus.PASSED for item in report.runs)
    assert all(
        item.preserved_actual_ids == item.preserved_expected_ids
        for item in report.runs
    )
    assert all(
        item.invalidated_actual_ids == item.invalidated_expected_ids
        for item in report.runs
    )
    assert all(not item.false_positive_invalidations for item in report.runs)
    assert all(not item.missed_invalidations for item in report.runs)
    assert all(item.plan_status is ValidityStatus.NEEDS_REVIEW for item in report.runs)
    assert all(
        item.needs_review_artifact_ids
        == [get_scenario(item.scenario_id).initial_run.plan.id]
        for item in report.runs
    )
    assert all(
        item.old_grant_verification_code is VerificationCode.STALE_SNAPSHOT
        for item in report.runs
    )
    assert all(
        item.replacement_authorization_verdict is Verdict.ALLOW
        for item in report.runs
    )
    assert all(
        item.replacement_grant_verification_code is VerificationCode.VALID
        for item in report.runs
    )
    assert all(item.history_scope == "session" for item in report.runs)
    assert len(runner.latest_runs()) == 12


def test_run_all_prevalidates_ids_and_respects_an_explicit_empty_selection() -> None:
    runner = build_runner()

    empty = runner.run_all([])

    assert empty.completed == 0
    assert empty.runs == []
    with pytest.raises(KeyError, match="Unknown scenario: missing"):
        runner.run_all(["csv-exports-admin-only", "missing"])
    assert runner.latest_runs() == []
