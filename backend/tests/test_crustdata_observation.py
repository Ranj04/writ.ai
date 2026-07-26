from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from dragback.cli import run
from dragback.intake.approval import ApprovalChannel, ApprovalEvidence
from dragback.intake.crustdata import (
    CrustDataPersonObservationService,
    CrustDataReplayRequest,
)
from dragback.intake.replay import (
    CrustDataDeliveryKey,
    JsonCrustDataDeliveryReplayStore,
)
from dragback.services import agent_api
from dragback.workspaces.models import LiveWorkspaceImportRequest, LiveWorkspaceRecord
from dragback.workspaces.orchestrator import LiveWorkspaceOrchestrator
from dragback.workspaces.repository import JsonFileLiveWorkspaceRepository
from dragback.workspaces.supervisor import FixtureSupervisorRuntime
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = (
    REPO_ROOT
    / "fixtures"
    / "crustdata_person_role_change_documentation_reconstructed.json"
)
SOURCE_LABEL = (
    "documentation-reconstructed payload, replayed (not captured from CrustData)"
)


def _fixture() -> CrustDataReplayRequest:
    return CrustDataReplayRequest.model_validate_json(
        FIXTURE_PATH.read_text(encoding="utf-8")
    )


def _approval(
    *,
    person_id: str = "6324687",
    workspace_id: str = "workspace-alpha",
    decision_id: str = "DEC-ALPHA",
    evidence_ref: str = "approval://alpha",
) -> ApprovalEvidence:
    return ApprovalEvidence(
        workspace_id=workspace_id,
        decision_id=decision_id,
        approver_user_id=person_id,
        permission_id="approve_compliance",
        channel=ApprovalChannel.CLI,
        evidence_ref=evidence_ref,
        approved_at=datetime(2026, 7, 15, 18, 30, tzinfo=UTC),
        confirmed_proposal_fingerprint="sha256:" + "a" * 64,
        confirmed_proposal_instance_id=f"proposal-{decision_id}",
    )


def _service(path: Path) -> CrustDataPersonObservationService:
    return CrustDataPersonObservationService(
        replay_store=JsonCrustDataDeliveryReplayStore(path),
        expected_api_version="2025-11-01",
    )


def _departure_fixture() -> CrustDataReplayRequest:
    raw = _fixture().model_dump(mode="json", by_alias=True)
    metadata = raw["payload"]["metadata"]
    metadata["run_id"] = 64201
    metadata["notification_id"] = "ntf_64201_documentation_reconstructed"
    result = raw["payload"]["results"][0]
    result["changes"] = [
        {
            "field": "experience.employment_details.current.name",
            "type": "changed",
            "from": "Example Holdings",
            "to": None,
        }
    ]
    result["record"]["basic_profile"]["current_title"] = None
    return CrustDataReplayRequest.model_validate(raw)


def test_documentation_reconstructed_fixture_is_explicitly_not_a_capture() -> None:
    request = _fixture()

    assert request.fixture_provenance.captured_from_crustdata is False
    assert request.fixture_provenance.kind == "documentation-reconstructed"
    assert request.fixture_provenance.label == SOURCE_LABEL
    assert "not a real CrustData webhook capture" in (
        request.fixture_provenance.notice
    )


def test_role_change_flags_exactly_the_decisions_that_person_approved(
    tmp_path: Path,
) -> None:
    approvals = (
        _approval(decision_id="DEC-ALPHA", evidence_ref="approval://alpha"),
        _approval(
            workspace_id="workspace-beta",
            decision_id="DEC-BETA",
            evidence_ref="approval://beta",
        ),
        _approval(
            person_id="different-person",
            workspace_id="workspace-other",
            decision_id="DEC-OTHER",
            evidence_ref="approval://other",
        ),
    )
    before = [item.model_dump(mode="json") for item in approvals]

    result = _service(tmp_path / "deliveries.json").process(
        _fixture(),
        approval_evidence=approvals,
    )

    assert result.source_label == SOURCE_LABEL
    assert result.graph_mutated is False
    assert result.human_review_required is True
    assert {(item.workspace_id, item.decision_id) for item in result.flags} == {
        ("workspace-alpha", "DEC-ALPHA"),
        ("workspace-beta", "DEC-BETA"),
    }
    assert {item.approval_evidence_ref for item in result.flags} == {
        "approval://alpha",
        "approval://beta",
    }
    assert all(item.change_kind == "role-change" for item in result.flags)
    assert all("Vice President, Finance" in item.explanation for item in result.flags)
    assert all("Chief Financial Officer" in item.explanation for item in result.flags)
    assert all("Human confirmation is required" in item.explanation for item in result.flags)
    assert before == [item.model_dump(mode="json") for item in approvals]


def test_departure_flags_exactly_the_decisions_that_person_approved(
    tmp_path: Path,
) -> None:
    result = _service(tmp_path / "deliveries.json").process(
        _departure_fixture(),
        approval_evidence=(
            _approval(decision_id="DEC-ALPHA"),
            _approval(person_id="different-person", decision_id="DEC-OTHER"),
        ),
    )

    assert [item.decision_id for item in result.flags] == ["DEC-ALPHA"]
    flag = result.flags[0]
    assert flag.change_kind == "departure"
    assert flag.changes[0].old_value == "Example Holdings"
    assert flag.changes[0].new_value is None
    assert "approval://alpha" in flag.explanation
    assert "2026-07-15T18:30:00+00:00" in flag.explanation


def test_person_with_no_approval_evidence_produces_no_flags(
    tmp_path: Path,
) -> None:
    result = _service(tmp_path / "deliveries.json").process(
        _fixture(),
        approval_evidence=(_approval(person_id="different-person"),),
    )

    assert result.flags == ()
    assert result.human_review_required is False
    assert result.graph_mutated is False


def test_delivery_replay_raises_flags_once_across_a_fresh_store_instance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deliveries.json"

    first = _service(path).process(
        _fixture(),
        approval_evidence=(_approval(),),
    )
    duplicate = _service(path).process(
        _fixture(),
        approval_evidence=(_approval(),),
    )

    assert len(first.flags) == 1
    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert duplicate.flags == ()
    assert duplicate.existing_flag_ids == (first.flags[0].flag_id,)
    assert path.stat().st_mode & 0o777 == 0o600


class _EmptyWorkspaceRepository:
    def list(self) -> list[LiveWorkspaceRecord]:
        return []


def _configure_agent_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    bearer: str | None,
) -> Path:
    workspace_path = tmp_path / "workspaces.json"
    monkeypatch.setattr(
        agent_api,
        "settings",
        replace(
            agent_api.settings,
            workspace_store=str(workspace_path),
            crustdata_api_version="2025-11-01",
            crustdata_webhook_bearer=bearer,
        ),
    )
    monkeypatch.setattr(
        agent_api,
        "workspace_repository",
        _EmptyWorkspaceRepository(),
    )
    agent_api.crustdata_replay_stores.clear()
    return tmp_path / "workspaces-crustdata-deliveries.json"


@pytest.mark.parametrize(
    ("configured_bearer", "authorization", "status_code", "code"),
    [
        (
            "expected-secret",
            None,
            401,
            "CRUSTDATA_AUTHENTICATION_FAILED",
        ),
        (
            "expected-secret",
            "Bearer wrong-secret",
            401,
            "CRUSTDATA_AUTHENTICATION_FAILED",
        ),
        (
            None,
            "Bearer any-secret",
            503,
            "CRUSTDATA_AUTHENTICATION_NOT_CONFIGURED",
        ),
    ],
)
def test_replay_route_fails_closed_for_missing_wrong_or_unconfigured_bearer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured_bearer: str | None,
    authorization: str | None,
    status_code: int,
    code: str,
) -> None:
    store_path = _configure_agent_route(
        monkeypatch,
        tmp_path,
        bearer=configured_bearer,
    )
    headers = {"Authorization": authorization} if authorization else {}

    response = TestClient(agent_api.app).post(
        "/intake/crustdata/person/replay",
        json=_fixture().model_dump(mode="json", by_alias=True),
        headers=headers,
    )

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code
    assert not store_path.exists()


def test_actual_api_response_and_event_are_labelled_replayed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_agent_route(
        monkeypatch,
        tmp_path,
        bearer="expected-secret",
    )
    repository = JsonFileLiveWorkspaceRepository(tmp_path / "workspaces.json")
    workspace_request = LiveWorkspaceImportRequest.model_validate(
        yaml.safe_load(
            (REPO_ROOT / "examples" / "dragback-workspace.yaml").read_text(
                encoding="utf-8"
            )
        )
    )
    LiveWorkspaceOrchestrator(
        repository=repository,
        supervisor_runtime=FixtureSupervisorRuntime(),
    ).import_workspace(workspace_request)
    record = repository.get(workspace_request.id)
    record.baseline_approval_evidence = _approval(
        workspace_id=workspace_request.id,
        decision_id=workspace_request.baseline_decision.id,
    )
    repository.save(record)
    monkeypatch.setattr(agent_api, "workspace_repository", repository)
    sequence = agent_api.event_broker.current_sequence

    response = TestClient(agent_api.app).post(
        "/intake/crustdata/person/replay",
        json=_fixture().model_dump(mode="json", by_alias=True),
        headers={"Authorization": "Bearer expected-secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source_label"] == SOURCE_LABEL
    assert body["source_label"] != "live"
    assert len(body["flags"]) == 1
    assert body["flags"][0]["decision_id"] == (
        workspace_request.baseline_decision.id
    )
    events = asyncio.run(
        agent_api.event_broker.wait_for_events(
            sequence,
            timeout_seconds=0.01,
        )
    )
    assert len(events) == 1
    assert events[0].envelope["event"] == "crustdata.person-review.flagged"
    assert events[0].envelope["data"]["source_label"] == SOURCE_LABEL
    assert events[0].envelope["data"]["source_label"] != "live"


def test_malformed_payload_is_rejected_without_partial_replay_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store_path = _configure_agent_route(
        monkeypatch,
        tmp_path,
        bearer="expected-secret",
    )
    malformed = _fixture().model_dump(mode="json", by_alias=True)
    malformed["payload"]["results"] = []

    response = TestClient(agent_api.app).post(
        "/intake/crustdata/person/replay",
        json=malformed,
        headers={"Authorization": "Bearer expected-secret"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert not store_path.exists()


def test_crustdata_path_has_no_invalidation_or_authority_mutation_call() -> None:
    paths = (
        REPO_ROOT / "backend" / "dragback" / "intake" / "crustdata.py",
        REPO_ROOT / "backend" / "dragback" / "intake" / "replay.py",
    )

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "ValidityStatus" not in source
        assert "invalidated_scopes" not in source
        assert "apply_decision_change" not in source


def test_cli_replay_output_and_request_preserve_replayed_label(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/intake/crustdata/person/replay"
        assert request.headers["authorization"] == "Bearer expected-secret"
        assert json.loads(request.content) == fixture
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "source_label": SOURCE_LABEL,
                "duplicate": False,
                "flags": [
                    {
                        "person_name": "Documentation Example Person",
                        "person_id": "6324687",
                        "change_kind": "role-change",
                        "workspace_id": "workspace-alpha",
                        "decision_id": "DEC-ALPHA",
                        "approval_evidence_ref": "approval://alpha",
                        "approved_at": "2026-07-15T18:30:00Z",
                        "explanation": "Human review is required; the graph was not changed.",
                    }
                ],
            },
        )

    exit_code = run(
        [
            "workspace",
            "replay-crustdata",
            str(FIXTURE_PATH),
            "--bearer",
            "expected-secret",
        ],
        transport=httpx.MockTransport(handler),
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert f"Source: {SOURCE_LABEL}" in output
    assert "New review flags: 1" in output
    assert "approval://alpha" in output
    assert "Source: live" not in output


def test_an_abandoned_reservation_is_retried_rather_than_losing_the_review(
    tmp_path: Path,
) -> None:
    """A crash between reserving and completing must not silently drop the flags.

    Raised by the cross-model review of the integration. The reservation is
    written before the flags are computed, so a process that dies in between
    left the delivery permanently `reserved` — and every retry answered
    "duplicate, zero flags", losing the human review that should have been
    raised. Losing a review flag is the one outcome this path exists to prevent.
    """

    store = JsonCrustDataDeliveryReplayStore(tmp_path / "deliveries.json")
    key = CrustDataDeliveryKey(watch_id=1, run_id=2, notification_id="ntf-abandoned")

    # First attempt reserves, then dies before `complete`.
    assert store.reserve(key) is True

    # A fresh process reading the same file must be able to try again.
    retry = JsonCrustDataDeliveryReplayStore(tmp_path / "deliveries.json")
    assert retry.reserve(key) is True

    retry.complete(key, result={"flags": ["crustdata-review-" + "0" * 64]})

    # Once it has genuinely completed, it is a duplicate forever after.
    assert retry.reserve(key) is False
    assert JsonCrustDataDeliveryReplayStore(tmp_path / "deliveries.json").reserve(key) is False
