from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from writai.domain import (
    AgentPlan,
    Artifact,
    Edge,
    GrantPayload,
    InvalidationReport,
    LoopState,
    PlanMismatch,
    ValidityStatus,
    Verdict,
    VerificationCode,
)
from writai.integrations.callwright import CallReceipt
from writai.scenarios.models import ScenarioCategory, ScenarioRiskLevel


class ScenarioResultStatus(StrEnum):
    NOT_RUN = "not-run"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


class ScenarioStage(StrEnum):
    AUTHORIZED = "authorized"
    DECISION_CHANGED = "decision-changed"
    WORK_STOPPED = "work-stopped"
    REAUTHORIZED = "reauthorized"


class ScenarioRunRequest(BaseModel):
    scenario_id: str = Field(min_length=1, max_length=128)


class ScenarioAdvanceRequest(BaseModel):
    """Optional optimistic-stage binding for safe retries after a lost response."""

    expected_stage: ScenarioStage | None = None


class ScenarioRunAllRequest(BaseModel):
    scenario_ids: list[str] | None = None


class ScenarioAuthorizationView(BaseModel):
    verdict: Verdict
    reason: str
    graph_version: str
    task_id: str
    affected_scopes: set[str] = Field(default_factory=set)
    mismatches: list[PlanMismatch] = Field(default_factory=list)
    current_requirements: dict[str, dict[str, Any]] = Field(default_factory=dict)
    invalidation_path: list[str] = Field(default_factory=list)
    invalidated_artifact_ids: list[str] = Field(default_factory=list)
    preserved_artifact_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    grant: GrantPayload | None = None


class ScenarioExecutionResult(BaseModel):
    applied: bool
    reason: str
    verification_code: VerificationCode
    pull_request_url: str | None = None
    execution_mode: Literal["simulated", "live"] | None = None
    call_receipt: CallReceipt | None = None


class ScenarioCorrectiveActionLifecycle(StrEnum):
    FIXTURE_PREVIEW = "fixture-preview"
    AUTHORIZED_PLAN_ACTION = "authorized-plan-action"


class ScenarioCorrectiveActionView(BaseModel):
    id: str
    description: str
    scopes: set[str] = Field(default_factory=set)
    source: Literal["fixture"] = "fixture"
    representation: Literal["plan-action"] = "plan-action"
    graph_artifact_id: None = None
    persisted_as_graph_artifact: Literal[False] = False
    lifecycle: ScenarioCorrectiveActionLifecycle


class ScenarioOutcomeSummary(BaseModel):
    preserved_task_ids: list[str] = Field(default_factory=list)
    invalidated_task_ids: list[str] = Field(default_factory=list)
    needs_review_artifact_ids: list[str] = Field(default_factory=list)
    original_plan_id: str
    original_plan_status: ValidityStatus
    corrective_actions: list[ScenarioCorrectiveActionView] = Field(default_factory=list)
    old_grant_verification_code: VerificationCode | None = None
    replacement_authorization_verdict: Verdict | None = None
    replacement_grant_verification_code: VerificationCode | None = None
    may_continue: bool
    primary_provenance_path: list[str] = Field(default_factory=list)
    history_scope: Literal["session"] = "session"


class ScenarioEvent(BaseModel):
    sequence: int = Field(ge=1)
    stage: ScenarioStage
    event_type: str
    label: str
    detail: str
    created_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class ScenarioEvaluationCheck(BaseModel):
    id: str
    label: str
    expected: str
    actual: str
    passed: bool


class ScenarioEvaluation(BaseModel):
    status: ScenarioResultStatus
    checks: list[ScenarioEvaluationCheck]
    runtime_ms: float = Field(ge=0)
    actual_preserved_task_ids: list[str] = Field(default_factory=list)
    actual_invalidated_task_ids: list[str] = Field(default_factory=list)
    actual_needs_review_artifact_ids: list[str] = Field(default_factory=list)
    actual_newly_required_action_ids: list[str] = Field(default_factory=list)
    false_positive_invalidations: list[str] = Field(default_factory=list)
    missed_invalidations: list[str] = Field(default_factory=list)
    failure_reasons: list[str] = Field(default_factory=list)


class ScenarioRunView(BaseModel):
    run_id: str
    context_id: str
    scenario_id: str
    status: ScenarioResultStatus
    active_stage: ScenarioStage
    graph_version: str
    artifacts: list[Artifact]
    edges: list[Edge]
    started_at: datetime
    completed_at: datetime | None = None
    original_plan: AgentPlan
    corrected_plan: AgentPlan | None = None
    original_authorization: ScenarioAuthorizationView
    conflict_authorization: ScenarioAuthorizationView | None = None
    corrected_authorization: ScenarioAuthorizationView | None = None
    original_grant: GrantPayload | None = None
    replacement_grant: GrantPayload | None = None
    invalidation_report: InvalidationReport | None = None
    old_execution: ScenarioExecutionResult | None = None
    new_execution: ScenarioExecutionResult | None = None
    events: list[ScenarioEvent] = Field(default_factory=list)
    evaluation: ScenarioEvaluation | None = None
    agent_loop_state: LoopState
    agent_history: list[str] = Field(default_factory=list)
    outcome_summary: ScenarioOutcomeSummary


class ScenarioRunSummary(BaseModel):
    run_id: str
    scenario_id: str
    scenario_name: str
    category: ScenarioCategory
    risk_level: ScenarioRiskLevel
    status: ScenarioResultStatus
    preserved_expected: int = Field(ge=0)
    preserved_actual: int = Field(ge=0)
    preserved_expected_ids: list[str] = Field(default_factory=list)
    preserved_actual_ids: list[str] = Field(default_factory=list)
    invalidated_expected: int = Field(ge=0)
    invalidated_actual: int = Field(ge=0)
    invalidated_expected_ids: list[str] = Field(default_factory=list)
    invalidated_actual_ids: list[str] = Field(default_factory=list)
    false_positive_invalidations: list[str] = Field(default_factory=list)
    missed_invalidations: list[str] = Field(default_factory=list)
    old_grant_rejected_expected: bool
    old_grant_rejected: bool
    reauthorization_expected: bool
    reauthorization_succeeded: bool
    runtime_ms: float = Field(ge=0)
    failure_reasons: list[str] = Field(default_factory=list)
    completed_at: datetime
    inspectable: bool = True
    plan_status: ValidityStatus | None = None
    needs_review_artifact_ids: list[str] = Field(default_factory=list)
    old_grant_verification_code: VerificationCode | None = None
    replacement_authorization_verdict: Verdict | None = None
    replacement_grant_verification_code: VerificationCode | None = None
    history_scope: Literal["session"] = "session"


class ScenarioArtifactPreview(BaseModel):
    id: str
    title: str
    description: str
    scopes: set[str] = Field(default_factory=set)


class ScenarioTaskExpectedStatus(StrEnum):
    PRESERVED = "preserved"
    INVALIDATED = "invalidated"


class ScenarioTaskPreview(ScenarioArtifactPreview):
    expected_status: ScenarioTaskExpectedStatus


class ScenarioCatalogItem(BaseModel):
    id: str
    name: str
    category: ScenarioCategory
    description: str
    risk_level: ScenarioRiskLevel
    original_decision_id: str
    original_decision_text: str
    original_graph_version: str
    new_decision_id: str
    new_decision_text: str
    new_graph_version: str
    why_changed: str
    risk_if_old_authorization_continues: str
    expected_corrected_behavior: str
    selector_summary: str
    preserved_work: list[str]
    invalidated_work: list[str]
    newly_required_work: list[str]
    expected_preserved_count: int
    expected_invalidated_count: int
    provenance_node_count: int
    specification: ScenarioArtifactPreview
    ticket: ScenarioArtifactPreview
    tasks: list[ScenarioTaskPreview]
    initial_plan: AgentPlan
    corrective_actions: list[ScenarioCorrectiveActionView] = Field(default_factory=list)
    last_result: ScenarioResultStatus = ScenarioResultStatus.NOT_RUN
    last_run_at: datetime | None = None


class ScenarioCatalogResponse(BaseModel):
    scenarios: list[ScenarioCatalogItem]
    latest_runs: list[ScenarioRunSummary] = Field(default_factory=list)


class ScenarioRunAllReport(BaseModel):
    runs: list[ScenarioRunSummary]
    completed: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    total_runtime_ms: float = Field(ge=0)
    generated_at: datetime
