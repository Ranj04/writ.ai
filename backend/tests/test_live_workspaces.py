from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import cast
from urllib.parse import urlparse

import httpx
import pytest
from writai.auth.hexclave import HexclavePermissionError
from writai.domain import (
    AgentPlan,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    EdgeKind,
    GrantVerificationResult,
    PlanAction,
    VerificationCode,
)
from writai.fixtures import load_decision_v18
from writai.intake.approval import (
    ApprovalChannel,
    ApprovalEvidence,
    pending_from_workspace,
)
from writai.integrations.callwright import FixtureCallwrightClient
from writai.services import agent_api, authority_api, executor_api, support
from writai.workspaces.authority_contexts import (
    DynamicAuthorityContextCreateRequest,
    DynamicAuthorityContextRegistry,
    DynamicMutationApprovalRequest,
)
from writai.workspaces.models import (
    LiveWorkspaceImportRequest,
    LiveWorkspaceRecord,
    LiveWorkspaceStatus,
    LiveWorkspaceView,
    SlackUserIdentityBinding,
    WorkspaceExecutionResult,
    WorkspaceSlackBinding,
)
from writai.workspaces.orchestrator import LiveWorkspaceOrchestrator
from writai.workspaces.repository import (
    JsonFileLiveWorkspaceRepository,
    LiveWorkspaceConflict,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError


def workspace_import() -> LiveWorkspaceImportRequest:
    baseline_time = datetime(2026, 1, 1, tzinfo=UTC)
    return LiveWorkspaceImportRequest(
        id="refund-control",
        name="Refund controls",
        description="Stop automatic high-value refunds when finance policy changes.",
        authority_policy={
            "refund.calculation": {"finance-admin"},
            "refund.execution": {"finance-admin"},
        },
        baseline_decision=Artifact(
            id="DEC-REFUND-1",
            kind=ArtifactKind.DECISION,
            title="Automatic refunds",
            text="Refund calculation and execution may be automatic.",
            scopes={"refund.calculation", "refund.execution"},
            approval_status=ApprovalStatus.PROPOSAL,
            authority_role="finance-admin",
            effective_at=baseline_time,
            source_ref="manual://finance/decision-1",
            attributes={
                "requirements": {
                    "refund.calculation": {"formula": "policy-v1"},
                    "refund.execution": {"human_approval": False},
                }
            },
        ),
        specification=Artifact(
            id="SPEC-REFUND",
            kind=ArtifactKind.SPECIFICATION,
            title="Refund specification",
            scopes={"refund.calculation", "refund.execution"},
            source_ref="manual://finance/spec",
        ),
        ticket=Artifact(
            id="PAY-104",
            kind=ArtifactKind.TICKET,
            title="Automate customer refunds",
            scopes={"refund.calculation", "refund.execution"},
            source_ref="manual://tickets/PAY-104",
        ),
        tasks=[
            Artifact(
                id="TASK-CALCULATE",
                kind=ArtifactKind.TASK,
                title="Calculate refund",
                scopes={"refund.calculation"},
                source_ref="manual://tickets/PAY-104#calculate",
            ),
            Artifact(
                id="TASK-ISSUE",
                kind=ArtifactKind.TASK,
                title="Issue refund",
                scopes={"refund.execution"},
                source_ref="manual://tickets/PAY-104#issue",
            ),
        ],
        plan=AgentPlan(
            id="PLAN-REFUND-1",
            ticket_id="PAY-104",
            objective="Automate refunds",
            actions=[
                PlanAction(
                    id="ACTION-CALCULATE",
                    description="Calculate the refund",
                    scopes={"refund.calculation"},
                    attributes={"formula": "policy-v1"},
                ),
                PlanAction(
                    id="ACTION-ISSUE",
                    description="Issue the refund automatically",
                    scopes={"refund.execution"},
                    attributes={"human_approval": False},
                ),
            ],
        ),
    )


def workspace_import_with_slack_binding() -> LiveWorkspaceImportRequest:
    definition = workspace_import()
    definition.slack_binding = WorkspaceSlackBinding(
        workspace_id=definition.id,
        slack_team_id="T-REFUNDS",
        composio_connection_user_id="refunds-slack-connection",
        hexclave_team_id="hex-team-refunds",
        user_identities=(
            SlackUserIdentityBinding(
                slack_user_id="U-FINANCE",
                hexclave_user_id="finance-admin",
                evidence_ref="hexclave-session://binding/U-FINANCE",
            ),
        ),
    )
    return definition


def decision_proposal_body() -> dict[str, object]:
    return {
        "decision": Artifact(
            id="DEC-REFUND-2",
            kind=ArtifactKind.DECISION,
            title="High-value refunds need approval",
            text="Refund execution requires human approval.",
            scopes={"refund.execution"},
            approval_status=ApprovalStatus.PROPOSAL,
            authority_role="finance-admin",
            effective_at=datetime(2026, 1, 2, tzinfo=UTC),
            source_ref="manual://finance/decision-2",
            attributes={
                "requirements": {
                    "refund.execution": {"human_approval": True},
                }
            },
        ).model_dump(mode="json"),
        "supersedes_id": "DEC-REFUND-1",
        "affected_scopes": ["refund.execution"],
    }


def corrected_plan_body() -> dict[str, object]:
    plan = workspace_import().plan.model_copy(deep=True)
    plan.id = "PLAN-REFUND-2"
    issue = next(action for action in plan.actions if action.id == "ACTION-ISSUE")
    issue.description = "Wait for finance approval, then issue the refund"
    issue.attributes["human_approval"] = True
    return {"plan": plan.model_dump(mode="json")}


def callwright_workspace_import() -> LiveWorkspaceImportRequest:
    baseline_time = datetime(2026, 7, 24, tzinfo=UTC)
    return LiveWorkspaceImportRequest(
        id="voyagr-reservation",
        name="VOYAGR reservation call",
        description="Stop a stale reservation call and submit only the corrected call.",
        authority_policy={
            "event.copy": {"event-ops-lead"},
            "reservation.time": {"event-ops-lead"},
        },
        baseline_decision=Artifact(
            id="DEC-VOYAGR-001",
            kind=ArtifactKind.DECISION,
            title="Launch dinner plan",
            text="Prepare a concise summary and request the venue for 7:00 PM.",
            scopes={"event.copy", "reservation.time"},
            approval_status=ApprovalStatus.PROPOSAL,
            authority_role="event-ops-lead",
            effective_at=baseline_time,
            source_ref="manual://event-ops/launch-dinner-plan",
            attributes={
                "requirements": {
                    "event.copy": {"tone": "concise"},
                    "reservation.time": {
                        "requested_time": "2026-07-26T19:00:00-07:00"
                    },
                }
            },
        ),
        specification=Artifact(
            id="SPEC-VOYAGR-001",
            kind=ArtifactKind.SPECIFICATION,
            title="Launch dinner coordination",
            scopes={"event.copy", "reservation.time"},
            source_ref="manual://event-ops/launch-dinner-spec",
        ),
        ticket=Artifact(
            id="EVENT-208",
            kind=ArtifactKind.TICKET,
            title="Coordinate the launch dinner",
            scopes={"event.copy", "reservation.time"},
            source_ref="manual://tickets/EVENT-208",
            attributes={"assigned_agent": "Reservation Calling Agent"},
        ),
        tasks=[
            Artifact(
                id="TASK-101",
                kind=ArtifactKind.TASK,
                title="Prepare guest summary",
                text="Prepare a concise guest summary for the venue.",
                scopes={"event.copy"},
                source_ref="manual://tickets/EVENT-208#summary",
                attributes={
                    "agent_name": "Guest Summary Subagent",
                    "runtime_provider": "codex",
                },
            ),
            Artifact(
                id="TASK-102",
                kind=ArtifactKind.TASK,
                title="Call venue for the approved time",
                text="Use Callwright to request the approved reservation time.",
                scopes={"reservation.time"},
                source_ref="manual://tickets/EVENT-208#call",
                attributes={
                    "agent_name": "Reservation Call Subagent",
                    "runtime_provider": "claude-code",
                },
            ),
        ],
        plan=AgentPlan(
            id="PLAN-VOYAGR-017",
            ticket_id="EVENT-208",
            objective="Prepare the dinner details and request the reservation",
            actions=[
                PlanAction(
                    id="ACTION-SUMMARY-001",
                    description="Prepare a concise guest summary",
                    scopes={"event.copy"},
                    attributes={"task_id": "TASK-101", "tone": "concise"},
                ),
                PlanAction(
                    id="ACTION-CALL-001",
                    description=(
                        "Call the venue for 7:00 PM, "
                        "the approved reservation time"
                    ),
                    scopes={"reservation.time"},
                    attributes={
                        "provider": "voyagr-callwright",
                        "phone_number_ref": "demo-venue",
                        "objective": (
                            "Request a reservation for four guests without making "
                            "a paid commitment."
                        ),
                        "requested_time": "2026-07-26T19:00:00-07:00",
                        "party_size": 4,
                        "max_deposit_usd": 0,
                        "instructions": [
                            "Ask whether the approved time is available.",
                            "Politely end the call after receiving the answer.",
                        ],
                        "allowed_commitments": [
                            "Request the reservation only when no deposit is required."
                        ],
                        "language": "en",
                    },
                ),
            ],
        ),
    )


def callwright_change_body() -> dict[str, object]:
    return {
        "decision": Artifact(
            id="DEC-VOYAGR-002",
            kind=ArtifactKind.DECISION,
            title="Launch dinner reservations move to 8:30 PM",
            text="All venue reservations must now be requested for 8:30 PM.",
            scopes={"reservation.time"},
            approval_status=ApprovalStatus.PROPOSAL,
            authority_role="event-ops-lead",
            effective_at=datetime(2026, 7, 25, tzinfo=UTC),
            source_ref="manual://event-ops/schedule-change",
            attributes={
                "requirements": {
                    "reservation.time": {
                        "requested_time": "2026-07-26T20:30:00-07:00"
                    }
                }
            },
        ).model_dump(mode="json"),
        "supersedes_id": "DEC-VOYAGR-001",
        "affected_scopes": ["reservation.time"],
    }


def corrected_callwright_plan_body() -> dict[str, object]:
    plan = callwright_workspace_import().plan.model_copy(deep=True)
    plan.id = "PLAN-VOYAGR-018"
    call_action = next(
        action for action in plan.actions if action.id == "ACTION-CALL-001"
    )
    call_action.description = (
        "Call the venue for 8:30 PM, the newly approved time"
    )
    call_action.attributes["requested_time"] = "2026-07-26T20:30:00-07:00"
    return {"plan": plan.model_dump(mode="json")}


def test_import_builds_agent_plan_provenance_edges_and_persists_atomically(
    tmp_path: Path,
) -> None:
    definition = workspace_import()
    edges = definition.graph_edges()
    assert {
        edge.source_id
        for edge in edges
        if edge.kind is EdgeKind.CURRENTLY_DRIVES
        and edge.target_id == definition.plan.id
    } == {"TASK-CALCULATE", "TASK-ISSUE"}

    repository = JsonFileLiveWorkspaceRepository(tmp_path / "nested" / "workspaces.json")
    record = LiveWorkspaceRecord(
        definition=definition,
        context_id="live-refund-control",
        graph_version="graph-v17",
        current_plan=definition.plan,
    )
    repository.create(record)

    loaded = JsonFileLiveWorkspaceRepository(repository.path).get("refund-control")
    assert loaded.definition == definition
    assert loaded.status is LiveWorkspaceStatus.IMPORTED
    assert not list(repository.path.parent.glob("*.tmp"))


def test_repository_rejects_duplicate_workspace_ids_without_overwriting(
    tmp_path: Path,
) -> None:
    definition = workspace_import()
    repository = JsonFileLiveWorkspaceRepository(tmp_path / "workspaces.json")
    original = LiveWorkspaceRecord(
        definition=definition,
        context_id="live-refund-control",
        graph_version="graph-v17",
        current_plan=definition.plan,
    )
    repository.create(original)
    replacement_definition = definition.model_copy(
        update={"name": "Unexpected replacement"},
        deep=True,
    )
    replacement = LiveWorkspaceRecord(
        definition=replacement_definition,
        context_id="live-refund-control",
        graph_version="graph-v17",
        current_plan=replacement_definition.plan,
    )

    with pytest.raises(
        LiveWorkspaceConflict,
        match="Live Workspace already exists: refund-control",
    ):
        repository.create(replacement)

    assert repository.get("refund-control").definition.name == "Refund controls"


def test_import_rejects_a_decorative_disconnected_graph() -> None:
    raw = workspace_import().model_dump(mode="json")
    raw["edges"] = [
        {
            "source_id": "SPEC-REFUND",
            "target_id": "PAY-104",
            "kind": "CREATES",
            "scopes": ["refund.execution"],
        }
    ]

    with pytest.raises(ValidationError, match="missing authority provenance"):
        LiveWorkspaceImportRequest.model_validate(raw)


def test_import_rejects_a_task_absent_from_the_agent_plan() -> None:
    raw = callwright_workspace_import().model_dump(mode="json")
    tasks = cast(list[dict[str, object]], raw["tasks"])
    tasks.append(
        Artifact(
            id="TASK-103",
            kind=ArtifactKind.TASK,
            title="Unplanned venue follow-up",
            text="Perform work that the approved plan never authorized.",
            scopes={"event.copy"},
        ).model_dump(mode="json")
    )

    with pytest.raises(
        ValidationError,
        match="every Task must be represented by an AgentPlan action",
    ):
        LiveWorkspaceImportRequest.model_validate(raw)


def test_import_rejects_one_unbound_action_matching_multiple_tasks() -> None:
    raw = callwright_workspace_import().model_dump(mode="json")
    tasks = cast(list[dict[str, object]], raw["tasks"])
    tasks.append(
        Artifact(
            id="TASK-103",
            kind=ArtifactKind.TASK,
            title="Second venue call",
            text="Make another reservation call in the same scope.",
            scopes={"reservation.time"},
        ).model_dump(mode="json")
    )

    with pytest.raises(
        ValidationError,
        match=(
            "unbound AgentPlan action 'ACTION-CALL-001' matches multiple "
            "Tasks.*set attributes.task_id"
        ),
    ):
        LiveWorkspaceImportRequest.model_validate(raw)


def test_import_rejects_an_unscoped_authority_path() -> None:
    definition = workspace_import()
    raw = definition.model_dump(mode="json")
    raw["edges"] = [
        edge.model_dump(mode="json") for edge in definition.graph_edges()
    ]
    first_edge = next(
        edge
        for edge in raw["edges"]
        if edge["source_id"] == "DEC-REFUND-1"
        and edge["target_id"] == "SPEC-REFUND"
    )
    first_edge["scopes"].remove("refund.execution")

    with pytest.raises(
        ValidationError,
        match="continuous scoped authority path.*refund.execution",
    ):
        LiveWorkspaceImportRequest.model_validate(raw)


@pytest.mark.parametrize(
    ("broken_layer", "message"),
    [
        ("requirement-object", "requirements must be objects"),
        ("specification", "missing from: Specification"),
        ("ticket", "missing from: Ticket"),
        ("task", "missing from: Task"),
        ("plan", "missing from: AgentPlan action"),
        ("authority-role", "authority_role is not authorized"),
    ],
)
def test_import_rejects_silently_ignored_requirement_scopes(
    broken_layer: str,
    message: str,
) -> None:
    raw = workspace_import().model_dump(mode="json")
    scope = "refund.execution"
    if broken_layer == "requirement-object":
        raw["baseline_decision"]["attributes"]["requirements"][scope] = [
            "not-an-object"
        ]
    elif broken_layer == "specification":
        raw["specification"]["scopes"].remove(scope)
    elif broken_layer == "ticket":
        # Before this guard, evaluate_plan intersected requirements with Ticket
        # scopes and silently ignored this approved requirement.
        raw["ticket"]["scopes"].remove(scope)
    elif broken_layer == "task":
        for task in raw["tasks"]:
            task["scopes"] = [
                item for item in task["scopes"] if item != scope
            ]
    elif broken_layer == "plan":
        for action in raw["plan"]["actions"]:
            action["scopes"] = [
                item for item in action["scopes"] if item != scope
            ]
    else:
        raw["authority_policy"][scope] = ["security-reviewer"]

    with pytest.raises(ValidationError, match=message):
        LiveWorkspaceImportRequest.model_validate(raw)


@pytest.mark.parametrize("broken_input", ["requirement-mismatch", "task-reference"])
def test_import_rejects_a_plan_that_cannot_receive_initial_authorization(
    broken_input: str,
) -> None:
    raw = workspace_import().model_dump(mode="json")
    issue_action = next(
        action
        for action in raw["plan"]["actions"]
        if action["id"] == "ACTION-ISSUE"
    )
    if broken_input == "requirement-mismatch":
        issue_action["attributes"]["human_approval"] = True
        message = "does not satisfy baseline requirement"
    else:
        issue_action["attributes"]["task_id"] = "TASK-DOES-NOT-EXIST"
        message = "references a missing or non-valid Task"

    with pytest.raises(ValidationError, match=message):
        LiveWorkspaceImportRequest.model_validate(raw)


def test_dynamic_authority_seed_rejects_extra_decisions() -> None:
    definition = workspace_import()
    extra = definition.baseline_decision.model_copy(deep=True)
    extra.id = "DEC-PREAPPROVED-INJECTION"
    extra.approval_status = ApprovalStatus.APPROVED

    with pytest.raises(ValidationError, match="only its baseline Decision"):
        DynamicAuthorityContextCreateRequest(
            context_id="seed-purity-check",
            version=17,
            artifacts=[*definition.graph_artifacts(), extra],
            edges=definition.graph_edges(),
            authority_policy=definition.authority_policy,
            baseline_decision_id=definition.baseline_decision.id,
        )


def test_workspace_slack_binding_rejects_cross_workspace_or_default_connection() -> None:
    definition = workspace_import().model_dump(mode="json")
    definition["slack_binding"] = {
        "workspace_id": "another-workspace",
        "slack_team_id": "T-REFUNDS",
        "composio_connection_user_id": "refunds-slack-connection",
        "hexclave_team_id": "hex-team-refunds",
    }
    with pytest.raises(
        ValidationError,
        match="must match the Workspace ID",
    ):
        LiveWorkspaceImportRequest.model_validate(definition)

    definition["slack_binding"]["workspace_id"] = "refund-control"
    definition["slack_binding"]["composio_connection_user_id"] = "default"
    with pytest.raises(
        ValidationError,
        match="explicit non-default user ID",
    ):
        LiveWorkspaceImportRequest.model_validate(definition)


def test_workspace_slack_binding_round_trips_and_reaches_authority_context(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, authority, _executor, store_path = live_services
    definition = workspace_import_with_slack_binding()
    imported = agent.post(
        "/live-workspaces/import",
        json=definition.model_dump(mode="json"),
    )
    assert imported.status_code == 201
    assert imported.json()["slack_binding"]["hexclave_team_id"] == (
        "hex-team-refunds"
    )
    stored = JsonFileLiveWorkspaceRepository(store_path).get("refund-control")
    assert stored.definition.slack_binding == definition.slack_binding

    assert _approve_workspace_baseline(agent).status_code == 200
    context = authority.get(
        "/live-workspaces/authority/contexts/live-refund-control",
        headers={
            support.INTERNAL_SERVICE_AUTH_HEADER: support.internal_service_token(
                authority_api.settings.grant_secret
            )
        },
    ).json()
    slack_binding = definition.slack_binding
    assert slack_binding is not None
    assert context["slack_binding"] == slack_binding.model_dump(mode="json")


@pytest.fixture
def live_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, TestClient, TestClient, Path]:
    authority = TestClient(authority_api.app)
    agent = TestClient(agent_api.app)
    executor = TestClient(executor_api.app)
    store_path = tmp_path / "live-workspaces.json"

    monkeypatch.setattr(
        authority_api,
        "workspace_contexts",
        DynamicAuthorityContextRegistry(
            grant_secret="live-workspace-test-secret",
            grant_ttl_seconds=3600,
            authority_threshold=0.75,
        ),
    )
    monkeypatch.setattr(
        agent_api,
        "workspace_orchestrator",
        LiveWorkspaceOrchestrator(
            repository=JsonFileLiveWorkspaceRepository(store_path)
        ),
    )

    class IdentityResolver:
        def resolve_user_id(self, *, approval_token: str) -> str:
            if not approval_token.startswith("test-user:"):
                raise AssertionError("unexpected approval credential")
            return approval_token.removeprefix("test-user:")

    class PermissionChecker:
        def has_permission(self, *, user_id: str, permission_id: str) -> bool:
            return user_id == permission_id

    monkeypatch.setattr(
        agent_api,
        "_approval_identity_resolver",
        lambda: IdentityResolver(),
    )
    monkeypatch.setattr(
        agent_api,
        "_workspace_permission_checker",
        lambda _workspace: PermissionChecker(),
    )

    def route_post(url: str, **kwargs: object) -> httpx.Response:
        parsed = urlparse(url)
        headers = cast(dict[str, str], kwargs.get("headers", {}))
        body = cast(dict[str, object] | None, kwargs.get("json"))
        if parsed.port == 8001:
            return authority.post(parsed.path, json=body, headers=headers)
        if parsed.port == 8003:
            return executor.post(parsed.path, json=body, headers=headers)
        raise httpx.ConnectError(
            f"Unexpected service URL: {url}",
            request=httpx.Request("POST", url),
        )

    def route_get(url: str, **kwargs: object) -> httpx.Response:
        parsed = urlparse(url)
        if parsed.port == 8001:
            return authority.get(
                parsed.path,
                headers=cast(dict[str, str], kwargs.get("headers", {})),
            )
        raise httpx.ConnectError(
            f"Unexpected service URL: {url}",
            request=httpx.Request("GET", url),
        )

    def route_delete(url: str, **kwargs: object) -> httpx.Response:
        parsed = urlparse(url)
        if parsed.port == 8001:
            return authority.delete(
                parsed.path,
                headers=cast(dict[str, str], kwargs.get("headers", {})),
            )
        raise httpx.ConnectError(
            f"Unexpected service URL: {url}",
            request=httpx.Request("DELETE", url),
        )

    monkeypatch.setattr(support.httpx, "post", route_post)
    monkeypatch.setattr(support.httpx, "get", route_get)
    monkeypatch.setattr(support.httpx, "delete", route_delete)
    return agent, authority, executor, store_path


def _assert_no_signed_token(body: dict[str, object]) -> None:
    serialized = json.dumps(body)
    assert '"token"' not in serialized


def test_live_workspace_api_rejects_duplicate_custom_ids_without_overwriting(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, _authority, _executor, _store_path = live_services
    original = workspace_import().model_dump(mode="json")

    created = agent.post("/live-workspaces/import", json=original)
    assert created.status_code == 201

    replacement = {**original, "name": "Unexpected replacement"}
    duplicate = agent.post("/live-workspaces/import", json=replacement)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "LIVE_WORKSPACE_CONFLICT"

    stored = agent.get("/live-workspaces/refund-control")
    assert stored.status_code == 200
    assert stored.json()["name"] == "Refund controls"
    listed = agent.get("/live-workspaces").json()
    assert [workspace["id"] for workspace in listed["workspaces"]] == [
        "refund-control"
    ]


def _authorize_workspace(agent: TestClient) -> None:
    assert (
        agent.post(
            "/live-workspaces/import",
            json=workspace_import().model_dump(mode="json"),
        ).status_code
        == 201
    )
    assert _approve_workspace_baseline(agent).status_code == 200
    assert (
        agent.post(
            "/live-workspaces/refund-control/authorize",
            json={},
        ).json()["status"]
        == "authorized"
    )


def _approve_workspace_change(
    agent: TestClient,
    *,
    workspace_id: str = "refund-control",
    decision_id: str = "DEC-REFUND-2",
    user_id: str = "finance-admin",
) -> httpx.Response:
    state = agent.get(f"/live-workspaces/{workspace_id}").json()
    fingerprint = state["pending_proposal_fingerprint"]
    instance_id = state["pending_proposal_instance_id"]
    assert isinstance(fingerprint, str)
    assert isinstance(instance_id, str)
    return agent.post(
        f"/live-workspaces/{workspace_id}/decisions/{decision_id}/approve",
        json={
            "approval_token": f"test-user:{user_id}",
            "channel": "workspace-ui",
            "evidence_ref": (
                f"workspace-ui://{workspace_id}/{decision_id}/{fingerprint}"
            ),
            "confirmed_proposal_fingerprint": fingerprint,
            "confirmed_proposal_instance_id": instance_id,
        },
    )


def _approve_workspace_baseline(
    agent: TestClient,
    *,
    workspace_id: str = "refund-control",
    user_id: str = "finance-admin",
    fingerprint: str | None = None,
    instance_id: str | None = None,
    evidence_ref: str | None = None,
) -> httpx.Response:
    state = agent.get(f"/live-workspaces/{workspace_id}").json()
    selected_fingerprint = (
        fingerprint
        if fingerprint is not None
        else state["baseline_proposal_fingerprint"]
    )
    selected_instance_id = (
        instance_id
        if instance_id is not None
        else state["baseline_proposal_instance_id"]
    )
    assert isinstance(selected_fingerprint, str)
    assert isinstance(selected_instance_id, str)
    return agent.post(
        f"/live-workspaces/{workspace_id}/baseline/approve",
        json={
            "approval_token": f"test-user:{user_id}",
            "channel": "workspace-ui",
            "evidence_ref": (
                evidence_ref
                or (
                    f"workspace-ui://{workspace_id}/baseline/"
                    f"{selected_fingerprint}"
                )
            ),
            "confirmed_proposal_fingerprint": selected_fingerprint,
            "confirmed_proposal_instance_id": selected_instance_id,
        },
    )


def test_actor_role_only_cannot_approve_workspace_baseline(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, _authority, _executor, _store_path = live_services
    imported = agent.post(
        "/live-workspaces/import",
        json=workspace_import().model_dump(mode="json"),
    ).json()

    rejected = agent.post(
        "/live-workspaces/refund-control/baseline/approve",
        json={"actor_role": "finance-admin"},
    )

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "INVALID_REQUEST"
    current = agent.get("/live-workspaces/refund-control").json()
    assert current["baseline_approved"] is False
    assert current["baseline_approval_evidence"] is None
    assert current["baseline_proposal_fingerprint"] == (
        imported["baseline_proposal_fingerprint"]
    )
    assert current["baseline_proposal_instance_id"] == (
        imported["baseline_proposal_instance_id"]
    )
    assert current["graph_version"] == "graph-v17"
    assert not any(
        event["event_type"] == "decision.approval-rejected"
        for event in current["history"]
    )


def test_missing_baseline_confirmation_is_audited_before_permission_lookup(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _authority, _executor, store_path = live_services
    agent.post(
        "/live-workspaces/import",
        json=workspace_import().model_dump(mode="json"),
    )

    def permission_checker_must_not_run(_workspace: object) -> object:
        raise AssertionError("missing confirmation must stop before permission")

    monkeypatch.setattr(
        agent_api,
        "_workspace_permission_checker",
        permission_checker_must_not_run,
    )
    rejected = agent.post(
        "/live-workspaces/refund-control/baseline/approve",
        json={
            "approval_token": "test-user:finance-admin",
            "channel": "workspace-ui",
            "evidence_ref": "workspace-ui://refund-control/missing",
        },
    )

    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == (
        "MISSING_BASELINE_CONFIRMATION"
    )
    state = agent.get("/live-workspaces/refund-control").json()
    assert state["baseline_approved"] is False
    assert state["graph_version"] == "graph-v17"
    event = state["history"][-1]
    assert event["event_type"] == "decision.approval-rejected"
    assert event["data"]["disposition"] == "MISSING_CONFIRMATION"
    assert event["data"]["permission_id"] == "finance-admin"
    assert event["data"]["confirmed_proposal_fingerprint"] is None
    assert "test-user:finance-admin" not in store_path.read_text(
        encoding="utf-8"
    )


def test_baseline_approval_binds_current_proposal_and_persists_evidence(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _authority, _executor, store_path = live_services
    permission_calls: list[tuple[str, str]] = []
    selected_teams: list[str] = []

    class PermissionChecker:
        def has_permission(self, *, user_id: str, permission_id: str) -> bool:
            permission_calls.append((user_id, permission_id))
            return user_id == permission_id

    def checker_for(workspace: Mapping[str, object]) -> PermissionChecker:
        binding = workspace["slack_binding"]
        assert isinstance(binding, dict)
        selected_teams.append(str(binding["hexclave_team_id"]))
        return PermissionChecker()

    monkeypatch.setattr(
        agent_api,
        "_workspace_permission_checker",
        checker_for,
    )
    imported = agent.post(
        "/live-workspaces/import",
        json=workspace_import_with_slack_binding().model_dump(mode="json"),
    ).json()
    fingerprint = imported["baseline_proposal_fingerprint"]
    instance_id = imported["baseline_proposal_instance_id"]
    assert isinstance(fingerprint, str)
    assert isinstance(instance_id, str)

    stale = _approve_workspace_baseline(
        agent,
        fingerprint=f"sha256:{'0' * 64}",
        instance_id=instance_id,
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "STALE_BASELINE_CONFIRMATION"
    assert permission_calls == []
    stale_state = agent.get("/live-workspaces/refund-control").json()
    assert stale_state["graph_version"] == "graph-v17"
    stale_event = stale_state["history"][-1]
    assert stale_event["event_type"] == "decision.approval-rejected"
    assert stale_event["data"]["disposition"] == "STALE_CONFIRMATION"
    assert stale_event["data"]["permission_id"] == "finance-admin"

    evidence_ref = (
        f"workspace-ui://refund-control/baseline/{fingerprint}"
    )
    approved = _approve_workspace_baseline(
        agent,
        evidence_ref=evidence_ref,
    )
    assert approved.status_code == 200
    body = approved.json()
    evidence = body["baseline_approval_evidence"]
    assert evidence == {
        "workspace_id": "refund-control",
        "decision_id": "DEC-REFUND-1",
        "approver_user_id": "finance-admin",
        "permission_id": "finance-admin",
        "channel": "workspace-ui",
        "evidence_ref": evidence_ref,
        "approved_at": evidence["approved_at"],
        "confirmed_proposal_fingerprint": fingerprint,
        "confirmed_proposal_instance_id": instance_id,
    }
    assert permission_calls == [("finance-admin", "finance-admin")]
    assert selected_teams == ["hex-team-refunds"]

    repeated = _approve_workspace_baseline(
        agent,
        evidence_ref=evidence_ref,
    )
    assert repeated.status_code == 200
    assert repeated.json()["baseline_approval_evidence"] == evidence
    assert permission_calls == [("finance-admin", "finance-admin")]
    assert len(
        [
            event
            for event in repeated.json()["history"]
            if event["event_type"] == "baseline.approved"
        ]
    ) == 1

    cross_channel_retry = agent.post(
        "/live-workspaces/refund-control/baseline/approve",
        json={
            "approval_token": "test-user:engineer",
            "channel": "cli",
            "evidence_ref": "cli://refund-control/already-approved",
            "confirmed_proposal_fingerprint": fingerprint,
            "confirmed_proposal_instance_id": instance_id,
        },
    )
    assert cross_channel_retry.status_code == 200
    assert (
        cross_channel_retry.json()["baseline_approval_evidence"]
        == evidence
    )
    assert permission_calls == [("finance-admin", "finance-admin")]

    stored = JsonFileLiveWorkspaceRepository(store_path).get(
        "refund-control"
    )
    assert stored.baseline_approval_evidence is not None
    assert (
        stored.baseline_approval_evidence.confirmed_proposal_instance_id
        == instance_id
    )


def _apply_workspace_change(agent: TestClient) -> None:
    _authorize_workspace(agent)
    assert (
        agent.post(
            "/live-workspaces/refund-control/decisions/propose",
            json=decision_proposal_body(),
        ).status_code
        == 200
    )
    assert _approve_workspace_change(agent).json()["status"] == "change-applied"


def test_approval_preview_is_scope_sensitive_graph_derived_and_read_only(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, authority, _executor, _store_path = live_services
    _authorize_workspace(agent)
    proposed = agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    ).json()

    preview_response = agent.get(
        "/live-workspaces/refund-control/decisions/DEC-REFUND-2/preview"
    )

    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["pending"]["proposal_fingerprint"] == (
        proposed["pending_proposal_fingerprint"]
    )
    assert preview["pending"]["proposal_instance_id"] == (
        proposed["pending_proposal_instance_id"]
    )
    assert preview["interrupted_assignment_ids"] == [
        "ASSIGNMENT-TASK-ISSUE"
    ]
    assert preview["preserved_assignment_ids"] == [
        "ASSIGNMENT-TASK-CALCULATE"
    ]
    assert preview["interrupted_count"] == 1
    assert preview["preserved_count"] == 1
    assert preview["total_assignment_count"] == 2
    assert preview["assignment_provenance_paths"] == {
        "ASSIGNMENT-TASK-ISSUE": [
            "DEC-REFUND-2",
            "DEC-REFUND-1",
            "SPEC-REFUND",
            "PAY-104",
            "TASK-ISSUE",
        ]
    }
    unchanged = agent.get("/live-workspaces/refund-control").json()
    assert unchanged["status"] == "change-proposed"
    assert unchanged["graph_version"] == "graph-v17"
    authority_state = authority.get(
        "/live-workspaces/authority/contexts/live-refund-control"
    ).json()
    assert authority_state["graph_version"] == "graph-v17"
    assert all(
        artifact["id"] != "DEC-REFUND-2"
        for artifact in authority_state["artifacts"]
    )


def test_actor_role_only_cannot_apply_a_pending_decision(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, _authority, _executor, _store_path = live_services
    _authorize_workspace(agent)
    agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    )

    rejected = agent.post(
        "/live-workspaces/refund-control/decisions/DEC-REFUND-2/approve",
        json={"actor_role": "finance-admin"},
    )

    assert rejected.status_code == 422
    state = agent.get("/live-workspaces/refund-control").json()
    assert state["graph_version"] == "graph-v17"
    assert state["status"] == "change-proposed"
    assert state["approved_mutations"] == []
    assert not any(
        event["event_type"] == "decision.approval-rejected"
        for event in state["history"]
    )


@pytest.mark.parametrize("channel", ["cli", "workspace-ui"])
def test_missing_decision_confirmation_is_audited_without_permission_lookup(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
) -> None:
    agent, _authority, _executor, store_path = live_services
    _authorize_workspace(agent)
    proposed = agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    ).json()

    def permission_checker_must_not_run(_workspace: object) -> object:
        raise AssertionError("missing confirmation must stop before permission")

    monkeypatch.setattr(
        agent_api,
        "_workspace_permission_checker",
        permission_checker_must_not_run,
    )
    rejected = agent.post(
        "/live-workspaces/refund-control/decisions/DEC-REFUND-2/approve",
        json={
            "approval_token": "test-user:finance-admin",
            "channel": channel,
            "evidence_ref": f"{channel}://refund-control/missing",
        },
    )

    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == (
        "MISSING_PROPOSAL_CONFIRMATION"
    )
    state = agent.get("/live-workspaces/refund-control").json()
    assert state["graph_version"] == "graph-v17"
    assert state["pending_proposal_fingerprint"] == (
        proposed["pending_proposal_fingerprint"]
    )
    event = state["history"][-1]
    assert event["event_type"] == "decision.approval-rejected"
    assert event["data"]["disposition"] == "MISSING_CONFIRMATION"
    assert event["data"]["approval_channel"] == channel
    assert event["data"]["permission_id"] == "finance-admin"
    assert event["data"]["proposal_instance_id"] == (
        proposed["pending_proposal_instance_id"]
    )
    assert "test-user:finance-admin" not in store_path.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("unavailable", "status_code", "error_code", "disposition"),
    [
        (False, 403, "APPROVER_NOT_AUTHORIZED", "IGNORED_NOT_AUTHORIZED"),
        (
            True,
            503,
            "PERMISSION_CHECK_UNAVAILABLE",
            "PERMISSION_CHECK_UNAVAILABLE",
        ),
    ],
)
def test_decision_permission_rejections_are_durable_and_non_mutating(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
    unavailable: bool,
    status_code: int,
    error_code: str,
    disposition: str,
) -> None:
    agent, _authority, _executor, store_path = live_services
    _authorize_workspace(agent)
    proposed = agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    ).json()

    class Checker:
        def has_permission(self, **_kwargs: object) -> bool:
            if unavailable:
                raise HexclavePermissionError("permission service unavailable")
            return False

    monkeypatch.setattr(
        agent_api,
        "_workspace_permission_checker",
        lambda _workspace: Checker(),
    )
    rejected = _approve_workspace_change(agent)

    assert rejected.status_code == status_code
    assert rejected.json()["error"]["code"] == error_code
    state = agent.get("/live-workspaces/refund-control").json()
    assert state["graph_version"] == "graph-v17"
    assert state["status"] == "change-proposed"
    assert state["pending_proposal_instance_id"] == (
        proposed["pending_proposal_instance_id"]
    )
    event = state["history"][-1]
    assert event["event_type"] == "decision.approval-rejected"
    assert event["data"]["disposition"] == disposition
    assert event["data"]["approver_user_id"] == "finance-admin"
    assert event["data"]["permission_id"] == "finance-admin"
    assert "test-user:finance-admin" not in store_path.read_text(
        encoding="utf-8"
    )


def test_caller_cannot_impersonate_a_hexclave_user_in_approval_body(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, _authority, _executor, _store_path = live_services
    _authorize_workspace(agent)
    proposed = agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    ).json()
    fingerprint = proposed["pending_proposal_fingerprint"]
    instance_id = proposed["pending_proposal_instance_id"]

    rejected = agent.post(
        "/live-workspaces/refund-control/decisions/DEC-REFUND-2/approve",
        json={
            "approval_token": "test-user:engineer",
            "approver_user_id": "finance-admin",
            "channel": "workspace-ui",
            "evidence_ref": "workspace-ui://impersonation-attempt",
            "confirmed_proposal_fingerprint": fingerprint,
            "confirmed_proposal_instance_id": instance_id,
        },
    )

    assert rejected.status_code == 422
    state = agent.get("/live-workspaces/refund-control").json()
    assert state["graph_version"] == "graph-v17"
    assert state["approved_mutations"] == []
    assert not any(
        event["event_type"] == "decision.approval-rejected"
        for event in state["history"]
    )


def test_stale_confirmation_cannot_approve_reused_decision_id(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _authority, _executor, _store_path = live_services
    _authorize_workspace(agent)
    first = agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    ).json()
    first_fingerprint = first["pending_proposal_fingerprint"]
    first_instance_id = first["pending_proposal_instance_id"]
    assert agent.delete(
        "/live-workspaces/refund-control/decisions/pending"
    ).status_code == 200

    replacement = decision_proposal_body()
    replacement_decision = cast(dict[str, object], replacement["decision"])
    replacement_decision["text"] = "All refunds now require two approvers."
    replacement_decision["authority_role"] = "security-admin"
    attributes = cast(dict[str, object], replacement_decision["attributes"])
    requirements = cast(dict[str, object], attributes["requirements"])
    requirements["refund.execution"] = {"human_approval": "two_person"}
    second = agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=replacement,
    ).json()
    assert second["pending_proposal_fingerprint"] != first_fingerprint

    class MustNotCheckPermission:
        def has_permission(self, **_kwargs: object) -> bool:
            raise AssertionError("stale confirmation must stop before permission lookup")

    monkeypatch.setattr(
        agent_api,
        "_workspace_permission_checker",
        lambda _workspace: MustNotCheckPermission(),
    )
    stale = agent.post(
        "/live-workspaces/refund-control/decisions/DEC-REFUND-2/approve",
        json={
            "approval_token": "test-user:finance-admin",
            "channel": "workspace-ui",
            "evidence_ref": "workspace-ui://stale-confirmation",
            "confirmed_proposal_fingerprint": first_fingerprint,
            "confirmed_proposal_instance_id": first_instance_id,
        },
    )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "STALE_PROPOSAL_CONFIRMATION"
    state = agent.get("/live-workspaces/refund-control").json()
    assert state["graph_version"] == "graph-v17"
    assert state["status"] == "change-proposed"
    assert state["pending_proposal_fingerprint"] == (
        second["pending_proposal_fingerprint"]
    )
    event = state["history"][-1]
    assert event["event_type"] == "decision.approval-rejected"
    assert event["data"]["disposition"] == "STALE_CONFIRMATION"
    assert event["data"]["permission_id"] == "finance-admin"
    assert event["data"]["confirmed_proposal_fingerprint"] == (
        first_fingerprint
    )
    assert event["data"]["confirmed_proposal_instance_id"] == (
        first_instance_id
    )


def test_in_flight_approval_cannot_cross_identical_reproposal_instance(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, authority, _executor, _store_path = live_services
    _authorize_workspace(agent)
    first = agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    ).json()
    entered_permission_check = Event()
    release_permission_check = Event()

    class BlockingPermissionChecker:
        def has_permission(self, **_kwargs: object) -> bool:
            entered_permission_check.set()
            assert release_permission_check.wait(timeout=5)
            return True

    monkeypatch.setattr(
        agent_api,
        "_workspace_permission_checker",
        lambda _workspace: BlockingPermissionChecker(),
    )
    responses: list[httpx.Response] = []

    def approve_old_instance() -> None:
        responses.append(
            agent.post(
                (
                    "/live-workspaces/refund-control/decisions/"
                    "DEC-REFUND-2/approve"
                ),
                json={
                    "approval_token": "test-user:finance-admin",
                    "channel": "workspace-ui",
                    "evidence_ref": "workspace-ui://delayed-old-instance",
                    "confirmed_proposal_fingerprint": (
                        first["pending_proposal_fingerprint"]
                    ),
                    "confirmed_proposal_instance_id": (
                        first["pending_proposal_instance_id"]
                    ),
                },
            )
        )

    thread = Thread(target=approve_old_instance)
    thread.start()
    assert entered_permission_check.wait(timeout=5)
    assert agent.delete(
        "/live-workspaces/refund-control/decisions/pending"
    ).status_code == 200
    second = agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    ).json()
    assert second["pending_proposal_fingerprint"] == (
        first["pending_proposal_fingerprint"]
    )
    assert second["pending_proposal_instance_id"] != (
        first["pending_proposal_instance_id"]
    )
    release_permission_check.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert responses[0].status_code == 409
    state = agent.get("/live-workspaces/refund-control").json()
    assert state["status"] == "change-proposed"
    assert state["graph_version"] == "graph-v17"
    assert state["pending_proposal_instance_id"] == (
        second["pending_proposal_instance_id"]
    )
    assert state["approved_mutations"] == []
    authority_state = authority.get(
        "/live-workspaces/authority/contexts/live-refund-control"
    ).json()
    assert authority_state["graph_version"] == "graph-v17"


def test_approval_evidence_is_persisted_with_history_and_survives_reload(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, _authority, _executor, store_path = live_services
    _authorize_workspace(agent)
    proposed = agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    ).json()
    approved = _approve_workspace_change(agent).json()

    evidence = approved["approved_mutations"][0]["approval_evidence"]
    assert evidence == {
        "workspace_id": "refund-control",
        "decision_id": "DEC-REFUND-2",
        "approver_user_id": "finance-admin",
        "permission_id": "finance-admin",
        "channel": "workspace-ui",
        "evidence_ref": (
            "workspace-ui://refund-control/DEC-REFUND-2/"
            f"{proposed['pending_proposal_fingerprint']}"
        ),
        "approved_at": evidence["approved_at"],
        "confirmed_proposal_fingerprint": (
            proposed["pending_proposal_fingerprint"]
        ),
        "confirmed_proposal_instance_id": (
            proposed["pending_proposal_instance_id"]
        ),
    }
    approval_event = next(
        event
        for event in approved["history"]
        if event["event_type"] == "decision.approved"
    )
    assert approval_event["data"]["approver_user_id"] == "finance-admin"
    assert approval_event["data"]["approval_channel"] == "workspace-ui"
    assert approval_event["data"]["confirmed_proposal_fingerprint"] == (
        proposed["pending_proposal_fingerprint"]
    )
    assert approval_event["data"]["confirmed_proposal_instance_id"] == (
        proposed["pending_proposal_instance_id"]
    )

    cross_channel_retry = agent.post(
        (
            "/live-workspaces/refund-control/decisions/"
            "DEC-REFUND-2/approve"
        ),
        json={
            "approval_token": "test-user:engineer",
            "channel": "cli",
            "evidence_ref": "cli://refund-control/already-approved",
            "confirmed_proposal_fingerprint": (
                proposed["pending_proposal_fingerprint"]
            ),
            "confirmed_proposal_instance_id": (
                proposed["pending_proposal_instance_id"]
            ),
        },
    )
    assert cross_channel_retry.status_code == 200
    assert (
        cross_channel_retry.json()["approved_mutations"][0][
            "approval_evidence"
        ]
        == evidence
    )
    assert sum(
        event["event_type"] == "decision.approved"
        for event in cross_channel_retry.json()["history"]
    ) == 1

    reloaded = LiveWorkspaceView.from_record(
        JsonFileLiveWorkspaceRepository(store_path).get("refund-control")
    ).model_dump(mode="json")
    assert reloaded["approved_mutations"][0]["approval_evidence"] == evidence


def test_live_workspace_service_flow_is_real_selective_and_persistent(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, authority, _executor, store_path = live_services

    imported_response = agent.post(
        "/live-workspaces/import",
        json=workspace_import().model_dump(mode="json"),
    )
    assert imported_response.status_code == 201
    imported = imported_response.json()
    assert imported["status"] == "imported"
    assert imported["baseline_approved"] is False
    assert imported["graph_version"] == "graph-v17"
    assert imported["initial_plan"]["id"] == "PLAN-REFUND-1"
    assert imported["current_plan"]["id"] == "PLAN-REFUND-1"
    _assert_no_signed_token(imported)

    unapproved = agent.post("/live-workspaces/refund-control/authorize", json={})
    assert unapproved.status_code == 409

    rejected = _approve_workspace_baseline(agent, user_id="engineer")
    assert rejected.status_code == 403
    rejected_state = agent.get("/live-workspaces/refund-control").json()
    assert rejected_state["baseline_approved"] is False
    assert rejected_state["graph_version"] == "graph-v17"

    baseline = _approve_workspace_baseline(agent).json()
    assert baseline["status"] == "baseline-approved"
    assert baseline["baseline_approved"] is True
    assert baseline["baseline_decision"]["approval_status"] == "approved"

    authorized = agent.post(
        "/live-workspaces/refund-control/authorize",
        json={},
    ).json()
    assert authorized["status"] == "authorized"
    assert authorized["initial_authorization"]["verdict"] == "ALLOW"
    assert authorized["initial_authorization"]["grant"]["decision_snapshot"] == "graph-v17"
    _assert_no_signed_token(authorized)

    proposed = agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    ).json()
    assert proposed["status"] == "change-proposed"
    assert proposed["graph_version"] == "graph-v17"

    changed = _approve_workspace_change(agent).json()
    assert changed["status"] == "change-applied"
    assert changed["graph_version"] == "graph-v18"
    assert changed["latest_approved_mutation"]["decision"]["text"] == (
        "Refund execution requires human approval."
    )
    assert (
        changed["latest_approved_mutation"]["decision"]["approval_status"]
        == "approved"
    )
    assert changed["conflict_authorization"]["verdict"] == "REPLAN"
    assert changed["invalidation_report"]["invalidated_task_ids"] == ["TASK-ISSUE"]
    assert changed["invalidation_report"]["preserved_task_ids"] == ["TASK-CALCULATE"]
    plan_path = next(
        path["node_ids"]
        for path in changed["invalidation_report"]["paths"]
        if path["artifact_id"] == "PLAN-REFUND-1"
    )
    assert plan_path == [
        "DEC-REFUND-2",
        "DEC-REFUND-1",
        "SPEC-REFUND",
        "PAY-104",
        "TASK-ISSUE",
        "PLAN-REFUND-1",
    ]

    stale = agent.post(
        "/live-workspaces/refund-control/grants/initial/verify",
        json={},
    ).json()
    assert stale["status"] == "initial-grant-rejected"
    assert stale["initial_verification"]["applied"] is False
    assert stale["initial_verification"]["verification_code"] == "STALE_SNAPSHOT"

    updated = agent.put(
        "/live-workspaces/refund-control/plan",
        json=corrected_plan_body(),
    ).json()
    assert updated["status"] == "plan-updated"
    assert updated["initial_plan"]["id"] == "PLAN-REFUND-1"
    assert updated["current_plan"]["id"] == "PLAN-REFUND-2"

    reauthorized = agent.post(
        "/live-workspaces/refund-control/reauthorize",
        json={},
    ).json()
    assert reauthorized["status"] == "reauthorized"
    assert reauthorized["replacement_authorization"]["verdict"] == "ALLOW"
    assert (
        reauthorized["replacement_authorization"]["grant"]["decision_snapshot"]
        == "graph-v18"
    )

    complete = agent.post(
        "/live-workspaces/refund-control/grants/replacement/verify",
        json={},
    ).json()
    assert complete["status"] == "complete"
    assert complete["replacement_verification"]["applied"] is True
    assert complete["replacement_verification"]["verification_code"] == "VALID"
    assert complete["initial_plan"]["id"] == "PLAN-REFUND-1"
    assert complete["current_plan"]["id"] == "PLAN-REFUND-2"
    assert len(complete["history"]) == 10
    rejection = next(
        event
        for event in complete["history"]
        if event["event_type"] == "decision.approval-rejected"
    )
    assert rejection["data"]["disposition"] == "IGNORED_NOT_AUTHORIZED"
    assert rejection["data"]["permission_id"] == "finance-admin"
    _assert_no_signed_token(complete)

    persisted = JsonFileLiveWorkspaceRepository(store_path).get("refund-control")
    assert persisted.status is LiveWorkspaceStatus.COMPLETE
    assert persisted.initial_authorization is not None
    assert persisted.initial_authorization.grant is not None
    assert persisted.initial_authorization.grant.token
    listed = agent.get("/live-workspaces").json()
    assert listed["workspaces"][0]["id"] == "refund-control"
    assert listed["workspaces"][0]["initial_plan"]["id"] == "PLAN-REFUND-1"
    assert listed["workspaces"][0]["current_plan"]["id"] == "PLAN-REFUND-2"
    _assert_no_signed_token(listed)


def test_callwright_workspace_stops_the_stale_call_and_submits_only_the_correction(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _authority, _executor, _store_path = live_services
    callwright = FixtureCallwrightClient()
    monkeypatch.setattr(executor_api, "callwright_client", callwright)
    monkeypatch.setattr(
        executor_api,
        "settings",
        replace(
            executor_api.settings,
            execution_provider="fixture",
            callwright_demo_phone_number=None,
        ),
    )
    definition = callwright_workspace_import()

    imported = agent.post(
        "/live-workspaces/import",
        json=definition.model_dump(mode="json"),
    )
    assert imported.status_code == 201
    assert imported.json()["graph_version"] == "graph-v17"
    assert _approve_workspace_baseline(
        agent,
        workspace_id="voyagr-reservation",
        user_id="event-ops-lead",
    ).status_code == 200
    initial = agent.post(
        "/live-workspaces/voyagr-reservation/authorize",
        json={},
    ).json()
    assert initial["initial_authorization"]["verdict"] == "ALLOW"
    assert initial["initial_authorization"]["grant"]["decision_snapshot"] == (
        "graph-v17"
    )

    proposed = agent.post(
        "/live-workspaces/voyagr-reservation/decisions/propose",
        json=callwright_change_body(),
    )
    assert proposed.status_code == 200
    changed = _approve_workspace_change(
        agent,
        workspace_id="voyagr-reservation",
        decision_id="DEC-VOYAGR-002",
        user_id="event-ops-lead",
    ).json()
    assert changed["graph_version"] == "graph-v18"
    assert changed["conflict_authorization"]["verdict"] == "REPLAN"
    assert changed["invalidation_report"]["invalidated_task_ids"] == ["TASK-102"]
    assert changed["invalidation_report"]["preserved_task_ids"] == ["TASK-101"]
    assert changed["invalidation_report"]["directly_mentioned_artifact_ids"] == []
    assert "EVENT-208" not in changed["latest_approved_mutation"]["decision"]["text"]

    stale = agent.post(
        "/live-workspaces/voyagr-reservation/grants/initial/verify",
        json={},
    ).json()
    assert stale["status"] == "initial-grant-rejected"
    assert stale["initial_verification"]["verification_code"] == "STALE_SNAPSHOT"
    assert callwright.submission_count == 0

    updated = agent.put(
        "/live-workspaces/voyagr-reservation/plan",
        json=corrected_callwright_plan_body(),
    )
    assert updated.status_code == 200
    reauthorized = agent.post(
        "/live-workspaces/voyagr-reservation/reauthorize",
        json={},
    ).json()
    assert reauthorized["replacement_authorization"]["verdict"] == "ALLOW"
    assert reauthorized["replacement_authorization"]["grant"][
        "decision_snapshot"
    ] == "graph-v18"

    complete = agent.post(
        "/live-workspaces/voyagr-reservation/grants/replacement/verify",
        json={},
    ).json()
    receipt = complete["replacement_verification"]["call_receipt"]
    authorization_id = complete["replacement_authorization"]["grant"][
        "authorization_id"
    ]
    submitted = callwright.request_for(authorization_id)

    assert complete["status"] == "complete"
    assert complete["replacement_verification"]["applied"] is True
    assert complete["replacement_verification"]["verification_code"] == "VALID"
    assert complete["replacement_verification"]["execution_mode"] == "simulated"
    assert receipt["provider"] == "voyagr-callwright-fixture"
    assert receipt["status"] == "queued"
    assert callwright.submission_count == 1
    assert submitted is not None
    assert submitted.decision_snapshot == "graph-v18"
    assert "Requested time: 2026-07-26T20:30:00-07:00." in submitted.brief
    assert "2026-07-26T19:00:00-07:00" not in submitted.brief
    assert "+1" not in json.dumps(complete)
    _assert_no_signed_token(complete)


def test_supervisor_selectively_interrupts_redirects_and_completes_subagents(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _authority, _executor, _store_path = live_services
    callwright = FixtureCallwrightClient()
    monkeypatch.setattr(executor_api, "callwright_client", callwright)
    monkeypatch.setattr(
        executor_api,
        "settings",
        replace(
            executor_api.settings,
            execution_provider="fixture",
            callwright_demo_phone_number=None,
        ),
    )
    definition = callwright_workspace_import()

    imported = agent.post(
        "/live-workspaces/import",
        json=definition.model_dump(mode="json"),
    ).json()
    assert imported["supervisor"]["adapter"] == "fixture-agent-runtime"
    assert imported["supervisor"]["execution_mode"] == "simulated"
    assert imported["supervisor"]["state"] == "queued"
    assert len(imported["supervisor"]["assignments"]) == 2
    queued = {
        assignment["task_id"]: assignment
        for assignment in imported["supervisor"]["assignments"]
    }
    expected_task_101 = {
        "id": "ASSIGNMENT-TASK-101",
        "task_id": "TASK-101",
        "task_title": "Prepare guest summary",
        "agent_name": "Guest Summary Subagent",
        "runtime_provider": "codex",
        "execution_mode": "simulated",
        "run_id": "LIVE-VOYAGR-RESERVATION-TASK-101-RUN-1",
        "state": "queued",
        "scopes": ["event.copy"],
        "action_ids": ["ACTION-SUMMARY-001"],
        "authorized_actions": [
            (
                "Plan action ACTION-SUMMARY-001: Prepare a concise guest "
                "summary"
            ),
        ],
        "plan_id": "PLAN-VOYAGR-017",
        "decision_snapshot": "graph-v17",
        "redirected_from_run_id": None,
        "interrupt_reason": None,
        "redirect_instruction": None,
        "provenance_path": [],
        "interrupt_enforced": False,
    }
    assert {
        key: queued["TASK-101"][key] for key in expected_task_101
    } == expected_task_101
    assert queued["TASK-102"]["runtime_provider"] == "claude-code"
    assert queued["TASK-102"]["action_ids"] == ["ACTION-CALL-001"]
    assert queued["TASK-102"]["authorized_actions"] == [
        (
            "Plan action ACTION-CALL-001: Call the venue for 7:00 PM, "
            "the approved reservation time"
        ),
    ]

    baseline = _approve_workspace_baseline(
        agent,
        workspace_id="voyagr-reservation",
        user_id="event-ops-lead",
    ).json()
    assert baseline["supervisor"] == imported["supervisor"]

    authorized = agent.post(
        "/live-workspaces/voyagr-reservation/authorize",
        json={},
    ).json()
    running = {
        assignment["task_id"]: assignment
        for assignment in authorized["supervisor"]["assignments"]
    }
    assert authorized["supervisor"]["state"] == "running"
    assert {assignment["state"] for assignment in running.values()} == {"running"}
    assert {
        assignment["decision_snapshot"] for assignment in running.values()
    } == {"graph-v17"}

    proposed = agent.post(
        "/live-workspaces/voyagr-reservation/decisions/propose",
        json=callwright_change_body(),
    ).json()
    assert proposed["status"] == "change-proposed"
    assert proposed["supervisor"] == authorized["supervisor"]

    changed = _approve_workspace_change(
        agent,
        workspace_id="voyagr-reservation",
        decision_id="DEC-VOYAGR-002",
        user_id="event-ops-lead",
    ).json()
    changed_assignments = {
        assignment["task_id"]: assignment
        for assignment in changed["supervisor"]["assignments"]
    }
    assert changed["invalidation_report"]["preserved_task_ids"] == ["TASK-101"]
    assert changed["invalidation_report"]["invalidated_task_ids"] == ["TASK-102"]
    assert changed["supervisor"]["state"] == "interrupting"
    assert changed_assignments["TASK-101"]["state"] == "continuing"
    assert changed_assignments["TASK-101"]["run_id"] == running["TASK-101"]["run_id"]
    assert changed_assignments["TASK-101"]["provenance_path"] == []
    assert changed_assignments["TASK-102"]["state"] == "interrupted"
    assert changed_assignments["TASK-102"]["run_id"] == running["TASK-102"]["run_id"]
    assert changed_assignments["TASK-102"]["interrupt_enforced"] is False
    assert changed_assignments["TASK-102"]["redirect_instruction"] == (
        "Stop Call venue for the approved time. Return control to the writ.ai "
        "supervisor and request a corrected plan for reservation.time at graph-v18."
    )
    assert changed_assignments["TASK-102"]["provenance_path"] == [
        "DEC-VOYAGR-002",
        "DEC-VOYAGR-001",
        "SPEC-VOYAGR-001",
        "EVENT-208",
        "TASK-102",
        "PLAN-VOYAGR-017",
    ]

    stale = agent.post(
        "/live-workspaces/voyagr-reservation/grants/initial/verify",
        json={},
    ).json()
    stale_assignments = {
        assignment["task_id"]: assignment
        for assignment in stale["supervisor"]["assignments"]
    }
    assert stale["initial_verification"]["verification_code"] == "STALE_SNAPSHOT"
    assert stale_assignments["TASK-101"]["state"] == "continuing"
    assert stale_assignments["TASK-101"]["interrupt_enforced"] is False
    assert stale_assignments["TASK-102"]["state"] == "interrupted"
    assert stale_assignments["TASK-102"]["interrupt_enforced"] is True
    assert callwright.submission_count == 0

    updated = agent.put(
        "/live-workspaces/voyagr-reservation/plan",
        json=corrected_callwright_plan_body(),
    ).json()
    redirected = {
        assignment["task_id"]: assignment
        for assignment in updated["supervisor"]["assignments"]
    }
    assert updated["supervisor"]["state"] == "redirecting"
    assert redirected["TASK-101"] == stale_assignments["TASK-101"]
    assert redirected["TASK-102"]["state"] == "redirected"
    assert redirected["TASK-102"]["run_id"] == (
        "LIVE-VOYAGR-RESERVATION-TASK-102-RUN-2"
    )
    assert redirected["TASK-102"]["redirected_from_run_id"] == (
        "LIVE-VOYAGR-RESERVATION-TASK-102-RUN-1"
    )
    assert redirected["TASK-102"]["plan_id"] == "PLAN-VOYAGR-018"
    assert redirected["TASK-102"]["decision_snapshot"] == "graph-v18"
    assert redirected["TASK-102"]["authorized_actions"][-1] == (
        "Plan action ACTION-CALL-001: Call the venue for 8:30 PM, "
        "the newly approved time"
    )
    assert "approved reservation time" not in redirected["TASK-102"][
        "authorized_actions"
    ][-1]
    assert "corrected plan PLAN-VOYAGR-018" in redirected["TASK-102"][
        "redirect_instruction"
    ]

    reauthorized = agent.post(
        "/live-workspaces/voyagr-reservation/reauthorize",
        json={},
    ).json()
    resumed = {
        assignment["task_id"]: assignment
        for assignment in reauthorized["supervisor"]["assignments"]
    }
    assert reauthorized["supervisor"]["state"] == "resumed"
    assert resumed["TASK-101"]["state"] == "continuing"
    assert resumed["TASK-102"]["state"] == "resumed"
    assert resumed["TASK-102"]["decision_snapshot"] == "graph-v18"

    complete = agent.post(
        "/live-workspaces/voyagr-reservation/grants/replacement/verify",
        json={},
    ).json()
    completed = {
        assignment["task_id"]: assignment
        for assignment in complete["supervisor"]["assignments"]
    }
    assert complete["status"] == "complete"
    assert complete["supervisor"]["state"] == "completed"
    assert completed["TASK-101"]["state"] == "continuing"
    assert completed["TASK-102"]["state"] == "completed"
    assert completed["TASK-102"]["run_id"] == resumed["TASK-102"]["run_id"]
    assert callwright.submission_count == 1


def test_legacy_workspace_record_without_supervisor_loads_safely(
    tmp_path: Path,
) -> None:
    definition = workspace_import()
    legacy = LiveWorkspaceRecord(
        definition=definition,
        context_id="live-refund-control",
        graph_version="graph-v17",
        current_plan=definition.plan,
    ).model_dump(mode="json")
    legacy.pop("supervisor")
    store_path = tmp_path / "legacy-workspaces.json"
    store_path.write_text(
        json.dumps({"schema_version": 1, "workspaces": [legacy]}),
        encoding="utf-8",
    )

    loaded = JsonFileLiveWorkspaceRepository(store_path).get("refund-control")

    assert loaded.supervisor is None
    assert LiveWorkspaceView.from_record(loaded).supervisor is None


def test_restart_rehydrates_authority_by_replaying_approved_changes(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _authority, _executor, store_path = live_services
    agent.post(
        "/live-workspaces/import",
        json=workspace_import().model_dump(mode="json"),
    )
    _approve_workspace_baseline(agent)
    agent.post("/live-workspaces/refund-control/authorize", json={})
    agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    )
    changed = _approve_workspace_change(agent)
    assert changed.status_code == 200

    monkeypatch.setattr(
        authority_api,
        "workspace_contexts",
        DynamicAuthorityContextRegistry(
            grant_secret="live-workspace-test-secret",
            grant_ttl_seconds=3600,
            authority_threshold=0.75,
        ),
    )
    monkeypatch.setattr(
        agent_api,
        "workspace_orchestrator",
        LiveWorkspaceOrchestrator(
            repository=JsonFileLiveWorkspaceRepository(store_path)
        ),
    )

    verified = agent.post(
        "/live-workspaces/refund-control/grants/initial/verify",
        json={},
    )
    assert verified.status_code == 200
    body = verified.json()
    assert body["graph_version"] == "graph-v18"
    assert body["initial_verification"]["verification_code"] == "STALE_SNAPSHOT"
    state = TestClient(authority_api.app).get(
        "/live-workspaces/authority/contexts/live-refund-control"
    ).json()
    assert state["graph_version"] == "graph-v18"
    artifacts = {artifact["id"]: artifact for artifact in state["artifacts"]}
    assert artifacts["TASK-CALCULATE"]["validity"] == "VALID"
    assert artifacts["TASK-ISSUE"]["validity"] == "INVALIDATED"


def test_plan_update_requires_executor_proof_of_stale_snapshot(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, _authority, _executor, _store_path = live_services
    _apply_workspace_change(agent)

    skipped_verification = agent.put(
        "/live-workspaces/refund-control/plan",
        json=corrected_plan_body(),
    )

    assert skipped_verification.status_code == 409
    state = agent.get("/live-workspaces/refund-control").json()
    assert state["status"] == "change-applied"
    assert state["initial_verification"] is None


def test_non_stale_grant_failure_cannot_unlock_replanning(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _authority, _executor, _store_path = live_services
    _apply_workspace_change(agent)

    def expired_verification(**_kwargs: object) -> GrantVerificationResult:
        return GrantVerificationResult(
            valid=False,
            code=VerificationCode.EXPIRED,
            reason="The injected grant has expired.",
        )

    monkeypatch.setattr(executor_api, "post_model", expired_verification)
    verified = agent.post(
        "/live-workspaces/refund-control/grants/initial/verify",
        json={},
    )

    assert verified.status_code == 200
    body = verified.json()
    assert body["status"] == "change-applied"
    assert body["initial_verification"]["verification_code"] == "EXPIRED"
    blocked = agent.put(
        "/live-workspaces/refund-control/plan",
        json=corrected_plan_body(),
    )
    assert blocked.status_code == 409


def test_completion_rechecks_persisted_stale_snapshot_proof(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, _authority, _executor, store_path = live_services
    _apply_workspace_change(agent)
    agent.post(
        "/live-workspaces/refund-control/grants/initial/verify",
        json={},
    )
    agent.put(
        "/live-workspaces/refund-control/plan",
        json=corrected_plan_body(),
    )
    reauthorized = agent.post(
        "/live-workspaces/refund-control/reauthorize",
        json={},
    )
    assert reauthorized.json()["status"] == "reauthorized"

    repository = JsonFileLiveWorkspaceRepository(store_path)
    record = repository.get("refund-control")
    record.initial_verification = WorkspaceExecutionResult(
        applied=False,
        reason="Persisted proof was replaced with a non-stale failure.",
        verification_code=VerificationCode.EXPIRED,
    )
    repository.save(record)

    completion = agent.post(
        "/live-workspaces/refund-control/grants/replacement/verify",
        json={},
    )
    assert completion.status_code == 409
    assert agent.get("/live-workspaces/refund-control").json()["status"] == (
        "reauthorized"
    )


def test_failed_replacement_verification_cannot_rotate_authorization(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, _authority, _executor, _store_path = live_services
    _apply_workspace_change(agent)
    agent.post(
        "/live-workspaces/refund-control/grants/initial/verify",
        json={},
    )
    agent.put(
        "/live-workspaces/refund-control/plan",
        json=corrected_plan_body(),
    )
    first = agent.post(
        "/live-workspaces/refund-control/reauthorize",
        json={},
    ).json()

    def failed_execution(**_kwargs: object) -> WorkspaceExecutionResult:
        return WorkspaceExecutionResult(
            applied=False,
            reason="The protected execution outcome needs review.",
            verification_code=VerificationCode.VALID,
            execution_mode="live",
        )

    monkeypatch.setattr(
        agent_api.workspace_orchestrator._transport,
        "execute",
        failed_execution,
    )
    failed = agent.post(
        "/live-workspaces/refund-control/grants/replacement/verify",
        json={},
    ).json()
    repeated = agent.post(
        "/live-workspaces/refund-control/reauthorize",
        json={},
    ).json()

    assert failed["status"] == "reauthorized"
    assert failed["replacement_verification"]["applied"] is False
    assert repeated["replacement_authorization"] == first[
        "replacement_authorization"
    ]
    assert repeated["replacement_verification"] == failed[
        "replacement_verification"
    ]
    assert len(repeated["history"]) == len(failed["history"])


def test_missing_supersession_target_is_rejected_before_persistence_and_by_authority(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, authority, _executor, _store_path = live_services
    _authorize_workspace(agent)
    proposal = decision_proposal_body()
    proposal["supersedes_id"] = "DEC-DOES-NOT-EXIST"

    rejected = agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=proposal,
    )
    assert rejected.status_code == 409
    state = agent.get("/live-workspaces/refund-control").json()
    assert state["status"] == "authorized"
    assert state["pending_mutation"] is None

    direct = authority.post(
        (
            "/live-workspaces/authority/contexts/live-refund-control/"
            "mutations/approve"
        ),
        json={
            "mutation": {
                "decision": proposal["decision"],
                "supersedes_id": "DEC-DOES-NOT-EXIST",
                "affected_scopes": proposal["affected_scopes"],
            },
            "actor_role": "finance-admin",
        },
    )
    assert direct.status_code == 422
    assert direct.json()["error"]["code"] == "INVALID_REQUEST"
    assert (
        authority.get(
            "/live-workspaces/authority/contexts/live-refund-control"
        ).json()["graph_version"]
        == "graph-v17"
    )


def test_well_formed_authority_mutation_route_requires_internal_service_auth(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, authority, _executor, _store_path = live_services
    _authorize_workspace(agent)
    state = agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    ).json()
    pending = pending_from_workspace(state)
    assert pending is not None
    evidence = ApprovalEvidence(
        workspace_id=pending.workspace_id,
        decision_id=pending.decision_id,
        approver_user_id="finance-admin",
        permission_id=pending.permission_id,
        channel=ApprovalChannel.WORKSPACE_UI,
        evidence_ref="workspace-ui://direct-authority-bypass",
        approved_at=datetime(2026, 7, 25, 5, 0, tzinfo=UTC),
        confirmed_proposal_fingerprint=pending.proposal_fingerprint,
        confirmed_proposal_instance_id=pending.proposal_instance_id,
    )
    request = DynamicMutationApprovalRequest(
        mutation=state["pending_mutation"],
        actor_role=pending.permission_id,
        proposal_fingerprint=pending.proposal_fingerprint,
        approval_evidence=evidence,
    )

    rejected = authority.post(
        (
            "/live-workspaces/authority/contexts/live-refund-control/"
            "mutations/approve"
        ),
        json=request.model_dump(mode="json"),
    )

    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "INTERNAL_SERVICE_AUTH_REQUIRED"
    context = authority.get(
        "/live-workspaces/authority/contexts/live-refund-control"
    ).json()
    assert context["graph_version"] == "graph-v17"
    assert context["approval_evidence"] == {}


def test_authority_baseline_route_requires_internal_service_auth(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, authority, _executor, _store_path = live_services
    assert agent.post(
        "/live-workspaces/import",
        json=workspace_import().model_dump(mode="json"),
    ).status_code == 201
    definition = workspace_import()
    assert authority.post(
        "/live-workspaces/authority/contexts",
        json=DynamicAuthorityContextCreateRequest(
            context_id="live-refund-control",
            version=17,
            artifacts=definition.graph_artifacts(),
            edges=definition.graph_edges(),
            authority_policy=definition.authority_policy,
            baseline_decision_id=definition.baseline_decision.id,
        ).model_dump(mode="json"),
        headers={
            support.INTERNAL_SERVICE_AUTH_HEADER:
                support.internal_service_token(
                    authority_api.settings.grant_secret
                )
        },
    ).status_code == 201

    rejected = authority.post(
        (
            "/live-workspaces/authority/contexts/live-refund-control/"
            "baseline/approve"
        ),
        json={"actor_role": "finance-admin"},
    )

    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "INTERNAL_SERVICE_AUTH_REQUIRED"
    state = authority.get(
        "/live-workspaces/authority/contexts/live-refund-control"
    ).json()
    assert state["baseline_approved"] is False
    assert state["graph_version"] == "graph-v17"


def test_direct_approved_ingest_is_disabled_outside_demo_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_api.runtime.reset()
    monkeypatch.setattr(
        authority_api,
        "settings",
        replace(authority_api.settings, demo_reset_enabled=False),
    )

    rejected = TestClient(authority_api.app).post(
        "/decisions/ingest",
        json=load_decision_v18().model_dump(mode="json"),
    )

    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "FIXTURE_INGEST_DISABLED"
    assert authority_api.runtime.graph.version_label == "graph-v17"


def test_authority_context_create_and_delete_require_internal_service_auth(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    _agent, authority, _executor, _store_path = live_services
    definition = workspace_import()
    request = DynamicAuthorityContextCreateRequest(
        context_id="live-refund-control",
        version=17,
        artifacts=definition.graph_artifacts(),
        edges=definition.graph_edges(),
        authority_policy=definition.authority_policy,
        baseline_decision_id=definition.baseline_decision.id,
    )

    rejected_create = authority.post(
        "/live-workspaces/authority/contexts",
        json=request.model_dump(mode="json"),
    )

    assert rejected_create.status_code == 403
    assert rejected_create.json()["error"]["code"] == (
        "INTERNAL_SERVICE_AUTH_REQUIRED"
    )
    assert authority.get(
        "/live-workspaces/authority/contexts/live-refund-control"
    ).status_code == 404

    headers = {
        support.INTERNAL_SERVICE_AUTH_HEADER:
            support.internal_service_token(
                authority_api.settings.grant_secret
            )
    }
    assert authority.post(
        "/live-workspaces/authority/contexts",
        json=request.model_dump(mode="json"),
        headers=headers,
    ).status_code == 201

    rejected_delete = authority.delete(
        "/live-workspaces/authority/contexts/live-refund-control"
    )

    assert rejected_delete.status_code == 403
    assert rejected_delete.json()["error"]["code"] == (
        "INTERNAL_SERVICE_AUTH_REQUIRED"
    )
    assert authority.get(
        "/live-workspaces/authority/contexts/live-refund-control"
    ).status_code == 200
    assert authority.delete(
        "/live-workspaces/authority/contexts/live-refund-control",
        headers=headers,
    ).status_code == 200


def test_bad_pending_proposal_can_be_canceled_and_replaced(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, _authority, _executor, _store_path = live_services
    _authorize_workspace(agent)
    bad = decision_proposal_body()
    bad_decision = cast(dict[str, object], bad["decision"])
    bad_decision["authority_role"] = "engineer"

    assert (
        agent.post(
            "/live-workspaces/refund-control/decisions/propose",
            json=bad,
        ).json()["status"]
        == "change-proposed"
    )
    rejected = _approve_workspace_change(agent, user_id="engineer")
    assert rejected.status_code == 409
    assert agent.get("/live-workspaces/refund-control").json()["status"] == (
        "change-proposed"
    )

    canceled = agent.delete(
        "/live-workspaces/refund-control/decisions/pending"
    )
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "authorized"
    assert canceled.json()["pending_mutation"] is None
    replacement = agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    )
    assert replacement.status_code == 200
    assert replacement.json()["status"] == "change-proposed"


def test_existing_context_with_wrong_lineage_is_rebuilt_before_mutation(
    live_services: tuple[TestClient, TestClient, TestClient, Path],
) -> None:
    agent, authority, _executor, _store_path = live_services
    _authorize_workspace(agent)
    assert (
        authority.delete(
            "/live-workspaces/authority/contexts/live-refund-control",
            headers={
                support.INTERNAL_SERVICE_AUTH_HEADER:
                    support.internal_service_token(
                        authority_api.settings.grant_secret
                    )
            },
        ).status_code
        == 200
    )
    poisoned = workspace_import()
    poisoned.baseline_decision.text = "Poisoned baseline with the same IDs."
    created = authority.post(
        "/live-workspaces/authority/contexts",
        json=DynamicAuthorityContextCreateRequest(
            context_id="live-refund-control",
            version=17,
            artifacts=poisoned.graph_artifacts(),
            edges=poisoned.graph_edges(),
            authority_policy=poisoned.authority_policy,
            baseline_decision_id=poisoned.baseline_decision.id,
        ).model_dump(mode="json"),
        headers={
            support.INTERNAL_SERVICE_AUTH_HEADER:
                support.internal_service_token(
                    authority_api.settings.grant_secret
                )
        },
    )
    assert created.status_code == 201
    assert (
        authority.post(
            (
                "/live-workspaces/authority/contexts/live-refund-control/"
                "baseline/approve"
            ),
            json={"actor_role": "finance-admin"},
            headers={
                support.INTERNAL_SERVICE_AUTH_HEADER: (
                    support.internal_service_token(
                        authority_api.settings.grant_secret
                    )
                )
            },
        ).status_code
        == 200
    )

    agent.post(
        "/live-workspaces/refund-control/decisions/propose",
        json=decision_proposal_body(),
    )
    applied = _approve_workspace_change(agent)
    assert applied.status_code == 200
    assert applied.json()["graph_version"] == "graph-v18"
    state = authority.get(
        "/live-workspaces/authority/contexts/live-refund-control"
    ).json()
    baseline = next(
        artifact
        for artifact in state["artifacts"]
        if artifact["id"] == "DEC-REFUND-1"
    )
    assert baseline["text"] == workspace_import().baseline_decision.text
