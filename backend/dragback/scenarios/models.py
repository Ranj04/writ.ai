from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from dragback.domain import (
    AgentPlan,
    AgentRun,
    Artifact,
    DecisionMutation,
    Edge,
    Verdict,
)


class ScenarioCategory(StrEnum):
    COMPLIANCE = "compliance"
    SECURITY = "security"
    PRODUCT = "product"
    INFRASTRUCTURE = "infrastructure"
    PRIVACY = "privacy"
    FINANCE = "finance"
    ACCESS_CONTROL = "access-control"
    DATA_GOVERNANCE = "data-governance"


class ScenarioRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ScenarioMetadata(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: ScenarioCategory
    description: str = Field(min_length=1)
    risk_level: ScenarioRiskLevel


class ScenarioNarrative(BaseModel):
    why_changed: str = Field(min_length=1)
    expected_corrected_behavior: str = Field(min_length=1)
    risk_if_old_authorization_continues: str = Field(min_length=1)


class ScenarioGraphSeed(BaseModel):
    version: int = Field(ge=0)
    artifacts: list[Artifact] = Field(min_length=1)
    edges: list[Edge] = Field(min_length=1)


class ScenarioPresentation(BaseModel):
    selector_summary: str = Field(min_length=1)
    preserved_work: list[str] = Field(min_length=1)
    invalidated_work: list[str] = Field(min_length=1)
    newly_required_work: list[str] = Field(min_length=1)
    old_grant_rejection_copy: str = Field(min_length=1)
    demo_takeaway: str = Field(min_length=1)


class ScenarioExpectation(BaseModel):
    """Assertion-only outcomes; these values never drive authority behavior."""

    preserved_task_ids: frozenset[str] = Field(min_length=1)
    invalidated_task_ids: frozenset[str] = Field(min_length=1)
    needs_review_artifact_ids: frozenset[str] = Field(default_factory=frozenset)
    newly_required_action_ids: frozenset[str] = Field(min_length=1)
    conflict_verdict: Verdict = Verdict.REPLAN
    old_grant_should_be_rejected: bool = True
    corrected_plan_should_be_authorized: bool = True
    replacement_grant_should_execute: bool = True


class ScenarioDefinition(BaseModel):
    metadata: ScenarioMetadata
    narrative: ScenarioNarrative
    graph_seed: ScenarioGraphSeed
    initial_run: AgentRun
    mutation: DecisionMutation
    corrected_plan: AgentPlan
    authority_policy: dict[str, set[str]]
    presentation: ScenarioPresentation
    expectations: ScenarioExpectation

    @model_validator(mode="after")
    def validate_definition(self) -> ScenarioDefinition:
        from dragback.scenarios.validation import validate_scenario_definition

        validate_scenario_definition(self)
        return self
