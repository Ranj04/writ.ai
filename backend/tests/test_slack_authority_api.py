from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi.testclient import TestClient
from writai.domain import ApprovalStatus, Artifact, ArtifactKind
from writai.intake.gate import (
    DeterministicIntakeGate,
    GateResult,
    IntakeGateDisposition,
    IntakeGateReason,
)
from writai.intake.slack import SlackIntakeOutcome, VerifiedSlackMessage
from writai.services import authority_api
from writai.services.support import ApiError
from writai.workspaces.authority_contexts import (
    DynamicAuthorityContextNotFound,
)
from writai.workspaces.models import (
    SlackUserIdentityBinding,
    WorkspaceProposalRequest,
    WorkspaceSlackBinding,
)


def _message() -> VerifiedSlackMessage:
    return VerifiedSlackMessage(
        event_id="evt-1",
        connection_user_id="writai-user",
        author_user_id="U1",
        team_id="T1",
        channel_id="C1",
        message_ts="1784952300.000001",
        delivered_at=datetime(2026, 7, 25, 4, 5, tzinfo=UTC),
        text="Exports must be admin-only.",
        source_ref="slack://T1/C1/1784952300.000001",
    )


def _proposal() -> WorkspaceProposalRequest:
    return WorkspaceProposalRequest(
        decision=Artifact(
            id="DEC-018",
            kind=ArtifactKind.DECISION,
            title="Admin-only exports",
            scopes={"export.authorization"},
            approval_status=ApprovalStatus.PROPOSAL,
            authority_role="approve_compliance",
            effective_at=datetime(2026, 7, 25, 4, 5, tzinfo=UTC),
            source_ref="slack://T1/C1/1784952300.000001",
            attributes={
                "requirements": {
                    "export.authorization": {
                        "audience": "admin_only"
                    }
                }
            },
        ),
        supersedes_id="DEC-004",
        affected_scopes={"export.authorization"},
    )


def test_parked_slack_candidate_never_calls_agent_or_mutates_graph(
    monkeypatch,
) -> None:
    outcome = SlackIntakeOutcome(
        workspace_id="csv-exports",
        message=_message(),
        gate=GateResult(
            disposition=IntakeGateDisposition.PARKED,
            reasons=(IntakeGateReason.AUTHOR_NOT_AUTHORIZED,),
            affected_scopes=frozenset({"export.authorization"}),
        ),
        proposal=None,
    )
    monkeypatch.setattr(
        authority_api,
        "_process_slack_delivery",
        lambda **kwargs: outcome,
    )
    monkeypatch.setattr(
        authority_api,
        "post_model",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("parked intake must not call the agent")
        ),
    )

    response = TestClient(authority_api.app).post(
        "/intake/slack/csv-exports",
        content=b"signed",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "parked"
    assert response.json()["graph_mutated"] is False


def test_eligible_slack_draft_uses_existing_workspace_proposal_route(
    monkeypatch,
) -> None:
    proposal = _proposal()
    outcome = SlackIntakeOutcome(
        workspace_id="csv-exports",
        message=_message(),
        gate=GateResult(
            disposition=IntakeGateDisposition.PENDING,
            reasons=(),
            affected_scopes=frozenset({"export.authorization"}),
            permission_id="approve_compliance",
            checked_permissions=("approve_compliance",),
        ),
        proposal=proposal,
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        authority_api,
        "_process_slack_delivery",
        lambda **kwargs: outcome,
    )

    def fake_post_model(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(graph_version="graph-v17")

    monkeypatch.setattr(authority_api, "post_model", fake_post_model)
    response = TestClient(authority_api.app).post(
        "/intake/slack/csv-exports",
        content=b"signed",
    )

    assert response.status_code == 200
    url = calls[0]["url"]
    assert isinstance(url, str)
    assert url.endswith(
        "/live-workspaces/csv-exports/decisions/propose"
    )
    assert calls[0]["payload"] == proposal
    body = response.json()
    assert body["status"] == "pending-human-confirmation"
    assert body["graph_mutated"] is False
    assert body["human_reviewed"] is False


class RecordingVerifier:
    def __init__(self, message: VerifiedSlackMessage) -> None:
        self.message = message
        self.calls = 0

    def parse_message(self, **_kwargs: object) -> VerifiedSlackMessage:
        self.calls += 1
        return self.message


class Contexts:
    def __init__(self, state: object | None) -> None:
        self._state = state

    def state(self, _context_id: str):
        if self._state is None:
            raise DynamicAuthorityContextNotFound("missing")
        return self._state


def _bound_state(
    *,
    workspace_id: str = "csv-exports",
    slack_team_id: str = "T1",
    connection_user_id: str = "writai-user",
) -> SimpleNamespace:
    return SimpleNamespace(
        baseline_decision_id="DEC-004",
        authority_policy={
            "export.authorization": {"approve_compliance"},
        },
        artifacts=[],
        slack_binding=WorkspaceSlackBinding(
            workspace_id=workspace_id,
            slack_team_id=slack_team_id,
            composio_connection_user_id=connection_user_id,
            hexclave_team_id="hex-team-csv",
            user_identities=(
                SlackUserIdentityBinding(
                    slack_user_id="U1",
                    hexclave_user_id="hex-user-1",
                    evidence_ref="provisioning://csv-exports/U1",
                ),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("workspace_id", "state"),
    [
        ("csv-exports", _bound_state(slack_team_id="T-WRONG")),
        ("csv-exports", _bound_state(connection_user_id="other-connection")),
        ("other-workspace", _bound_state(workspace_id="csv-exports")),
    ],
)
def test_signed_event_binding_mismatch_stops_before_extraction_or_permission(
    monkeypatch: pytest.MonkeyPatch,
    workspace_id: str,
    state: SimpleNamespace,
) -> None:
    verifier = RecordingVerifier(_message())
    downstream_calls: list[str] = []
    monkeypatch.setattr(
        authority_api,
        "_live_slack_verifier",
        lambda: verifier,
    )
    monkeypatch.setattr(authority_api, "workspace_contexts", Contexts(state))
    monkeypatch.setattr(
        authority_api,
        "build_decision_extractor",
        lambda _settings: downstream_calls.append("extractor"),
    )
    monkeypatch.setattr(
        authority_api,
        "HexclavePermissionChecker",
        lambda **_kwargs: downstream_calls.append("permission"),
    )

    with pytest.raises(ApiError) as raised:
        authority_api._process_slack_delivery(
            workspace_id=workspace_id,
            body=b"signed",
            headers={"x-test-signature": "verified"},
        )

    assert raised.value.status_code == 403
    assert raised.value.code == "SLACK_WORKSPACE_BINDING_MISMATCH"
    assert verifier.calls == 1
    assert downstream_calls == []


def test_unmapped_slack_author_stops_before_extraction_or_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = RecordingVerifier(_message())
    downstream_calls: list[str] = []
    state = _bound_state()
    state.slack_binding = state.slack_binding.model_copy(
        update={"user_identities": ()}
    )
    monkeypatch.setattr(authority_api, "_live_slack_verifier", lambda: verifier)
    monkeypatch.setattr(authority_api, "workspace_contexts", Contexts(state))
    monkeypatch.setattr(
        authority_api,
        "build_decision_extractor",
        lambda _settings: downstream_calls.append("extractor"),
    )
    monkeypatch.setattr(
        authority_api,
        "HexclavePermissionChecker",
        lambda **_kwargs: downstream_calls.append("permission"),
    )

    with pytest.raises(ApiError) as raised:
        authority_api._process_slack_delivery(
            workspace_id="csv-exports",
            body=b"signed",
            headers={"x-test-signature": "verified"},
        )

    assert raised.value.status_code == 403
    assert raised.value.code == "SLACK_IDENTITY_BINDING_NOT_FOUND"
    assert verifier.calls == 1
    assert downstream_calls == []


def test_signed_event_for_unknown_workspace_authenticates_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = RecordingVerifier(_message())
    downstream_calls: list[str] = []
    monkeypatch.setattr(
        authority_api,
        "_live_slack_verifier",
        lambda: verifier,
    )
    monkeypatch.setattr(authority_api, "workspace_contexts", Contexts(None))
    monkeypatch.setattr(
        authority_api,
        "build_decision_extractor",
        lambda _settings: downstream_calls.append("extractor"),
    )

    with pytest.raises(ApiError) as raised:
        authority_api._process_slack_delivery(
            workspace_id="unknown-workspace",
            body=b"signed",
            headers={"x-test-signature": "verified"},
        )

    assert raised.value.status_code == 404
    assert verifier.calls == 1
    assert downstream_calls == []


def test_matching_binding_uses_workspace_bound_hexclave_team(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = RecordingVerifier(_message())
    captured_checker_arguments: list[dict[str, object]] = []
    checker = object()
    outcome = SlackIntakeOutcome(
        workspace_id="csv-exports",
        message=_message(),
        gate=GateResult(
            disposition=IntakeGateDisposition.PARKED,
            reasons=(IntakeGateReason.LOW_CONFIDENCE,),
            affected_scopes=frozenset({"export.authorization"}),
        ),
        proposal=None,
    )

    class Intake:
        def __init__(self, **kwargs: object) -> None:
            gate = cast(DeterministicIntakeGate, kwargs["gate"])
            assert gate._permission_checker is checker

        def ingest_verified(self, **kwargs: object) -> SlackIntakeOutcome:
            assert kwargs["message"] == _message()
            assert kwargs["authority_user_id"] == "hex-user-1"
            assert kwargs["supersession_root_id"] == "DEC-004"
            return outcome

    def checker_factory(**kwargs: object) -> object:
        captured_checker_arguments.append(kwargs)
        return checker

    monkeypatch.setattr(
        authority_api,
        "_live_slack_verifier",
        lambda: verifier,
    )
    monkeypatch.setattr(
        authority_api,
        "workspace_contexts",
        Contexts(_bound_state()),
    )
    monkeypatch.setattr(
        authority_api,
        "build_decision_extractor",
        lambda _settings: object(),
    )
    monkeypatch.setattr(
        authority_api,
        "HexclavePermissionChecker",
        checker_factory,
    )
    monkeypatch.setattr(authority_api, "hexclave_permission_checkers", {})
    monkeypatch.setattr(
        authority_api,
        "settings",
        replace(
            authority_api.settings,
            hexclave_api_url="https://verified.hexclave.example/api/v1",
            hexclave_permission_cache_ttl_seconds=17,
        ),
    )
    monkeypatch.setattr(authority_api, "SlackDecisionIntake", Intake)

    result = authority_api._process_slack_delivery(
        workspace_id="csv-exports",
        body=b"signed",
        headers={"x-test-signature": "verified"},
    )

    assert result == outcome
    assert captured_checker_arguments[0]["team_id"] == "hex-team-csv"
    assert captured_checker_arguments[0]["api_url"] == (
        "https://verified.hexclave.example/api/v1"
    )
    assert captured_checker_arguments[0]["cache_ttl_seconds"] == 17
    assert verifier.calls == 1


def test_authority_reuses_hexclave_checker_for_same_team(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker_arguments: list[dict[str, object]] = []
    first_checker = object()
    second_checker = object()
    checkers = iter((first_checker, second_checker))

    def checker_factory(**kwargs: object) -> object:
        checker_arguments.append(kwargs)
        return next(checkers)

    monkeypatch.setattr(authority_api, "hexclave_permission_checkers", {})
    monkeypatch.setattr(
        authority_api,
        "HexclavePermissionChecker",
        checker_factory,
    )

    first = authority_api._hexclave_permission_checker("hex-team-csv")
    repeated = authority_api._hexclave_permission_checker("hex-team-csv")
    other = authority_api._hexclave_permission_checker("hex-team-legal")

    assert first is repeated is first_checker
    assert other is second_checker
    assert [item["team_id"] for item in checker_arguments] == [
        "hex-team-csv",
        "hex-team-legal",
    ]
