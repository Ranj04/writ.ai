from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from dragback.domain import (
    AgentPlan,
    AgentRun,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    DecisionMutation,
    Edge,
    EdgeKind,
    PlanAction,
)
from dragback.scenarios.models import (
    ScenarioCategory,
    ScenarioDefinition,
    ScenarioExpectation,
    ScenarioGraphSeed,
    ScenarioMetadata,
    ScenarioNarrative,
    ScenarioPresentation,
    ScenarioRiskLevel,
)

SCENARIO_AUTHORITY_POLICY: dict[str, set[str]] = {
    "agent.build": {"engineering", "security"},
    "agent.environment_access": {"security", "platform"},
    "agent.environment_access.production": {"security", "platform"},
    "agent.environment_access.staging": {"security", "platform"},
    "agent.merge_authority": {"security", "engineering"},
    "agent.release_approval": {"security", "engineering"},
    "agent.testing": {"engineering", "security"},
    "ai.evaluation": {"engineering", "product"},
    "ai.prompting": {"engineering", "product"},
    "ai.response_parsing": {"engineering"},
    "billing.domain": {"engineering", "finance"},
    "billing.invoice": {"engineering", "finance"},
    "billing.provider": {"compliance", "finance"},
    "data.backup": {"engineering", "platform"},
    "data.backup.crypto": {"engineering", "platform", "security"},
    "data.residency": {"compliance", "privacy", "security"},
    "data.schema": {"engineering"},
    "data.validation": {"engineering"},
    "export.audit": {"compliance", "engineering", "security"},
    "export.authorization": {"compliance", "product", "security"},
    "export.generation": {"engineering", "product"},
    "integration.authentication": {"engineering", "security"},
    "integration.mapping": {"engineering"},
    "integration.read": {"engineering", "product"},
    "logging.payload": {"compliance", "privacy", "security"},
    "migration.mapping": {"engineering", "platform"},
    "migration.reversibility": {"engineering", "platform"},
    "migration.schema": {"engineering", "platform"},
    "migration.transform": {"engineering", "platform"},
    "model.routing": {"privacy", "security"},
    "observability.infrastructure": {"engineering", "platform"},
    "observability.metadata": {"engineering", "security"},
    "privacy.deletion_scope": {"compliance", "privacy"},
    "records.write": {"product", "security"},
    "release.visibility": {"product", "security"},
    "telemetry.feature": {"engineering", "product"},
    "upload.file_types": {"product", "security"},
    "upload.interface": {"engineering", "product"},
    "upload.size_validation": {"engineering", "security"},
    "upload.storage": {"engineering", "platform"},
    "user.confirmation": {"engineering", "product"},
    "user.lookup": {"engineering", "product"},
    "user.primary_deletion": {"engineering", "privacy"},
}

SCENARIO_BASELINE_AUTHORITY_BY_SCOPE: dict[str, str] = {
    "agent.build": "engineering",
    "agent.environment_access": "security",
    "agent.environment_access.production": "security",
    "agent.environment_access.staging": "security",
    "agent.merge_authority": "security",
    "agent.release_approval": "security",
    "agent.testing": "engineering",
    "ai.evaluation": "product",
    "ai.prompting": "product",
    "ai.response_parsing": "engineering",
    "billing.domain": "finance",
    "billing.invoice": "finance",
    "billing.provider": "finance",
    "data.backup": "platform",
    "data.backup.crypto": "platform",
    "data.residency": "privacy",
    "data.schema": "engineering",
    "data.validation": "engineering",
    "export.audit": "compliance",
    "export.authorization": "compliance",
    "export.generation": "engineering",
    "integration.authentication": "security",
    "integration.mapping": "engineering",
    "integration.read": "product",
    "logging.payload": "privacy",
    "migration.mapping": "platform",
    "migration.reversibility": "platform",
    "migration.schema": "platform",
    "migration.transform": "platform",
    "model.routing": "privacy",
    "observability.infrastructure": "platform",
    "observability.metadata": "security",
    "privacy.deletion_scope": "privacy",
    "records.write": "product",
    "release.visibility": "product",
    "telemetry.feature": "product",
    "upload.file_types": "security",
    "upload.interface": "product",
    "upload.size_validation": "security",
    "upload.storage": "platform",
    "user.confirmation": "product",
    "user.lookup": "product",
    "user.primary_deletion": "privacy",
}


@dataclass(frozen=True)
class _TaskBlueprint:
    title: str
    description: str
    scope: str


@dataclass(frozen=True)
class _ActionBlueprint:
    title: str
    description: str
    scope: str


def _build_scenario(
    *,
    scenario_id: str,
    prefix: str,
    canonical_ids: bool,
    name: str,
    category: ScenarioCategory,
    risk_level: ScenarioRiskLevel,
    description: str,
    original_decision_title: str,
    original_decision_text: str,
    new_decision_title: str,
    new_decision_text: str,
    authority_role: str,
    why_changed: str,
    expected_corrected_behavior: str,
    risk_if_old_authorization_continues: str,
    requirements: dict[str, dict[str, Any]],
    changed_requirements: dict[str, dict[str, Any]],
    preserved_tasks: tuple[_TaskBlueprint, ...],
    invalidated_tasks: tuple[_TaskBlueprint, ...],
    new_actions: tuple[_ActionBlueprint, ...],
    demo_takeaway: str,
) -> ScenarioDefinition:
    if canonical_ids:
        original_decision_id = "DEC-004"
        new_decision_id = "DEC-018"
        specification_id = "SPEC-009"
        ticket_id = "TICKET-100"
        initial_plan_id = "PLAN-027"
        corrected_plan_id = "PLAN-028"
        run_id = "RUN-27"
    else:
        original_decision_id = f"{prefix}-DEC-001"
        new_decision_id = f"{prefix}-DEC-002"
        specification_id = f"{prefix}-SPEC-001"
        ticket_id = f"{prefix}-TICKET-001"
        initial_plan_id = f"{prefix}-PLAN-001"
        corrected_plan_id = f"{prefix}-PLAN-002"
        run_id = f"{prefix}-RUN-001"

    task_blueprints = (*preserved_tasks, *invalidated_tasks)
    task_ids = [
        f"TASK-{101 + index:03d}" if canonical_ids else f"{prefix}-TASK-{index + 1:03d}"
        for index in range(len(task_blueprints))
    ]
    task_pairs = list(zip(task_ids, task_blueprints, strict=True))
    preserved_ids = frozenset(task_ids[: len(preserved_tasks)])
    invalidated_ids = frozenset(task_ids[len(preserved_tasks) :])
    all_scopes = set(requirements)
    changed_scopes = set(changed_requirements)
    baseline_roles_for_change = {
        SCENARIO_BASELINE_AUTHORITY_BY_SCOPE[scope] for scope in changed_scopes
    }
    if len(baseline_roles_for_change) != 1:
        raise ValueError(
            "Every superseded scenario decision must have one authoritative baseline role."
        )
    baseline_role = next(iter(baseline_roles_for_change))

    original_decision = Artifact(
        id=original_decision_id,
        kind=ArtifactKind.DECISION,
        title=original_decision_title,
        text=original_decision_text,
        scopes=changed_scopes,
        approval_status=ApprovalStatus.APPROVED,
        authority_role=baseline_role,
        confidence=0.98,
        effective_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        source_ref=f"fixture://scenario/{scenario_id}/decision/original",
        attributes={
            "requirements": {
                scope: deepcopy(requirements[scope]) for scope in changed_scopes
            },
            "fixture_driven": True,
        },
    )
    companion_scopes_by_role: dict[str, set[str]] = {}
    for scope in sorted(all_scopes - changed_scopes):
        role = SCENARIO_BASELINE_AUTHORITY_BY_SCOPE[scope]
        companion_scopes_by_role.setdefault(role, set()).add(scope)
    baseline_companion_decisions: list[Artifact] = []
    for index, (role, role_scopes) in enumerate(
        sorted(companion_scopes_by_role.items()),
        start=1,
    ):
        baseline_companion_decisions.append(
            Artifact(
                id=f"{prefix}-BASELINE-{index:03d}",
                kind=ArtifactKind.DECISION,
                title=f"{name}: approved {role} baseline",
                text=(
                    "Approved graph-v17 baseline requirements for "
                    + ", ".join(sorted(role_scopes))
                    + "."
                ),
                scopes=role_scopes,
                approval_status=ApprovalStatus.APPROVED,
                authority_role=role,
                confidence=0.98,
                effective_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
                source_ref=(
                    f"fixture://scenario/{scenario_id}/decision/baseline/{index:03d}"
                ),
                attributes={
                    "requirements": {
                        scope: deepcopy(requirements[scope])
                        for scope in role_scopes
                    },
                    "fixture_driven": True,
                },
            )
        )
    baseline_decisions = [original_decision, *baseline_companion_decisions]
    specification = Artifact(
        id=specification_id,
        kind=ArtifactKind.SPECIFICATION,
        title=f"{name} specification",
        text=f"Specification compiled from the approved graph-v17 baseline for {name}.",
        scopes=all_scopes,
        source_ref=f"fixture://scenario/{scenario_id}/specification",
    )
    ticket = Artifact(
        id=ticket_id,
        kind=ArtifactKind.TICKET,
        title=name,
        text=description,
        scopes=all_scopes,
        source_ref=f"fixture://scenario/{scenario_id}/ticket",
    )
    tasks = [
        Artifact(
            id=task_id,
            kind=ArtifactKind.TASK,
            title=blueprint.title,
            text=blueprint.description,
            scopes={blueprint.scope},
            source_ref=f"fixture://scenario/{scenario_id}/task/{task_id}",
        )
        for task_id, blueprint in task_pairs
    ]
    plan_artifact = Artifact(
        id=initial_plan_id,
        kind=ArtifactKind.AGENT_PLAN,
        title=f"Initial plan for {name}",
        text=f"Implement the original approved behavior for {name}.",
        scopes={blueprint.scope for blueprint in task_blueprints},
        source_ref=f"agent://scenario/{scenario_id}/run/{run_id}/plan/{initial_plan_id}",
    )

    edges = [
        *[
            Edge(
                source_id=decision.id,
                target_id=specification_id,
                kind=EdgeKind.BASIS_FOR,
                scopes=set(decision.scopes),
                evidence_ref=decision.source_ref,
            )
            for decision in baseline_decisions
        ],
        Edge(
            source_id=specification_id,
            target_id=ticket_id,
            kind=EdgeKind.CREATES,
            scopes=all_scopes,
            evidence_ref=specification.source_ref,
        ),
        *[
            Edge(
                source_id=ticket_id,
                target_id=task_id,
                kind=EdgeKind.DECOMPOSES_TO,
                scopes={blueprint.scope},
                evidence_ref=task.source_ref,
            )
            for (task_id, blueprint), task in zip(task_pairs, tasks, strict=True)
        ],
        *[
            Edge(
                source_id=task_id,
                target_id=initial_plan_id,
                kind=EdgeKind.CURRENTLY_DRIVES,
                scopes={blueprint.scope},
                evidence_ref=plan_artifact.source_ref,
            )
            for task_id, blueprint in task_pairs
        ],
    ]

    initial_actions = [
        PlanAction(
            id=f"ACTION-{index + 1}" if canonical_ids else f"{prefix}-ACTION-{index + 1:03d}",
            description=blueprint.description,
            scopes={blueprint.scope},
            attributes={
                "task_id": task_id,
                **deepcopy(requirements[blueprint.scope]),
            },
        )
        for index, (task_id, blueprint) in enumerate(task_pairs)
    ]
    initial_plan = AgentPlan(
        id=initial_plan_id,
        ticket_id=ticket_id,
        objective=f"Implement {name} under the original approved decision",
        actions=initial_actions,
    )
    initial_run = AgentRun(run_id=run_id, ticket_id=ticket_id, plan=initial_plan)

    corrected_actions = [
        action.model_copy(deep=True)
        for action, (task_id, _) in zip(initial_actions, task_pairs, strict=True)
        if task_id in preserved_ids
    ]
    first_new_action_index = len(initial_actions) + 1
    fixture_action_ids: list[str] = []
    for offset, blueprint in enumerate(new_actions):
        action_index = first_new_action_index + offset
        action_id = (
            f"ACTION-{action_index}" if canonical_ids else f"{prefix}-ACTION-{action_index:03d}"
        )
        fixture_action_ids.append(action_id)
        corrected_actions.append(
            PlanAction(
                id=action_id,
                description=blueprint.description,
                scopes={blueprint.scope},
                attributes={
                    "fixture_driven": True,
                    **deepcopy(changed_requirements[blueprint.scope]),
                },
            )
        )
    corrected_plan = AgentPlan(
        id=corrected_plan_id,
        ticket_id=ticket_id,
        objective=f"Correct {name} for the newly approved requirements",
        actions=corrected_actions,
    )

    new_decision = Artifact(
        id=new_decision_id,
        kind=ArtifactKind.DECISION,
        title=new_decision_title,
        text=new_decision_text,
        scopes=changed_scopes,
        approval_status=ApprovalStatus.APPROVED,
        authority_role=authority_role,
        confidence=0.97,
        effective_at=datetime(2026, 2, 1, 14, 30, tzinfo=UTC),
        source_ref=f"fixture://scenario/{scenario_id}/decision/new",
        attributes={"requirements": deepcopy(changed_requirements)},
    )
    mutation = DecisionMutation(
        decision=new_decision,
        supersedes_id=original_decision_id,
        affected_scopes=changed_scopes,
    )

    return ScenarioDefinition(
        metadata=ScenarioMetadata(
            id=scenario_id,
            name=name,
            category=category,
            description=description,
            risk_level=risk_level,
        ),
        narrative=ScenarioNarrative(
            why_changed=why_changed,
            expected_corrected_behavior=expected_corrected_behavior,
            risk_if_old_authorization_continues=risk_if_old_authorization_continues,
        ),
        graph_seed=ScenarioGraphSeed(
            version=17,
            artifacts=[
                *baseline_decisions,
                specification,
                ticket,
                *tasks,
                plan_artifact,
            ],
            edges=edges,
        ),
        initial_run=initial_run,
        mutation=mutation,
        corrected_plan=corrected_plan,
        authority_policy=deepcopy(SCENARIO_AUTHORITY_POLICY),
        presentation=ScenarioPresentation(
            selector_summary=description,
            preserved_work=[task.title for task in preserved_tasks],
            invalidated_work=[task.title for task in invalidated_tasks],
            newly_required_work=[action.title for action in new_actions],
            old_grant_rejection_copy=(
                "Execution rejected: the authorization predates the active decision "
                f"“{new_decision_title}”."
            ),
            demo_takeaway=demo_takeaway,
        ),
        expectations=ScenarioExpectation(
            preserved_task_ids=preserved_ids,
            invalidated_task_ids=invalidated_ids,
            needs_review_artifact_ids=frozenset({initial_plan_id}),
            newly_required_action_ids=frozenset(fixture_action_ids),
        ),
    )


SCENARIO_CATALOG: tuple[ScenarioDefinition, ...] = (
    _build_scenario(
        scenario_id="csv-exports-admin-only",
        prefix="CSV",
        canonical_ids=True,
        name="CSV exports become admin-only",
        category=ScenarioCategory.COMPLIANCE,
        risk_level=ScenarioRiskLevel.HIGH,
        description="A compliance decision narrows account-data exports from all users to admins.",
        original_decision_title="All users may export account data",
        original_decision_text="CSV export is approved for all authenticated users.",
        new_decision_title="Exports must be admin-only",
        new_decision_text="For compliance, CSV exports are restricted to administrators.",
        authority_role="compliance",
        why_changed="Compliance determined that account exports expose sensitive customer data.",
        expected_corrected_behavior=(
            "Keep CSV generation, formatting, and auditing while enforcing admin-only access."
        ),
        risk_if_old_authorization_continues=(
            "Standard users could export account data after the restriction became effective."
        ),
        requirements={
            "export.generation": {"format": "csv"},
            "export.audit": {"audit_logging": True},
            "export.authorization": {"audience": "all_users"},
        },
        changed_requirements={
            "export.authorization": {"audience": "admin_only"},
        },
        preserved_tasks=(
            _TaskBlueprint(
                "Generate CSV files",
                "Serialize account data into a valid CSV file.",
                "export.generation",
            ),
            _TaskBlueprint(
                "Format export columns",
                "Apply the approved CSV column ordering and formatting.",
                "export.generation",
            ),
            _TaskBlueprint(
                "Add export audit logging",
                "Record auditable export events without changing access policy.",
                "export.audit",
            ),
        ),
        invalidated_tasks=(
            _TaskBlueprint(
                "Display exports to standard users",
                "Show the export control to every authenticated user.",
                "export.authorization",
            ),
            _TaskBlueprint(
                "Allow standard-user export API access",
                "Permit standard users to invoke the export endpoint.",
                "export.authorization",
            ),
        ),
        new_actions=(
            _ActionBlueprint(
                "Add admin role check",
                "Show export controls only after an administrator role check.",
                "export.authorization",
            ),
            _ActionBlueprint(
                "Add server-side authorization validation",
                "Enforce administrator access at the export API boundary.",
                "export.authorization",
            ),
        ),
        demo_takeaway="Dragback preserves CSV work while stopping only obsolete access behavior.",
    ),
    _build_scenario(
        scenario_id="payment-provider-unapproved",
        prefix="PAYMENT",
        canonical_ids=False,
        name="Payment provider is no longer approved",
        category=ScenarioCategory.FINANCE,
        risk_level=ScenarioRiskLevel.CRITICAL,
        description=(
            "Compliance removes the selected billing provider from the approved vendor list."
        ),
        original_decision_title="Use the selected provider for subscription billing",
        original_decision_text=(
            "Subscription billing may use the selected external payment provider."
        ),
        new_decision_title="Selected payment provider is prohibited",
        new_decision_text="Subscription billing must use an adapter backed by an approved vendor.",
        authority_role="compliance",
        why_changed="The vendor failed a compliance review and was removed from the approved list.",
        expected_corrected_behavior=(
            "Retain provider-neutral billing logic and replace only provider-specific integration."
        ),
        risk_if_old_authorization_continues=(
            "New transactions and credentials could be sent to a prohibited vendor."
        ),
        requirements={
            "billing.domain": {"architecture": "provider_neutral"},
            "billing.invoice": {"calculation": "standard"},
            "billing.provider": {"provider_policy": "selected_vendor"},
        },
        changed_requirements={
            "billing.provider": {"provider_policy": "approved_adapter"},
        },
        preserved_tasks=(
            _TaskBlueprint(
                "Maintain billing domain models",
                "Keep provider-neutral subscription and payment models.",
                "billing.domain",
            ),
            _TaskBlueprint(
                "Calculate invoices",
                "Retain invoice totals, taxes, and proration calculations.",
                "billing.invoice",
            ),
            _TaskBlueprint(
                "Maintain subscription state logic",
                "Preserve provider-neutral subscription state transitions.",
                "billing.domain",
            ),
        ),
        invalidated_tasks=(
            _TaskBlueprint(
                "Integrate the removed provider SDK",
                "Call the selected vendor through its proprietary SDK.",
                "billing.provider",
            ),
            _TaskBlueprint(
                "Handle provider-specific webhooks",
                "Process webhook formats unique to the removed provider.",
                "billing.provider",
            ),
            _TaskBlueprint(
                "Deploy provider credentials",
                "Provision the removed provider's production credentials.",
                "billing.provider",
            ),
        ),
        new_actions=(
            _ActionBlueprint(
                "Build an approved-provider adapter",
                "Route billing through an adapter restricted to approved providers.",
                "billing.provider",
            ),
            _ActionBlueprint(
                "Create a provider migration plan",
                "Migrate provider-specific behavior behind the approved adapter.",
                "billing.provider",
            ),
        ),
        demo_takeaway="Vendor-specific work stops without discarding the billing domain.",
    ),
    _build_scenario(
        scenario_id="us-data-residency",
        prefix="RESIDENCY",
        canonical_ids=False,
        name="Customer data must remain in the United States",
        category=ScenarioCategory.DATA_GOVERNANCE,
        risk_level=ScenarioRiskLevel.CRITICAL,
        description="A residency policy prohibits customer data in non-US cloud regions.",
        original_decision_title="Use any supported cloud region",
        original_decision_text="Customer data may be stored in any supported cloud region.",
        new_decision_title="Customer data requires US residency",
        new_decision_text="Customer data and replicas must remain in United States regions.",
        authority_role="privacy",
        why_changed="A contractual data-residency obligation became effective.",
        expected_corrected_behavior=(
            "Preserve schema, validation, and portable backup cryptography while enforcing "
            "US-only placement for primary, replica, and backup data."
        ),
        risk_if_old_authorization_continues=(
            "Customer data could be placed or replicated outside the required jurisdiction."
        ),
        requirements={
            "data.schema": {"schema_mode": "portable"},
            "data.validation": {"validation": "enabled"},
            "data.backup.crypto": {"crypto_controls": "approved"},
            "data.residency": {"region_policy": "any_supported"},
        },
        changed_requirements={
            "data.residency": {"region_policy": "us_only"},
        },
        preserved_tasks=(
            _TaskBlueprint(
                "Define the database schema",
                "Keep the portable customer-data schema.",
                "data.schema",
            ),
            _TaskBlueprint(
                "Validate customer data",
                "Retain application-level customer-data validation.",
                "data.validation",
            ),
            _TaskBlueprint(
                "Define backup encryption and key controls",
                (
                    "Retain portable encryption and key-management controls; storage "
                    "location and replication topology are excluded."
                ),
                "data.backup.crypto",
            ),
        ),
        invalidated_tasks=(
            _TaskBlueprint(
                "Deploy to non-US regions",
                "Configure customer-data workloads in any supported region.",
                "data.residency",
            ),
            _TaskBlueprint(
                "Replicate data outside the US",
                "Enable global cross-region customer-data replication.",
                "data.residency",
            ),
        ),
        new_actions=(
            _ActionBlueprint(
                "Enforce US region placement",
                (
                    "Restrict primary, replica, and backup customer-data storage to "
                    "approved US regions."
                ),
                "data.residency",
            ),
            _ActionBlueprint(
                "Validate residency configuration",
                "Reject deployments that place customer data outside US regions.",
                "data.residency",
            ),
        ),
        demo_takeaway="Region-specific deployment stops while portable data work survives.",
    ),
    _build_scenario(
        scenario_id="public-launch-canceled",
        prefix="LAUNCH",
        canonical_ids=False,
        name="Public launch is canceled",
        category=ScenarioCategory.PRODUCT,
        risk_level=ScenarioRiskLevel.HIGH,
        description="The public rollout is canceled while internal testing remains approved.",
        original_decision_title="Launch publicly on Friday",
        original_decision_text="The feature should be released publicly on Friday.",
        new_decision_title="Continue internal testing only",
        new_decision_text="The public launch is canceled; internal testing may continue.",
        authority_role="product",
        why_changed="Product leadership paused the launch after readiness review.",
        expected_corrected_behavior=(
            "Keep implementation and internal validation behind an internal-only restriction."
        ),
        risk_if_old_authorization_continues=(
            "Customers could receive an unapproved feature and launch communication."
        ),
        requirements={
            "agent.build": {"build_mode": "feature_complete"},
            "agent.testing": {"test_audience": "internal"},
            "telemetry.feature": {"telemetry": "enabled"},
            "release.visibility": {"visibility": "public"},
        },
        changed_requirements={
            "release.visibility": {"visibility": "internal_only"},
        },
        preserved_tasks=(
            _TaskBlueprint(
                "Implement the core feature",
                "Complete core feature behavior independent of rollout.",
                "agent.build",
            ),
            _TaskBlueprint(
                "Run internal QA",
                "Continue internal quality assurance.",
                "agent.testing",
            ),
            _TaskBlueprint(
                "Collect feature telemetry",
                "Retain internal feature telemetry.",
                "telemetry.feature",
            ),
        ),
        invalidated_tasks=(
            _TaskBlueprint(
                "Enable the public feature flag",
                "Enable the feature for all production customers.",
                "release.visibility",
            ),
            _TaskBlueprint(
                "Publish the customer announcement",
                "Send the public launch announcement.",
                "release.visibility",
            ),
            _TaskBlueprint(
                "Roll out to production customers",
                "Complete the public production rollout.",
                "release.visibility",
            ),
        ),
        new_actions=(
            _ActionBlueprint(
                "Restrict access to internal users",
                "Configure the feature for internal testing only.",
                "release.visibility",
            ),
        ),
        demo_takeaway="The launch stops, but implementation and internal QA remain useful.",
    ),
    _build_scenario(
        scenario_id="api-read-only",
        prefix="READONLY",
        canonical_ids=False,
        name="API access becomes read-only",
        category=ScenarioCategory.ACCESS_CONTROL,
        risk_level=ScenarioRiskLevel.HIGH,
        description="An integration loses permission to modify customer records.",
        original_decision_title="Integration may read and modify records",
        original_decision_text=(
            "The integration may read, create, update, and delete customer records."
        ),
        new_decision_title="Integration access is read-only",
        new_decision_text="The integration may read customer records but may not modify them.",
        authority_role="security",
        why_changed="A least-privilege review removed write access from the integration.",
        expected_corrected_behavior=(
            "Retain authentication, reads, and mapping while enforcing a read-only scope."
        ),
        risk_if_old_authorization_continues=(
            "The integration could mutate or delete customer records without current approval."
        ),
        requirements={
            "integration.authentication": {"authentication": "required"},
            "integration.read": {"read_access": True},
            "integration.mapping": {"mapping": "canonical"},
            "records.write": {"write_access": "allowed"},
        },
        changed_requirements={
            "records.write": {"write_access": "forbidden"},
        },
        preserved_tasks=(
            _TaskBlueprint(
                "Authenticate the integration",
                "Retain secure integration authentication.",
                "integration.authentication",
            ),
            _TaskBlueprint(
                "Read customer records",
                "Keep approved customer-record reads.",
                "integration.read",
            ),
            _TaskBlueprint(
                "Map customer data",
                "Retain canonical data mapping.",
                "integration.mapping",
            ),
        ),
        invalidated_tasks=(
            _TaskBlueprint(
                "Create customer records",
                "Create customer records through the integration.",
                "records.write",
            ),
            _TaskBlueprint(
                "Update customer records",
                "Modify customer records through the integration.",
                "records.write",
            ),
            _TaskBlueprint(
                "Delete customer records",
                "Delete customer records through the integration.",
                "records.write",
            ),
        ),
        new_actions=(
            _ActionBlueprint(
                "Enforce read-only integration scope",
                "Reject every integration write operation.",
                "records.write",
            ),
        ),
        demo_takeaway="Read capabilities survive while all record mutations lose authorization.",
    ),
    _build_scenario(
        scenario_id="pii-safe-logging",
        prefix="LOGGING",
        canonical_ids=False,
        name="Logging may not contain personal data",
        category=ScenarioCategory.PRIVACY,
        risk_level=ScenarioRiskLevel.CRITICAL,
        description="A privacy decision prohibits personal or sensitive data in logs.",
        original_decision_title="Log complete request payloads",
        original_decision_text="Complete request payloads may be logged for debugging.",
        new_decision_title="Logs must exclude personal data",
        new_decision_text="Logs may not contain personal, secret, or sensitive request data.",
        authority_role="privacy",
        why_changed="Privacy review found that debug payloads could retain regulated information.",
        expected_corrected_behavior=(
            "Preserve observability while replacing raw payloads with redacted structured logs."
        ),
        risk_if_old_authorization_continues=(
            "Personal data, credentials, and tokens could be retained in log systems."
        ),
        requirements={
            "observability.infrastructure": {"logging": "enabled"},
            "observability.metadata": {"safe_metadata": True},
            "logging.payload": {"payload_policy": "complete"},
        },
        changed_requirements={
            "logging.payload": {"payload_policy": "redacted_only"},
        },
        preserved_tasks=(
            _TaskBlueprint(
                "Maintain logging infrastructure",
                "Keep the logging transport and storage pipeline.",
                "observability.infrastructure",
            ),
            _TaskBlueprint(
                "Log request IDs",
                "Retain non-sensitive request correlation IDs.",
                "observability.metadata",
            ),
            _TaskBlueprint(
                "Log error codes",
                "Retain structured error codes.",
                "observability.metadata",
            ),
            _TaskBlueprint(
                "Log performance metrics",
                "Retain non-personal timing and performance metrics.",
                "observability.metadata",
            ),
        ),
        invalidated_tasks=(
            _TaskBlueprint(
                "Log raw request bodies",
                "Write complete request bodies to debug logs.",
                "logging.payload",
            ),
            _TaskBlueprint(
                "Log email addresses",
                "Include customer email addresses in logs.",
                "logging.payload",
            ),
            _TaskBlueprint(
                "Log authentication tokens",
                "Include authentication tokens in debug output.",
                "logging.payload",
            ),
        ),
        new_actions=(
            _ActionBlueprint(
                "Redact sensitive values",
                "Redact personal and secret values before logging.",
                "logging.payload",
            ),
            _ActionBlueprint(
                "Emit structured safe logs",
                "Replace raw payloads with allowlisted structured fields.",
                "logging.payload",
            ),
        ),
        demo_takeaway="Observability remains; only unsafe payload capture is removed.",
    ),
    _build_scenario(
        scenario_id="human-approval-required",
        prefix="APPROVAL",
        canonical_ids=False,
        name="AI-generated changes require human approval",
        category=ScenarioCategory.SECURITY,
        risk_level=ScenarioRiskLevel.CRITICAL,
        description=(
            "Security requires recorded human approval before every AI-generated "
            "merge or deployment."
        ),
        original_decision_title="Low-risk AI changes may merge automatically",
        original_decision_text=(
            "The coding agent may merge and deploy low-risk changes automatically."
        ),
        new_decision_title="Human approval is required before merge or deployment",
        new_decision_text=(
            "Every AI-generated change requires recorded human approval before "
            "either merge or deployment."
        ),
        authority_role="security",
        why_changed="Security policy removed autonomous merge authority from coding agents.",
        expected_corrected_behavior=(
            "Continue coding, testing, and PR preparation but stop at a human approval gate."
        ),
        risk_if_old_authorization_continues=(
            "AI-generated changes could reach production without mandatory review."
        ),
        requirements={
            "agent.build": {"generation": "allowed"},
            "agent.testing": {"tests": "required"},
            "agent.release_approval": {"approval_gate": "automatic_low_risk"},
        },
        changed_requirements={
            "agent.release_approval": {"approval_gate": "human_required"},
        },
        preserved_tasks=(
            _TaskBlueprint(
                "Generate code changes",
                "Continue producing proposed code changes.",
                "agent.build",
            ),
            _TaskBlueprint(
                "Run tests",
                "Continue executing the required test suite.",
                "agent.testing",
            ),
            _TaskBlueprint(
                "Prepare a pull request",
                "Prepare changes for human review.",
                "agent.build",
            ),
        ),
        invalidated_tasks=(
            _TaskBlueprint(
                "Merge changes automatically",
                "Merge low-risk AI-generated changes without human review.",
                "agent.release_approval",
            ),
            _TaskBlueprint(
                "Deploy automatically",
                "Deploy automatically after the autonomous merge.",
                "agent.release_approval",
            ),
        ),
        new_actions=(
            _ActionBlueprint(
                "Wait for human approval",
                "Require a recorded human approval before merge or deployment.",
                "agent.release_approval",
            ),
        ),
        demo_takeaway="Agent productivity remains, but consequential execution gains a human gate.",
    ),
    _build_scenario(
        scenario_id="internal-model-only",
        prefix="MODEL",
        canonical_ids=False,
        name="Third-party model use is prohibited",
        category=ScenarioCategory.PRIVACY,
        risk_level=ScenarioRiskLevel.CRITICAL,
        description="Customer data may only be processed by the internally hosted model.",
        original_decision_title="Use any configured model provider",
        original_decision_text=(
            "Prompts may be sent to any configured internal or external model provider."
        ),
        new_decision_title="Customer data requires the internal model",
        new_decision_text="Only the internally hosted model may process customer data.",
        authority_role="privacy",
        why_changed="A data-processing policy prohibited third-party model providers.",
        expected_corrected_behavior=(
            "Keep provider-neutral prompt and evaluation work while enforcing internal routing."
        ),
        risk_if_old_authorization_continues=(
            "Customer data could be disclosed to an unapproved external model provider."
        ),
        requirements={
            "ai.prompting": {"templates": "provider_neutral"},
            "ai.response_parsing": {"parsing": "structured"},
            "ai.evaluation": {"evaluation": "enabled"},
            "model.routing": {"provider_policy": "any_configured"},
        },
        changed_requirements={
            "model.routing": {"provider_policy": "internal_only"},
        },
        preserved_tasks=(
            _TaskBlueprint(
                "Maintain prompt templates",
                "Keep provider-neutral prompt templates.",
                "ai.prompting",
            ),
            _TaskBlueprint(
                "Parse model responses",
                "Retain structured response parsing.",
                "ai.response_parsing",
            ),
            _TaskBlueprint(
                "Evaluate model output",
                "Retain model-output evaluation logic.",
                "ai.evaluation",
            ),
        ),
        invalidated_tasks=(
            _TaskBlueprint(
                "Call external model APIs",
                "Route customer prompts to external model APIs.",
                "model.routing",
            ),
            _TaskBlueprint(
                "Deploy external provider credentials",
                "Provision credentials for third-party model services.",
                "model.routing",
            ),
            _TaskBlueprint(
                "Fallback to third-party models",
                "Use an external provider when the primary model is unavailable.",
                "model.routing",
            ),
        ),
        new_actions=(
            _ActionBlueprint(
                "Enforce internal-model routing",
                "Route every customer-data request to the internally hosted model.",
                "model.routing",
            ),
        ),
        demo_takeaway="Reusable AI logic survives while external data transfer is stopped.",
    ),
    _build_scenario(
        scenario_id="pdf-uploads-only",
        prefix="UPLOAD",
        canonical_ids=False,
        name="File uploads are limited to PDFs",
        category=ScenarioCategory.SECURITY,
        risk_level=ScenarioRiskLevel.HIGH,
        description="The accepted upload surface narrows from several formats to PDF only.",
        original_decision_title="Accept PDF, image, and text uploads",
        original_decision_text="Users may upload PDFs, images, and text documents.",
        new_decision_title="Only PDF uploads are permitted",
        new_decision_text="The upload service must reject every non-PDF file.",
        authority_role="security",
        why_changed="Security reduced the parser attack surface to one reviewed document format.",
        expected_corrected_behavior=(
            "Keep upload and storage infrastructure while enforcing PDF-only validation."
        ),
        risk_if_old_authorization_continues=(
            "Unreviewed parsers could continue processing prohibited file types."
        ),
        requirements={
            "upload.interface": {"upload_ui": "enabled"},
            "upload.storage": {"storage": "managed"},
            "upload.size_validation": {"size_validation": "enabled"},
            "upload.file_types": {"allowed_types": "pdf_image_text"},
        },
        changed_requirements={
            "upload.file_types": {"allowed_types": "pdf_only"},
        },
        preserved_tasks=(
            _TaskBlueprint(
                "Maintain the upload interface",
                "Keep the generic file upload interface.",
                "upload.interface",
            ),
            _TaskBlueprint(
                "Store uploaded files",
                "Retain the managed storage layer.",
                "upload.storage",
            ),
            _TaskBlueprint(
                "Validate file size",
                "Retain file-size validation.",
                "upload.size_validation",
            ),
        ),
        invalidated_tasks=(
            _TaskBlueprint(
                "Parse uploaded images",
                "Process image file uploads.",
                "upload.file_types",
            ),
            _TaskBlueprint(
                "Parse text documents",
                "Process text-document uploads.",
                "upload.file_types",
            ),
            _TaskBlueprint(
                "Accept non-PDF MIME types",
                "Allow image and text MIME types through validation.",
                "upload.file_types",
            ),
        ),
        new_actions=(
            _ActionBlueprint(
                "Enforce PDF-only validation",
                "Reject extensions, MIME types, and signatures that are not PDF.",
                "upload.file_types",
            ),
        ),
        demo_takeaway="The upload platform remains useful while risky parsers are removed.",
    ),
    _build_scenario(
        scenario_id="reversible-database-migration",
        prefix="MIGRATION",
        canonical_ids=False,
        name="Database migration must be reversible",
        category=ScenarioCategory.INFRASTRUCTURE,
        risk_level=ScenarioRiskLevel.CRITICAL,
        description="A production migration must become backward-compatible and reversible.",
        original_decision_title="Apply a destructive schema migration",
        original_decision_text=(
            "The production schema may be changed with a destructive one-way migration."
        ),
        new_decision_title="Production migrations require rollback",
        new_decision_text=(
            "Every production migration must be backward-compatible and support rollback."
        ),
        authority_role="platform",
        why_changed="Reliability review prohibited irreversible production schema changes.",
        expected_corrected_behavior=(
            "Preserve target-schema intent and the source-to-target mapping while replanning "
            "production execution around expand-contract and rollback."
        ),
        risk_if_old_authorization_continues=(
            "A failed release could destroy data or leave production without a recovery path."
        ),
        requirements={
            "migration.schema": {"schema_design": "approved"},
            "migration.mapping": {"mapping_status": "validated"},
            "migration.reversibility": {"migration_mode": "destructive_allowed"},
        },
        changed_requirements={
            "migration.reversibility": {"migration_mode": "rollback_required"},
        },
        preserved_tasks=(
            _TaskBlueprint(
                "Design the new schema",
                "Retain the approved target schema design.",
                "migration.schema",
            ),
            _TaskBlueprint(
                "Specify the source-to-target data mapping",
                (
                    "Retain the validated mapping specification without preserving "
                    "one-way production execution."
                ),
                "migration.mapping",
            ),
        ),
        invalidated_tasks=(
            _TaskBlueprint(
                "Run a destructive one-way migration",
                "Apply schema changes that cannot be rolled back.",
                "migration.reversibility",
            ),
            _TaskBlueprint(
                "Delete columns immediately",
                "Drop legacy columns in the first production release.",
                "migration.reversibility",
            ),
        ),
        new_actions=(
            _ActionBlueprint(
                "Use a backward-compatible migration",
                (
                    "Apply an expand-contract schema migration and backward-compatible "
                    "data backfill for old and new code."
                ),
                "migration.reversibility",
            ),
            _ActionBlueprint(
                "Add a rollback procedure",
                "Define and verify the production rollback procedure.",
                "migration.reversibility",
            ),
        ),
        demo_takeaway="The target schema survives while irreversible rollout mechanics stop.",
    ),
    _build_scenario(
        scenario_id="agent-staging-only",
        prefix="STAGING",
        canonical_ids=False,
        name="Production access is removed from the agent",
        category=ScenarioCategory.ACCESS_CONTROL,
        risk_level=ScenarioRiskLevel.CRITICAL,
        description="The coding agent may build and test but can operate only in staging.",
        original_decision_title="Agent may access production",
        original_decision_text=(
            "The coding agent may deploy and inspect staging and production systems."
        ),
        new_decision_title="Agent access is limited to staging",
        new_decision_text="The coding agent may operate in staging but has no production access.",
        authority_role="security",
        why_changed=(
            "A privileged-access review removed production credentials from autonomous agents."
        ),
        expected_corrected_behavior=(
            "Keep builds, tests, and staging deployment while blocking production operations."
        ),
        risk_if_old_authorization_continues=(
            "The agent could deploy, read sensitive logs, or access production secrets."
        ),
        requirements={
            "agent.build": {"build": "allowed"},
            "agent.testing": {"tests": "required"},
            "agent.environment_access.staging": {"access": "allowed"},
            "agent.environment_access.production": {"access": "allowed"},
        },
        changed_requirements={
            "agent.environment_access.production": {"access": "denied"},
        },
        preserved_tasks=(
            _TaskBlueprint(
                "Generate application builds",
                "Continue generating deployable builds.",
                "agent.build",
            ),
            _TaskBlueprint(
                "Run the test suite",
                "Continue executing automated tests.",
                "agent.testing",
            ),
            _TaskBlueprint(
                "Deploy to staging",
                "Continue deployments within the staging environment.",
                "agent.environment_access.staging",
            ),
        ),
        invalidated_tasks=(
            _TaskBlueprint(
                "Deploy to production",
                "Deploy agent-generated builds to production.",
                "agent.environment_access.production",
            ),
            _TaskBlueprint(
                "Read production logs",
                "Inspect logs from production services.",
                "agent.environment_access.production",
            ),
            _TaskBlueprint(
                "Access production secrets",
                "Read credentials and secrets from production.",
                "agent.environment_access.production",
            ),
        ),
        new_actions=(
            _ActionBlueprint(
                "Enforce staging-only environment scope",
                "Reject every agent operation targeting production.",
                "agent.environment_access.production",
            ),
        ),
        demo_takeaway="Development continues in staging while production privileges are revoked.",
    ),
    _build_scenario(
        scenario_id="delete-derived-user-data",
        prefix="DELETION",
        canonical_ids=False,
        name="User deletion must remove derived data",
        category=ScenarioCategory.DATA_GOVERNANCE,
        risk_level=ScenarioRiskLevel.CRITICAL,
        description="Account deletion expands to profiles, embeddings, caches, and exports.",
        original_decision_title="Delete the primary user record",
        original_decision_text="Deleting a user removes the primary account record.",
        new_decision_title="Deletion must include derived user data",
        new_decision_text=(
            "Deleting a user must remove derived profiles, embeddings, caches, and exports."
        ),
        authority_role="privacy",
        why_changed="Privacy clarified that deletion obligations apply to every derived data copy.",
        expected_corrected_behavior=(
            "Retain the deletion entry point and add complete derived-data cleanup "
            "and verification."
        ),
        risk_if_old_authorization_continues=(
            "Derived personal data could remain after the user receives a deletion confirmation."
        ),
        requirements={
            "user.primary_deletion": {"primary_delete": True},
            "user.lookup": {"lookup": "enabled"},
            "user.confirmation": {"confirmation": "required"},
            "privacy.deletion_scope": {"deletion_scope": "primary_only"},
        },
        changed_requirements={
            "privacy.deletion_scope": {"deletion_scope": "all_derived_data"},
        },
        preserved_tasks=(
            _TaskBlueprint(
                "Delete the primary user record",
                "Retain deletion of the primary account record.",
                "user.primary_deletion",
            ),
            _TaskBlueprint(
                "Look up the user account",
                "Retain account lookup before deletion.",
                "user.lookup",
            ),
            _TaskBlueprint(
                "Confirm deletion intent",
                "Retain the user-facing deletion confirmation flow.",
                "user.confirmation",
            ),
        ),
        invalidated_tasks=(
            _TaskBlueprint(
                "Complete deletion after primary-record removal",
                "Declare deletion complete without removing derived data.",
                "privacy.deletion_scope",
            ),
        ),
        new_actions=(
            _ActionBlueprint(
                "Delete derived profiles",
                "Remove every derived profile associated with the user.",
                "privacy.deletion_scope",
            ),
            _ActionBlueprint(
                "Purge user caches and exports",
                "Remove cached user data and generated exports.",
                "privacy.deletion_scope",
            ),
            _ActionBlueprint(
                "Delete user embeddings",
                "Remove embeddings derived from the user's data.",
                "privacy.deletion_scope",
            ),
            _ActionBlueprint(
                "Verify complete deletion",
                "Verify that primary and derived user data no longer exists.",
                "privacy.deletion_scope",
            ),
        ),
        demo_takeaway="The trusted deletion entry point remains while incomplete completion stops.",
    ),
)


SCENARIOS_BY_ID: dict[str, ScenarioDefinition] = {
    scenario.metadata.id: scenario for scenario in SCENARIO_CATALOG
}


def get_scenario(scenario_id: str) -> ScenarioDefinition:
    try:
        scenario = SCENARIOS_BY_ID[scenario_id]
    except KeyError as exc:
        raise KeyError(f"Unknown scenario: {scenario_id}") from exc
    return scenario.model_copy(deep=True)


def list_scenarios() -> tuple[ScenarioDefinition, ...]:
    return tuple(scenario.model_copy(deep=True) for scenario in SCENARIO_CATALOG)
