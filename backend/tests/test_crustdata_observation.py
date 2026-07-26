from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from writai.cli import run
from writai.intake.approval import ApprovalChannel, ApprovalEvidence
from writai.intake.crustdata import (
    CrustDataCapturedProvenance,
    CrustDataFixtureProvenance,
    CrustDataIdentityMappingError,
    CrustDataPersonIdentityBindings,
    CrustDataPersonObservationService,
    CrustDataPersonWebhookPayload,
    CrustDataReplayRequest,
    FileCrustDataCaptureStore,
)
from writai.intake.replay import (
    CrustDataDeliveryKey,
    JsonCrustDataDeliveryReplayStore,
)
from writai.services import agent_api
from writai.workspaces.models import LiveWorkspaceImportRequest, LiveWorkspaceRecord
from writai.workspaces.orchestrator import LiveWorkspaceOrchestrator
from writai.workspaces.repository import JsonFileLiveWorkspaceRepository
from writai.workspaces.supervisor import FixtureSupervisorRuntime

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = (
    REPO_ROOT
    / "fixtures"
    / "crustdata_person_role_change_documentation_reconstructed.json"
)
SOURCE_LABEL = (
    "documentation-reconstructed payload, replayed (not captured from CrustData)"
)
CAPTURED_SOURCE_LABEL = (
    "configured CrustData callback payload, replayed from server capture "
    "(not live; no vendor signature verified)"
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


def _identity_bindings(
    *,
    hexclave_user_id: str = "hex-user-001",
) -> CrustDataPersonIdentityBindings:
    return CrustDataPersonIdentityBindings.model_validate(
        {
            "schema_version": 1,
            "people": [
                {
                    "crustdata_person_id": 6324687,
                    "hexclave_user_id": hexclave_user_id,
                    "evidence_ref": "provisioning://crustdata/person/6324687",
                }
            ],
        }
    )


def _service(
    path: Path,
    *,
    identity_bindings: CrustDataPersonIdentityBindings | None = None,
) -> CrustDataPersonObservationService:
    return CrustDataPersonObservationService(
        replay_store=JsonCrustDataDeliveryReplayStore(path),
        expected_api_version="2025-11-01",
        identity_bindings=identity_bindings,
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


def _captured_fixture() -> CrustDataReplayRequest:
    raw = _fixture().model_dump(mode="json", by_alias=True)
    raw["fixture_provenance"] = {
        "kind": "captured",
        "label": CAPTURED_SOURCE_LABEL,
        "received_by_configured_callback": True,
        "callback_authentication": "configured-shared-bearer",
        "vendor_signature_verified": False,
        "captured_at": "2026-07-25T18:00:00Z",
        "capture_evidence_ref": "test://crustdata/capture/ntf-64200",
        "notice": (
            "Test-only captured-provenance shape. Production must reference the "
            "actual sanitized watcher delivery."
        ),
    }
    return CrustDataReplayRequest.model_validate(raw)


def test_documentation_reconstructed_fixture_is_explicitly_not_a_capture() -> None:
    request = _fixture()

    assert isinstance(
        request.fixture_provenance,
        CrustDataFixtureProvenance,
    )
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


def test_captured_payload_uses_server_owned_identity_binding(
    tmp_path: Path,
) -> None:
    result = _service(
        tmp_path / "deliveries.json",
        identity_bindings=_identity_bindings(),
    ).process(
        _captured_fixture(),
        approval_evidence=(_approval(person_id="hex-user-001"),),
    )

    assert result.source_label == CAPTURED_SOURCE_LABEL
    assert isinstance(
        result.fixture_provenance,
        CrustDataCapturedProvenance,
    )
    assert result.fixture_provenance.received_by_configured_callback is True
    assert result.fixture_provenance.vendor_signature_verified is False
    assert result.human_review_required is True
    assert [item.decision_id for item in result.flags] == ["DEC-ALPHA"]
    assert result.flags[0].person_id == "6324687"
    assert result.flags[0].identity_binding_evidence_ref == (
        "provisioning://crustdata/person/6324687"
    )
    assert "not live" in result.flags[0].source_label


def test_unmapped_captured_person_fails_closed_and_can_be_retried(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deliveries.json"

    with pytest.raises(
        CrustDataIdentityMappingError,
        match="require server-owned identity bindings",
    ):
        _service(path).process(
            _captured_fixture(),
            approval_evidence=(_approval(person_id="hex-user-001"),),
        )

    retry = _service(
        path,
        identity_bindings=_identity_bindings(),
    ).process(
        _captured_fixture(),
        approval_evidence=(_approval(person_id="hex-user-001"),),
    )

    assert retry.duplicate is False
    assert retry.human_review_required is True
    assert len(retry.flags) == 1


@pytest.mark.parametrize(
    "people",
    [
        [
            {
                "crustdata_person_id": 6324687,
                "hexclave_user_id": "hex-user-001",
                "evidence_ref": "provisioning://one",
            },
            {
                "crustdata_person_id": 6324687,
                "hexclave_user_id": "hex-user-002",
                "evidence_ref": "provisioning://two",
            },
        ],
        [
            {
                "crustdata_person_id": 6324687,
                "hexclave_user_id": "hex-user-001",
                "evidence_ref": "provisioning://one",
            },
            {
                "crustdata_person_id": 6324688,
                "hexclave_user_id": "hex-user-001",
                "evidence_ref": "provisioning://two",
            },
        ],
    ],
)
def test_identity_bindings_reject_ambiguous_mappings(
    people: list[dict[str, object]],
) -> None:
    with pytest.raises(ValueError, match="only one"):
        CrustDataPersonIdentityBindings.model_validate(
            {"schema_version": 1, "people": people}
        )


def test_identity_bindings_reject_malformed_json() -> None:
    with pytest.raises(
        CrustDataIdentityMappingError,
        match="identity bindings are invalid",
    ):
        CrustDataPersonIdentityBindings.from_json("{")


def test_capture_store_persists_owner_only_replay_and_deduplicates(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "captures"
    store = FileCrustDataCaptureStore(directory)

    first = store.capture(_fixture().payload)
    duplicate = store.capture(_fixture().payload)

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert duplicate.capture_id == first.capture_id
    path = directory / first.capture_file_name
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    captured = CrustDataReplayRequest.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    assert isinstance(
        captured.fixture_provenance,
        CrustDataCapturedProvenance,
    )
    assert captured.fixture_provenance.received_by_configured_callback is True
    assert captured.fixture_provenance.vendor_signature_verified is False
    assert captured.fixture_provenance.label == CAPTURED_SOURCE_LABEL


def test_capture_store_discards_unrequested_profile_fields(
    tmp_path: Path,
) -> None:
    raw = _fixture().payload.model_dump(mode="json", by_alias=True)
    profile = raw["results"][0]["record"]["basic_profile"]
    profile["personal_email"] = "private@example.test"
    raw["results"][0]["record"]["all_emails"] = ["private@example.test"]
    payload = CrustDataPersonWebhookPayload.model_validate(raw)

    receipt = FileCrustDataCaptureStore(tmp_path).capture(payload)
    captured_text = (tmp_path / receipt.capture_file_name).read_text(
        encoding="utf-8"
    )

    assert "private@example.test" not in captured_text
    assert "personal_email" not in captured_text
    assert "all_emails" not in captured_text


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
    replay_bearer: str | None,
    capture_bearer: str | None = "callback-secret",
    identity_bindings: str | None = None,
) -> Path:
    workspace_path = tmp_path / "workspaces.json"
    monkeypatch.setattr(
        agent_api,
        "settings",
        replace(
            agent_api.settings,
            workspace_store=str(workspace_path),
            crustdata_api_version="2025-11-01",
            crustdata_webhook_bearer=capture_bearer,
            crustdata_replay_bearer=replay_bearer,
            crustdata_person_identity_bindings=identity_bindings,
            crustdata_capture_dir=str(tmp_path / "captures"),
        ),
    )
    monkeypatch.setattr(
        agent_api,
        "workspace_repository",
        _EmptyWorkspaceRepository(),
    )
    agent_api.crustdata_replay_stores.clear()
    agent_api.crustdata_capture_stores.clear()
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
        replay_bearer=configured_bearer,
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


@pytest.mark.parametrize(
    ("configured_bearer", "authorization", "status_code", "code"),
    [
        (
            "callback-secret",
            None,
            401,
            "CRUSTDATA_AUTHENTICATION_FAILED",
        ),
        (
            "callback-secret",
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
def test_capture_route_fails_closed_for_missing_wrong_or_unconfigured_bearer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured_bearer: str | None,
    authorization: str | None,
    status_code: int,
    code: str,
) -> None:
    _configure_agent_route(
        monkeypatch,
        tmp_path,
        replay_bearer="operator-secret",
        capture_bearer=configured_bearer,
    )
    headers = {"Authorization": authorization} if authorization else {}

    response = TestClient(agent_api.app).post(
        "/intake/crustdata/person/capture",
        json=_fixture().payload.model_dump(mode="json", by_alias=True),
        headers=headers,
    )

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code
    assert not (tmp_path / "captures").exists()


@pytest.mark.parametrize(
    "path",
    [
        "/intake/crustdata/person/capture",
        "/intake/crustdata/person/replay",
    ],
)
def test_capture_and_replay_reject_a_shared_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path: str,
) -> None:
    store_path = _configure_agent_route(
        monkeypatch,
        tmp_path,
        replay_bearer="shared-secret",
        capture_bearer="shared-secret",
    )
    body = (
        _fixture().payload.model_dump(mode="json", by_alias=True)
        if path.endswith("/capture")
        else _fixture().model_dump(mode="json", by_alias=True)
    )

    response = TestClient(agent_api.app).post(
        path,
        json=body,
        headers={"Authorization": "Bearer shared-secret"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        "CRUSTDATA_AUTHENTICATION_CONFIGURATION_INVALID"
    )
    assert not store_path.exists()
    assert not (tmp_path / "captures").exists()


def test_replay_route_fails_closed_for_malformed_identity_bindings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store_path = _configure_agent_route(
        monkeypatch,
        tmp_path,
        replay_bearer="expected-secret",
        identity_bindings="{",
    )

    response = TestClient(agent_api.app).post(
        "/intake/crustdata/person/replay",
        json=_fixture().model_dump(mode="json", by_alias=True),
        headers={"Authorization": "Bearer expected-secret"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        "CRUSTDATA_IDENTITY_BINDINGS_INVALID"
    )
    assert not store_path.exists()


def test_capture_route_stores_raw_delivery_without_processing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_agent_route(
        monkeypatch,
        tmp_path,
        replay_bearer="expected-secret",
    )

    response = TestClient(agent_api.app).post(
        "/intake/crustdata/person/capture",
        json=_fixture().payload.model_dump(mode="json", by_alias=True),
        headers={"Authorization": "Bearer callback-secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source_label"] == (
        "configured CrustData callback payload, captured for replay "
        "(not processed live; no vendor signature verified)"
    )
    assert body["graph_mutated"] is False
    assert body["human_review_created"] is False
    capture_path = tmp_path / "captures" / body["capture_file_name"]
    assert capture_path.exists()
    assert capture_path.stat().st_mode & 0o777 == 0o600
    captured = CrustDataReplayRequest.model_validate_json(
        capture_path.read_text(encoding="utf-8")
    )
    assert captured.fixture_provenance.label == CAPTURED_SOURCE_LABEL


def test_capture_route_converts_an_invalid_capture_directory_to_503(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_agent_route(
        monkeypatch,
        tmp_path,
        replay_bearer="operator-secret",
    )
    (tmp_path / "captures").write_text("not a directory", encoding="utf-8")

    response = TestClient(agent_api.app).post(
        "/intake/crustdata/person/capture",
        json=_fixture().payload.model_dump(mode="json", by_alias=True),
        headers={"Authorization": "Bearer callback-secret"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CRUSTDATA_CAPTURE_UNAVAILABLE"


def test_forged_capture_is_rejected_before_genuine_capture_replays(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store_path = _configure_agent_route(
        monkeypatch,
        tmp_path,
        replay_bearer="operator-secret",
        identity_bindings=_identity_bindings().model_dump_json(),
    )
    repository = JsonFileLiveWorkspaceRepository(tmp_path / "workspaces.json")
    workspace_request = LiveWorkspaceImportRequest.model_validate(
        yaml.safe_load(
            (REPO_ROOT / "examples" / "writai-workspace.yaml").read_text(
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
        person_id="hex-user-001",
        workspace_id=workspace_request.id,
        decision_id=workspace_request.baseline_decision.id,
    )
    repository.save(record)
    monkeypatch.setattr(agent_api, "workspace_repository", repository)

    forged = TestClient(agent_api.app).post(
        "/intake/crustdata/person/replay",
        json=_captured_fixture().model_dump(mode="json", by_alias=True),
        headers={"Authorization": "Bearer operator-secret"},
    )

    assert forged.status_code == 422
    assert forged.json()["error"]["code"] == (
        "CRUSTDATA_CAPTURE_PROVENANCE_INVALID"
    )
    assert not store_path.exists()

    captured = TestClient(agent_api.app).post(
        "/intake/crustdata/person/capture",
        json=_fixture().payload.model_dump(mode="json", by_alias=True),
        headers={"Authorization": "Bearer callback-secret"},
    )
    capture_path = (
        tmp_path / "captures" / captured.json()["capture_file_name"]
    )
    genuine_body = json.loads(capture_path.read_text(encoding="utf-8"))
    replayed = TestClient(agent_api.app).post(
        "/intake/crustdata/person/replay",
        json=genuine_body,
        headers={"Authorization": "Bearer operator-secret"},
    )

    assert captured.status_code == 200
    assert replayed.status_code == 200
    assert replayed.json()["duplicate"] is False
    assert replayed.json()["source_label"] == CAPTURED_SOURCE_LABEL
    assert len(replayed.json()["flags"]) == 1
    assert replayed.json()["flags"][0]["identity_binding_evidence_ref"] == (
        "provisioning://crustdata/person/6324687"
    )
    assert store_path.exists()


def test_actual_api_response_and_event_are_labelled_replayed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_agent_route(
        monkeypatch,
        tmp_path,
        replay_bearer="expected-secret",
    )
    repository = JsonFileLiveWorkspaceRepository(tmp_path / "workspaces.json")
    workspace_request = LiveWorkspaceImportRequest.model_validate(
        yaml.safe_load(
            (REPO_ROOT / "examples" / "writai-workspace.yaml").read_text(
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
    serialized_event = json.dumps(events[0].envelope["data"], sort_keys=True)
    assert "Documentation Example Person" not in serialized_event
    assert "6324687" not in serialized_event
    assert workspace_request.id not in serialized_event
    assert workspace_request.baseline_decision.id not in serialized_event
    assert "approval://alpha" not in serialized_event


def test_malformed_payload_is_rejected_without_partial_replay_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store_path = _configure_agent_route(
        monkeypatch,
        tmp_path,
        replay_bearer="expected-secret",
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
        REPO_ROOT / "backend" / "writai" / "intake" / "crustdata.py",
        REPO_ROOT / "backend" / "writai" / "intake" / "replay.py",
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


def test_cli_duplicate_replay_names_retained_review_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    retained_flag = "crustdata-review-" + "a" * 64

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "source_label": SOURCE_LABEL,
                "duplicate": True,
                "flags": [],
                "existing_flag_ids": [retained_flag],
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
    assert "New review flags: 0" in output
    assert "Existing review flags retained: 1" in output
    assert retained_flag in output


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
