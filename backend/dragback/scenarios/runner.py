from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from time import perf_counter

from dragback.domain import (
    AgentPlan,
    AgentRun,
    Artifact,
    ArtifactKind,
    AuthorizationRequest,
    AuthorizationResult,
    Edge,
    EdgeKind,
    LoopState,
    MutationResult,
    ValidityStatus,
    Verdict,
    VerificationCode,
    utc_now,
)
from dragback.loop.workflow import apply_authorization_result
from dragback.provenance import select_primary_invalidation_path
from dragback.scenarios import ScenarioDefinition, get_scenario, list_scenarios
from dragback.scenarios.authority_contexts import ScenarioAuthorityContextCreateRequest
from dragback.scenarios.repository import (
    InMemoryScenarioRunRepository,
    ScenarioRunRepository,
)
from dragback.scenarios.run_models import (
    ScenarioArtifactPreview,
    ScenarioAuthorizationView,
    ScenarioCatalogItem,
    ScenarioCatalogResponse,
    ScenarioCorrectiveActionLifecycle,
    ScenarioCorrectiveActionView,
    ScenarioEvaluation,
    ScenarioEvaluationCheck,
    ScenarioEvent,
    ScenarioExecutionResult,
    ScenarioOutcomeSummary,
    ScenarioResultStatus,
    ScenarioRunAllReport,
    ScenarioRunSummary,
    ScenarioRunView,
    ScenarioStage,
    ScenarioTaskExpectedStatus,
    ScenarioTaskPreview,
)
from dragback.scenarios.transport import HttpScenarioTransport, ScenarioTransport


class ScenarioRunnerError(Exception):
    pass


class ScenarioRunNotFound(ScenarioRunnerError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"Unknown scenario run: {run_id}")
        self.run_id = run_id


class ScenarioRunConflict(ScenarioRunnerError):
    pass


@dataclass
class _ScenarioRuntime:
    definition: ScenarioDefinition
    run_id: str
    context_id: str
    agent_run: AgentRun
    original_plan: AgentPlan
    initial_authorization: AuthorizationResult
    original_grant_token: str
    graph_version: str
    started_at: datetime
    started_perf: float
    stage: ScenarioStage = ScenarioStage.AUTHORIZED
    status: ScenarioResultStatus = ScenarioResultStatus.RUNNING
    mutation_result: MutationResult | None = None
    conflict_authorization: AuthorizationResult | None = None
    corrected_authorization: AuthorizationResult | None = None
    old_execution: ScenarioExecutionResult | None = None
    new_execution: ScenarioExecutionResult | None = None
    events: list[ScenarioEvent] = field(default_factory=list)
    evaluation: ScenarioEvaluation | None = None
    completed_at: datetime | None = None
    cleaned_up: bool = False


def _authorization_view(result: AuthorizationResult) -> ScenarioAuthorizationView:
    return ScenarioAuthorizationView(
        verdict=result.verdict,
        reason=result.reason,
        graph_version=result.graph_version,
        task_id=result.task_id,
        affected_scopes=result.affected_scopes,
        mismatches=result.mismatches,
        current_requirements=result.current_requirements,
        invalidation_path=result.invalidation_path,
        invalidated_artifact_ids=result.invalidated_artifact_ids,
        preserved_artifact_ids=result.preserved_artifact_ids,
        evidence_refs=result.evidence_refs,
        grant=result.grant.payload if result.grant else None,
    )


def _display_set(values: set[str] | frozenset[str]) -> str:
    return ", ".join(sorted(values)) if values else "None"


def _longest_provenance_node_count(definition: ScenarioDefinition) -> int:
    target = definition.initial_run.plan.id
    outgoing: dict[str, list[str]] = {}
    for edge in definition.graph_seed.edges:
        outgoing.setdefault(edge.source_id, []).append(edge.target_id)
    queue: deque[tuple[str, list[str]]] = deque(
        [(definition.mutation.supersedes_id, [definition.mutation.supersedes_id])]
    )
    visited: set[str] = set()
    longest = 1
    while queue:
        artifact_id, path = queue.popleft()
        if artifact_id in visited:
            continue
        visited.add(artifact_id)
        if artifact_id == target:
            longest = max(longest, len(path) + 1)
        for child_id in outgoing.get(artifact_id, []):
            queue.append((child_id, [*path, child_id]))
    return longest


def _artifact_preview(artifact: Artifact) -> ScenarioArtifactPreview:
    return ScenarioArtifactPreview(
        id=artifact.id,
        title=artifact.title,
        description=artifact.text,
        scopes=artifact.scopes,
    )


def _corrective_action_views(
    definition: ScenarioDefinition,
    *,
    lifecycle: ScenarioCorrectiveActionLifecycle,
) -> list[ScenarioCorrectiveActionView]:
    original_action_ids = {action.id for action in definition.initial_run.plan.actions}
    return [
        ScenarioCorrectiveActionView(
            id=action.id,
            description=action.description,
            scopes=action.scopes,
            lifecycle=lifecycle,
        )
        for action in definition.corrected_plan.actions
        if action.id not in original_action_ids
    ]


class ScenarioRunner:
    """Agent-owned orchestration over isolated authority and executor services."""

    _MAX_RETAINED_RUNS_PER_SCENARIO = 5

    def __init__(
        self,
        *,
        transport: ScenarioTransport | None = None,
        repository: ScenarioRunRepository | None = None,
    ) -> None:
        self._transport = transport or HttpScenarioTransport()
        self._repository = repository or InMemoryScenarioRunRepository()
        self._runs: dict[str, _ScenarioRuntime] = {}
        self._lock = RLock()
        self._batch_running = False

    def catalog(self) -> ScenarioCatalogResponse:
        latest = {summary.scenario_id: summary for summary in self._repository.list_latest()}
        items: list[ScenarioCatalogItem] = []
        for definition in list_scenarios():
            artifacts = definition.graph_seed.artifacts
            original = next(
                artifact
                for artifact in artifacts
                if artifact.id == definition.mutation.supersedes_id
            )
            specification = next(
                artifact for artifact in artifacts if artifact.kind is ArtifactKind.SPECIFICATION
            )
            ticket = next(
                artifact
                for artifact in artifacts
                if artifact.id == definition.initial_run.ticket_id
            )
            task_previews = [
                ScenarioTaskPreview(
                    **_artifact_preview(artifact).model_dump(),
                    expected_status=(
                        ScenarioTaskExpectedStatus.PRESERVED
                        if artifact.id in definition.expectations.preserved_task_ids
                        else ScenarioTaskExpectedStatus.INVALIDATED
                    ),
                )
                for artifact in artifacts
                if artifact.kind is ArtifactKind.TASK
            ]
            summary = latest.get(definition.metadata.id)
            items.append(
                ScenarioCatalogItem(
                    id=definition.metadata.id,
                    name=definition.metadata.name,
                    category=definition.metadata.category,
                    description=definition.metadata.description,
                    risk_level=definition.metadata.risk_level,
                    original_decision_id=original.id,
                    original_decision_text=original.text,
                    original_graph_version=(f"graph-v{definition.graph_seed.version}"),
                    new_decision_id=definition.mutation.decision.id,
                    new_decision_text=definition.mutation.decision.text,
                    new_graph_version=(f"graph-v{definition.graph_seed.version + 1}"),
                    why_changed=definition.narrative.why_changed,
                    risk_if_old_authorization_continues=(
                        definition.narrative.risk_if_old_authorization_continues
                    ),
                    expected_corrected_behavior=(definition.narrative.expected_corrected_behavior),
                    selector_summary=definition.presentation.selector_summary,
                    preserved_work=definition.presentation.preserved_work,
                    invalidated_work=definition.presentation.invalidated_work,
                    newly_required_work=definition.presentation.newly_required_work,
                    expected_preserved_count=len(definition.expectations.preserved_task_ids),
                    expected_invalidated_count=len(definition.expectations.invalidated_task_ids),
                    provenance_node_count=_longest_provenance_node_count(definition),
                    specification=_artifact_preview(specification),
                    ticket=_artifact_preview(ticket),
                    tasks=task_previews,
                    initial_plan=definition.initial_run.plan.model_copy(deep=True),
                    corrective_actions=_corrective_action_views(
                        definition,
                        lifecycle=ScenarioCorrectiveActionLifecycle.FIXTURE_PREVIEW,
                    ),
                    last_result=(summary.status if summary else ScenarioResultStatus.NOT_RUN),
                    last_run_at=summary.completed_at if summary else None,
                )
            )
        return ScenarioCatalogResponse(
            scenarios=items,
            latest_runs=self._repository.list_latest(),
        )

    def definition(self, scenario_id: str) -> ScenarioDefinition:
        return get_scenario(scenario_id)

    def _event(
        self,
        runtime: _ScenarioRuntime,
        *,
        event_type: str,
        label: str,
        detail: str,
        data: dict[str, object] | None = None,
    ) -> None:
        runtime.events.append(
            ScenarioEvent(
                sequence=len(runtime.events) + 1,
                stage=runtime.stage,
                event_type=event_type,
                label=label,
                detail=detail,
                created_at=utc_now(),
                data=data or {},
            )
        )

    def start(self, scenario_id: str, *, allow_during_batch: bool = False) -> ScenarioRunView:
        with self._lock:
            if self._batch_running and not allow_during_batch:
                raise ScenarioRunConflict("Run all is already in progress.")
            definition = get_scenario(scenario_id)
            unique = uuid.uuid4().hex[:12]
            context_id = f"ctx-{scenario_id}-{unique}"
            run_id = f"{definition.initial_run.run_id}-{unique.upper()}"
            self._transport.create_context(
                ScenarioAuthorityContextCreateRequest(
                    context_id=context_id,
                    scenario_id=scenario_id,
                )
            )
            plan = definition.initial_run.plan.model_copy(deep=True)
            agent_run = definition.initial_run.model_copy(deep=True)
            agent_run.run_id = run_id
            agent_run.plan = plan.model_copy(deep=True)
            agent_run.state = LoopState.VERIFY
            try:
                initial = self._transport.authorize(
                    context_id,
                    AuthorizationRequest(
                        run_id=run_id,
                        task_id=definition.initial_run.ticket_id,
                        plan=plan,
                    ),
                )
            except Exception:
                try:
                    self._transport.delete_context(context_id)
                except Exception:
                    pass
                raise
            if initial.verdict is not Verdict.ALLOW or initial.grant is None:
                try:
                    self._transport.delete_context(context_id)
                except Exception:
                    pass
                raise ScenarioRunConflict(
                    "The scenario baseline did not receive an ALLOW authorization."
                )
            apply_authorization_result(agent_run, initial)
            agent_run.history.append(f"Initial verification: {initial.verdict.value}")

            runtime = _ScenarioRuntime(
                definition=definition,
                run_id=run_id,
                context_id=context_id,
                agent_run=agent_run,
                original_plan=plan,
                initial_authorization=initial,
                original_grant_token=initial.grant.token,
                graph_version=initial.graph_version,
                started_at=utc_now(),
                started_perf=perf_counter(),
            )
            self._event(
                runtime,
                event_type="authorization.issued",
                label="Original authorization issued",
                detail=(
                    f"{initial.grant.payload.authorization_id} is bound to {initial.graph_version}."
                ),
            )
            self._event(
                runtime,
                event_type="agent.work.started",
                label="Agent begins work",
                detail=f"{plan.id} is active for {plan.ticket_id}.",
            )
            self._runs[run_id] = runtime
            return self._view(runtime)

    def get(self, run_id: str) -> ScenarioRunView:
        with self._lock:
            try:
                runtime = self._runs[run_id]
            except KeyError as exc:
                raise ScenarioRunNotFound(run_id) from exc
            return self._view(runtime)

    def advance(
        self,
        run_id: str,
        *,
        expected_stage: ScenarioStage | None = None,
    ) -> ScenarioRunView:
        with self._lock:
            try:
                runtime = self._runs[run_id]
            except KeyError as exc:
                raise ScenarioRunNotFound(run_id) from exc
            if expected_stage is not None and runtime.stage is not expected_stage:
                # A previous request may have committed even if its response was
                # lost. Returning current state makes the retry idempotent.
                return self._view(runtime)
            if runtime.status is not ScenarioResultStatus.RUNNING:
                raise ScenarioRunConflict("The scenario run is already complete.")
            try:
                if runtime.stage is ScenarioStage.AUTHORIZED:
                    self._apply_decision(runtime)
                elif runtime.stage is ScenarioStage.DECISION_CHANGED:
                    self._stop_conflicting_work(runtime)
                elif runtime.stage is ScenarioStage.WORK_STOPPED:
                    self._reauthorize(runtime)
                else:
                    raise ScenarioRunConflict("The scenario run cannot advance.")
            except Exception as exc:
                self._record_failure(runtime, str(exc))
            return self._view(runtime)

    def _apply_decision(self, runtime: _ScenarioRuntime) -> None:
        mutation = runtime.definition.mutation
        result = self._transport.apply_mutation(runtime.context_id)
        if not result.applied or result.report is None:
            raise ScenarioRunConflict(result.reason)

        runtime.stage = ScenarioStage.DECISION_CHANGED
        runtime.mutation_result = result
        runtime.graph_version = result.graph_version
        self._event(
            runtime,
            event_type="decision.received",
            label="New decision received",
            detail=mutation.decision.text,
        )
        self._event(
            runtime,
            event_type="graph.traversal.started",
            label="Graph traversal starts",
            detail=(
                f"The authority follows typed downstream relationships from {mutation.decision.id}."
            ),
        )
        report = result.report
        self._event(
            runtime,
            event_type="graph.impact.identified",
            label="Impacted nodes identified",
            detail=f"{len(report.affected_artifact_ids)} artifacts intersect the changed scope.",
            data={"artifact_ids": report.affected_artifact_ids},
        )
        self._event(
            runtime,
            event_type="graph.work.preserved",
            label="Unaffected work preserved",
            detail=f"{len(report.preserved_artifact_ids)} artifacts remain valid.",
            data={"artifact_ids": report.preserved_artifact_ids},
        )
        self._event(
            runtime,
            event_type="graph.work.invalidated",
            label="Conflicting work invalidated",
            detail=", ".join(report.stopped_work_artifact_ids),
            data={"artifact_ids": report.stopped_work_artifact_ids},
        )

    def _stop_conflicting_work(self, runtime: _ScenarioRuntime) -> None:
        old_execution = self._transport.execute(
            context_id=runtime.context_id,
            token=runtime.original_grant_token,
            run_id=runtime.run_id,
            task_id=runtime.definition.initial_run.ticket_id,
            plan=runtime.original_plan,
        )
        runtime.old_execution = old_execution
        if (
            old_execution.applied
            or old_execution.verification_code is not VerificationCode.STALE_SNAPSHOT
        ):
            raise ScenarioRunConflict(
                "Safety invariant failed: the executor did not reject the old grant "
                "with STALE_SNAPSHOT."
            )
        conflict_authorization = self._transport.authorize(
            runtime.context_id,
            AuthorizationRequest(
                run_id=runtime.run_id,
                task_id=runtime.definition.initial_run.ticket_id,
                plan=runtime.original_plan,
            ),
        )
        runtime.conflict_authorization = conflict_authorization
        if conflict_authorization.verdict is not Verdict.REPLAN:
            raise ScenarioRunConflict(
                "Safety invariant failed: the conflicting plan did not receive REPLAN."
            )

        runtime.stage = ScenarioStage.WORK_STOPPED
        apply_authorization_result(runtime.agent_run, conflict_authorization)
        runtime.agent_run.history.append(
            f"Reauthorization: {conflict_authorization.verdict.value}"
        )
        self._event(
            runtime,
            event_type="grant.invalidated",
            label="Old grant is no longer usable",
            detail=old_execution.reason,
            data={"verification_code": old_execution.verification_code.value},
        )
        self._event(
            runtime,
            event_type="executor.rejected",
            label="Executor rejects old grant",
            detail=old_execution.reason,
        )
        self._event(
            runtime,
            event_type="agent.replan.required",
            label="Agent loop enters REPLAN",
            detail=conflict_authorization.reason,
        )

    def _reauthorize(self, runtime: _ScenarioRuntime) -> None:
        corrected = runtime.definition.corrected_plan.model_copy(deep=True)
        next_agent_run = runtime.agent_run.model_copy(deep=True)
        next_agent_run.plan = corrected.model_copy(deep=True)
        next_agent_run.state = LoopState.VERIFY
        corrected_authorization = self._transport.authorize(
            runtime.context_id,
            AuthorizationRequest(
                run_id=runtime.run_id,
                task_id=runtime.definition.initial_run.ticket_id,
                plan=corrected,
            ),
        )
        runtime.corrected_authorization = corrected_authorization
        corrected_grant = corrected_authorization.grant
        if corrected_authorization.verdict is not Verdict.ALLOW or corrected_grant is None:
            raise ScenarioRunConflict(
                "Safety invariant failed: the corrected plan did not receive ALLOW."
            )
        new_execution = self._transport.execute(
            context_id=runtime.context_id,
            token=corrected_grant.token,
            run_id=runtime.run_id,
            task_id=runtime.definition.initial_run.ticket_id,
            plan=corrected,
        )
        runtime.new_execution = new_execution
        if (
            not new_execution.applied
            or new_execution.verification_code is not VerificationCode.VALID
        ):
            raise ScenarioRunConflict(
                "Safety invariant failed: the replacement grant was not accepted as VALID."
            )

        runtime.stage = ScenarioStage.REAUTHORIZED
        apply_authorization_result(next_agent_run, corrected_authorization)
        next_agent_run.state = LoopState.COMPLETE
        next_agent_run.history.append(
            f"Corrected plan verification: {corrected_authorization.verdict.value}"
        )
        next_agent_run.history.append("Replacement grant verified; execution resumed.")
        runtime.agent_run = next_agent_run
        self._event(
            runtime,
            event_type="agent.plan.corrected",
            label="Agent submits corrected plan",
            detail=runtime.definition.narrative.expected_corrected_behavior,
            data={"plan_id": corrected.id, "source": "fixture"},
        )
        self._event(
            runtime,
            event_type="plan.evaluated",
            label="Corrected plan evaluated",
            detail=corrected_authorization.reason,
        )
        self._event(
            runtime,
            event_type="authorization.reissued",
            label="New authorization issued",
            detail=(
                f"{corrected_grant.payload.authorization_id} is bound to "
                f"{corrected_grant.payload.decision_snapshot}."
            ),
        )
        self._event(
            runtime,
            event_type="executor.resumed",
            label="Execution resumes",
            detail=new_execution.reason,
        )
        runtime.completed_at = utc_now()
        runtime.evaluation = self._evaluate(runtime, corrected)
        runtime.status = runtime.evaluation.status
        summary = self._summary(runtime)
        try:
            self._repository.save(summary)
        finally:
            self._cleanup(runtime)
            self._prune_completed_runs(runtime.definition.metadata.id)

    def _actual_outcomes(
        self, runtime: _ScenarioRuntime, corrected: AgentPlan
    ) -> tuple[set[str], set[str], set[str], set[str]]:
        report = runtime.mutation_result.report if runtime.mutation_result else None
        if report is None:
            return set(), set(), set(), set()
        artifacts = {artifact.id: artifact for artifact in runtime.definition.graph_seed.artifacts}
        preserved = set(report.preserved_task_ids)
        invalidated = set(report.invalidated_task_ids)
        needs_review = {
            artifact_id
            for artifact_id in report.needs_review_artifact_ids
            if artifacts.get(artifact_id) is not None
            and artifacts[artifact_id].kind
            in {ArtifactKind.TASK, ArtifactKind.AGENT_PLAN}
        }
        initial_action_ids = {action.id for action in runtime.original_plan.actions}
        newly_required = {
            action.id for action in corrected.actions if action.id not in initial_action_ids
        }
        return preserved, invalidated, needs_review, newly_required

    def _evaluate(self, runtime: _ScenarioRuntime, corrected: AgentPlan) -> ScenarioEvaluation:
        expectation = runtime.definition.expectations
        actual_preserved, actual_invalidated, actual_review, actual_new = self._actual_outcomes(
            runtime, corrected
        )
        report = runtime.mutation_result.report if runtime.mutation_result else None
        old_rejected = bool(
            runtime.old_execution
            and not runtime.old_execution.applied
            and runtime.old_execution.verification_code is VerificationCode.STALE_SNAPSHOT
        )
        corrected_allowed = bool(
            runtime.corrected_authorization
            and runtime.corrected_authorization.verdict is Verdict.ALLOW
            and runtime.corrected_authorization.grant is not None
        )
        replacement_executed = bool(runtime.new_execution and runtime.new_execution.applied)
        conflict_verdict = (
            runtime.conflict_authorization.verdict if runtime.conflict_authorization else None
        )
        expected_version = f"graph-v{runtime.definition.graph_seed.version + 1}"
        actual_version = (
            runtime.mutation_result.graph_version
            if runtime.mutation_result
            else runtime.graph_version
        )

        raw_checks = [
            (
                "baseline-allow",
                "Baseline plan authorized",
                Verdict.ALLOW.value,
                runtime.initial_authorization.verdict.value,
                runtime.initial_authorization.verdict is Verdict.ALLOW,
            ),
            (
                "graph-version",
                "Approved decision increments the graph once",
                expected_version,
                actual_version,
                actual_version == expected_version,
            ),
            (
                "preserved-set",
                "Valid tasks preserved",
                _display_set(expectation.preserved_task_ids),
                _display_set(actual_preserved),
                actual_preserved == set(expectation.preserved_task_ids),
            ),
            (
                "invalidated-set",
                "Conflicting tasks invalidated",
                _display_set(expectation.invalidated_task_ids),
                _display_set(actual_invalidated),
                actual_invalidated == set(expectation.invalidated_task_ids),
            ),
            (
                "needs-review-set",
                "Partially affected work requires review",
                _display_set(expectation.needs_review_artifact_ids),
                _display_set(actual_review),
                actual_review == set(expectation.needs_review_artifact_ids),
            ),
            (
                "new-actions-set",
                "Corrected plan adds required actions",
                _display_set(expectation.newly_required_action_ids),
                _display_set(actual_new),
                actual_new == set(expectation.newly_required_action_ids),
            ),
            (
                "old-grant",
                "Old grant rejected as stale",
                "STALE_SNAPSHOT",
                (
                    runtime.old_execution.verification_code.value
                    if runtime.old_execution
                    else "No execution"
                ),
                old_rejected == expectation.old_grant_should_be_rejected,
            ),
            (
                "conflict-verdict",
                "Original plan loses authorization",
                expectation.conflict_verdict.value,
                conflict_verdict.value if conflict_verdict else "None",
                conflict_verdict is expectation.conflict_verdict,
            ),
            (
                "corrected-plan",
                "Corrected plan receives ALLOW",
                "ALLOW",
                (
                    runtime.corrected_authorization.verdict.value
                    if runtime.corrected_authorization
                    else "None"
                ),
                corrected_allowed == expectation.corrected_plan_should_be_authorized,
            ),
            (
                "replacement-grant",
                "Replacement grant executes",
                "Accepted",
                "Accepted" if replacement_executed else "Rejected",
                replacement_executed == expectation.replacement_grant_should_execute,
            ),
            (
                "real-report",
                "Graph traversal produced evidence",
                "Report with paths",
                (f"{len(report.paths)} paths" if report is not None else "No report"),
                bool(report and report.paths),
            ),
        ]
        checks = [
            ScenarioEvaluationCheck(
                id=check_id,
                label=label,
                expected=expected,
                actual=actual,
                passed=passed,
            )
            for check_id, label, expected, actual, passed in raw_checks
        ]
        failure_reasons = [check.label for check in checks if not check.passed]
        runtime_ms = max(0.0, (perf_counter() - runtime.started_perf) * 1000)
        return ScenarioEvaluation(
            status=(
                ScenarioResultStatus.PASSED if not failure_reasons else ScenarioResultStatus.FAILED
            ),
            checks=checks,
            runtime_ms=runtime_ms,
            actual_preserved_task_ids=sorted(actual_preserved),
            actual_invalidated_task_ids=sorted(actual_invalidated),
            actual_needs_review_artifact_ids=sorted(actual_review),
            actual_newly_required_action_ids=sorted(actual_new),
            false_positive_invalidations=sorted(
                actual_invalidated - set(expectation.invalidated_task_ids)
            ),
            missed_invalidations=sorted(set(expectation.invalidated_task_ids) - actual_invalidated),
            failure_reasons=failure_reasons,
        )

    def _record_failure(self, runtime: _ScenarioRuntime, reason: str) -> None:
        """Persist a token-free, inspectable failure without advancing the stage."""

        if runtime.status is ScenarioResultStatus.FAILED:
            return
        message = reason.strip() or "The scenario stage failed without a reason."
        actual_preserved, actual_invalidated, actual_review, _ = self._actual_outcomes(
            runtime,
            runtime.original_plan,
        )
        self._event(
            runtime,
            event_type="scenario.failed",
            label="Scenario stage failed",
            detail=message,
        )
        runtime.completed_at = utc_now()
        runtime.evaluation = ScenarioEvaluation(
            status=ScenarioResultStatus.FAILED,
            checks=[
                ScenarioEvaluationCheck(
                    id="stage-safety",
                    label="Stage completed with required safety postconditions",
                    expected="Required postconditions satisfied",
                    actual=message,
                    passed=False,
                )
            ],
            runtime_ms=max(0.0, (perf_counter() - runtime.started_perf) * 1000),
            actual_preserved_task_ids=sorted(actual_preserved),
            actual_invalidated_task_ids=sorted(actual_invalidated),
            actual_needs_review_artifact_ids=sorted(actual_review),
            actual_newly_required_action_ids=[],
            false_positive_invalidations=sorted(
                actual_invalidated
                - set(runtime.definition.expectations.invalidated_task_ids)
            ),
            missed_invalidations=sorted(
                set(runtime.definition.expectations.invalidated_task_ids)
                - actual_invalidated
            ),
            failure_reasons=[message],
        )
        runtime.status = ScenarioResultStatus.FAILED
        runtime.agent_run.state = LoopState.BLOCKED
        runtime.agent_run.history.append(f"Scenario failed: {message}")
        try:
            self._repository.save(self._summary(runtime))
        finally:
            self._cleanup(runtime, suppress_errors=True)
            self._prune_completed_runs(runtime.definition.metadata.id)

    def _prune_completed_runs(self, scenario_id: str) -> None:
        completed = sorted(
            (
                runtime
                for runtime in self._runs.values()
                if runtime.definition.metadata.id == scenario_id
                and runtime.status is not ScenarioResultStatus.RUNNING
            ),
            key=lambda runtime: runtime.started_at,
            reverse=True,
        )
        for runtime in completed[self._MAX_RETAINED_RUNS_PER_SCENARIO :]:
            del self._runs[runtime.run_id]

    def _original_plan_status(self, runtime: _ScenarioRuntime) -> ValidityStatus:
        report = runtime.mutation_result.report if runtime.mutation_result else None
        if report is None:
            return ValidityStatus.VALID
        plan_id = runtime.original_plan.id
        if plan_id in report.needs_review_artifact_ids:
            return ValidityStatus.NEEDS_REVIEW
        if plan_id in report.affected_artifact_ids:
            return ValidityStatus.INVALIDATED
        return ValidityStatus.VALID

    def _outcome_summary(self, runtime: _ScenarioRuntime) -> ScenarioOutcomeSummary:
        report = runtime.mutation_result.report if runtime.mutation_result else None
        artifact_by_id = {
            artifact.id: artifact for artifact in runtime.definition.graph_seed.artifacts
        }
        review_required_work_ids = (
            [
                artifact_id
                for artifact_id in report.needs_review_artifact_ids
                if artifact_by_id.get(artifact_id) is not None
                and artifact_by_id[artifact_id].kind
                in {ArtifactKind.TASK, ArtifactKind.AGENT_PLAN}
            ]
            if report
            else []
        )
        primary = (
            select_primary_invalidation_path(
                report.paths,
                preferred_artifact_id=runtime.original_plan.id,
            )
            if report
            else None
        )
        primary_path = list(primary.node_ids) if primary is not None else []
        corrective_lifecycle = (
            ScenarioCorrectiveActionLifecycle.AUTHORIZED_PLAN_ACTION
            if runtime.corrected_authorization is not None
            and runtime.corrected_authorization.verdict is Verdict.ALLOW
            and runtime.corrected_authorization.grant is not None
            else ScenarioCorrectiveActionLifecycle.FIXTURE_PREVIEW
        )
        may_continue = bool(
            (
                runtime.stage is ScenarioStage.AUTHORIZED
                and runtime.status is ScenarioResultStatus.RUNNING
                and runtime.initial_authorization.verdict is Verdict.ALLOW
                and runtime.initial_authorization.grant is not None
            )
            or (
                runtime.stage is ScenarioStage.REAUTHORIZED
                and runtime.new_execution is not None
                and runtime.new_execution.applied
                and runtime.new_execution.verification_code is VerificationCode.VALID
            )
        )
        return ScenarioOutcomeSummary(
            preserved_task_ids=list(report.preserved_task_ids) if report else [],
            invalidated_task_ids=list(report.invalidated_task_ids) if report else [],
            needs_review_artifact_ids=review_required_work_ids,
            original_plan_id=runtime.original_plan.id,
            original_plan_status=self._original_plan_status(runtime),
            corrective_actions=_corrective_action_views(
                runtime.definition,
                lifecycle=corrective_lifecycle,
            ),
            old_grant_verification_code=(
                runtime.old_execution.verification_code
                if runtime.old_execution is not None
                else None
            ),
            replacement_authorization_verdict=(
                runtime.corrected_authorization.verdict
                if runtime.corrected_authorization is not None
                else None
            ),
            replacement_grant_verification_code=(
                runtime.new_execution.verification_code
                if runtime.new_execution is not None
                else None
            ),
            may_continue=may_continue,
            primary_provenance_path=primary_path,
        )

    def _view(self, runtime: _ScenarioRuntime) -> ScenarioRunView:
        corrected = runtime.corrected_authorization
        original_grant = runtime.initial_authorization.grant
        if original_grant is None:
            raise ScenarioRunConflict("The baseline authorization grant is missing.")
        artifacts = [
            artifact.model_copy(deep=True)
            for artifact in runtime.definition.graph_seed.artifacts
        ]
        edges = [edge.model_copy(deep=True) for edge in runtime.definition.graph_seed.edges]
        if runtime.mutation_result is not None:
            artifacts.append(runtime.definition.mutation.decision.model_copy(deep=True))
            edges.append(
                Edge(
                    source_id=runtime.definition.mutation.decision.id,
                    target_id=runtime.definition.mutation.supersedes_id,
                    kind=EdgeKind.SUPERSEDES,
                    scopes=set(runtime.definition.mutation.affected_scopes),
                    evidence_ref=runtime.definition.mutation.decision.source_ref,
                )
            )
            report = runtime.mutation_result.report
            if report is not None:
                affected_ids = set(report.affected_artifact_ids)
                for artifact in artifacts:
                    if artifact.id not in affected_ids:
                        continue
                    intersection = (
                        artifact.scopes
                        & runtime.definition.mutation.affected_scopes
                    )
                    artifact.invalidated_scopes |= intersection
                    artifact.validity = (
                        ValidityStatus.INVALIDATED
                        if artifact.scopes and intersection >= artifact.scopes
                        else ValidityStatus.NEEDS_REVIEW
                    )
        return ScenarioRunView(
            run_id=runtime.run_id,
            context_id=runtime.context_id,
            scenario_id=runtime.definition.metadata.id,
            status=runtime.status,
            active_stage=runtime.stage,
            graph_version=runtime.graph_version,
            artifacts=artifacts,
            edges=edges,
            started_at=runtime.started_at,
            completed_at=runtime.completed_at,
            original_plan=runtime.original_plan,
            corrected_plan=(
                runtime.definition.corrected_plan
                if runtime.stage is ScenarioStage.REAUTHORIZED
                else None
            ),
            original_authorization=_authorization_view(runtime.initial_authorization),
            conflict_authorization=(
                _authorization_view(runtime.conflict_authorization)
                if runtime.conflict_authorization
                else None
            ),
            corrected_authorization=(_authorization_view(corrected) if corrected else None),
            original_grant=original_grant.payload,
            replacement_grant=(corrected.grant.payload if corrected and corrected.grant else None),
            invalidation_report=(
                runtime.mutation_result.report if runtime.mutation_result else None
            ),
            old_execution=runtime.old_execution,
            new_execution=runtime.new_execution,
            events=runtime.events,
            evaluation=runtime.evaluation,
            agent_loop_state=runtime.agent_run.state,
            agent_history=runtime.agent_run.history,
            outcome_summary=self._outcome_summary(runtime),
        )

    def _summary(self, runtime: _ScenarioRuntime) -> ScenarioRunSummary:
        if runtime.evaluation is None or runtime.completed_at is None:
            raise ScenarioRunConflict("The scenario evaluation is not complete.")
        expectation = runtime.definition.expectations
        return ScenarioRunSummary(
            run_id=runtime.run_id,
            scenario_id=runtime.definition.metadata.id,
            scenario_name=runtime.definition.metadata.name,
            category=runtime.definition.metadata.category,
            risk_level=runtime.definition.metadata.risk_level,
            status=runtime.evaluation.status,
            preserved_expected=len(expectation.preserved_task_ids),
            preserved_actual=len(runtime.evaluation.actual_preserved_task_ids),
            preserved_expected_ids=sorted(expectation.preserved_task_ids),
            preserved_actual_ids=sorted(runtime.evaluation.actual_preserved_task_ids),
            invalidated_expected=len(expectation.invalidated_task_ids),
            invalidated_actual=len(runtime.evaluation.actual_invalidated_task_ids),
            invalidated_expected_ids=sorted(expectation.invalidated_task_ids),
            invalidated_actual_ids=sorted(
                runtime.evaluation.actual_invalidated_task_ids
            ),
            false_positive_invalidations=sorted(
                runtime.evaluation.false_positive_invalidations
            ),
            missed_invalidations=sorted(runtime.evaluation.missed_invalidations),
            old_grant_rejected_expected=expectation.old_grant_should_be_rejected,
            old_grant_rejected=bool(
                runtime.old_execution
                and not runtime.old_execution.applied
                and runtime.old_execution.verification_code is VerificationCode.STALE_SNAPSHOT
            ),
            reauthorization_expected=(
                expectation.corrected_plan_should_be_authorized
                and expectation.replacement_grant_should_execute
            ),
            reauthorization_succeeded=bool(
                runtime.corrected_authorization
                and runtime.corrected_authorization.verdict is Verdict.ALLOW
                and runtime.new_execution
                and runtime.new_execution.applied
            ),
            runtime_ms=runtime.evaluation.runtime_ms,
            failure_reasons=runtime.evaluation.failure_reasons,
            completed_at=runtime.completed_at,
            plan_status=self._original_plan_status(runtime),
            needs_review_artifact_ids=sorted(
                runtime.evaluation.actual_needs_review_artifact_ids
            ),
            old_grant_verification_code=(
                runtime.old_execution.verification_code
                if runtime.old_execution is not None
                else None
            ),
            replacement_authorization_verdict=(
                runtime.corrected_authorization.verdict
                if runtime.corrected_authorization is not None
                else None
            ),
            replacement_grant_verification_code=(
                runtime.new_execution.verification_code
                if runtime.new_execution is not None
                else None
            ),
        )

    def reset(self, scenario_id: str) -> None:
        with self._lock:
            if self._batch_running:
                raise ScenarioRunConflict("Run all is already in progress.")
            for run_id, runtime in list(self._runs.items()):
                if runtime.definition.metadata.id != scenario_id:
                    continue
                self._cleanup(runtime)
                del self._runs[run_id]

    def _cleanup(
        self,
        runtime: _ScenarioRuntime,
        *,
        suppress_errors: bool = False,
    ) -> None:
        if runtime.cleaned_up:
            return
        cleanup_error: Exception | None = None
        try:
            self._transport.delete_context(runtime.context_id)
        except Exception as exc:
            cleanup_error = exc
        finally:
            runtime.original_grant_token = ""
            runtime.agent_run.grant_token = None
            if runtime.initial_authorization.grant is not None:
                runtime.initial_authorization.grant.token = ""
            if (
                runtime.corrected_authorization is not None
                and runtime.corrected_authorization.grant is not None
            ):
                runtime.corrected_authorization.grant.token = ""
        if cleanup_error is None:
            runtime.cleaned_up = True
        elif not suppress_errors:
            raise cleanup_error

    def latest_runs(self) -> list[ScenarioRunSummary]:
        return self._repository.list_latest()

    def run_all(self, scenario_ids: list[str] | None = None) -> ScenarioRunAllReport:
        with self._lock:
            if self._batch_running:
                raise ScenarioRunConflict("Run all is already in progress.")
            self._batch_running = True
        try:
            selected = (
                scenario_ids
                if scenario_ids is not None
                else [definition.metadata.id for definition in list_scenarios()]
            )
            if len(selected) != len(set(selected)):
                raise ScenarioRunConflict("Run all scenario IDs must be unique.")
            definitions = [get_scenario(scenario_id) for scenario_id in selected]
            summaries: list[ScenarioRunSummary] = []
            for definition in definitions:
                scenario_id = definition.metadata.id
                active_run_id: str | None = None
                try:
                    view = self.start(scenario_id, allow_during_batch=True)
                    active_run_id = view.run_id
                    while view.status is ScenarioResultStatus.RUNNING:
                        view = self.advance(
                            view.run_id,
                            expected_stage=view.active_stage,
                        )
                    runtime = self._runs[view.run_id]
                    summaries.append(self._summary(runtime))
                except Exception as exc:
                    if active_run_id is not None and active_run_id in self._runs:
                        runtime = self._runs[active_run_id]
                        self._record_failure(runtime, str(exc))
                        summaries.append(self._summary(runtime))
                        continue
                    completed_at = utc_now()
                    failed = ScenarioRunSummary(
                        run_id=f"FAILED-{scenario_id}-{uuid.uuid4().hex[:8]}",
                        scenario_id=scenario_id,
                        scenario_name=definition.metadata.name,
                        category=definition.metadata.category,
                        risk_level=definition.metadata.risk_level,
                        status=ScenarioResultStatus.FAILED,
                        preserved_expected=len(definition.expectations.preserved_task_ids),
                        preserved_actual=0,
                        preserved_expected_ids=sorted(
                            definition.expectations.preserved_task_ids
                        ),
                        preserved_actual_ids=[],
                        invalidated_expected=len(definition.expectations.invalidated_task_ids),
                        invalidated_actual=0,
                        invalidated_expected_ids=sorted(
                            definition.expectations.invalidated_task_ids
                        ),
                        invalidated_actual_ids=[],
                        false_positive_invalidations=[],
                        missed_invalidations=sorted(
                            definition.expectations.invalidated_task_ids
                        ),
                        old_grant_rejected_expected=(
                            definition.expectations.old_grant_should_be_rejected
                        ),
                        old_grant_rejected=False,
                        reauthorization_expected=(
                            definition.expectations.corrected_plan_should_be_authorized
                            and definition.expectations.replacement_grant_should_execute
                        ),
                        reauthorization_succeeded=False,
                        runtime_ms=0,
                        failure_reasons=[str(exc)],
                        completed_at=completed_at,
                        inspectable=False,
                    )
                    self._repository.save(failed)
                    summaries.append(failed)
            return ScenarioRunAllReport(
                runs=summaries,
                completed=len(summaries),
                passed=sum(summary.status is ScenarioResultStatus.PASSED for summary in summaries),
                failed=sum(summary.status is ScenarioResultStatus.FAILED for summary in summaries),
                total_runtime_ms=sum(summary.runtime_ms for summary in summaries),
                generated_at=utc_now(),
            )
        finally:
            with self._lock:
                self._batch_running = False
