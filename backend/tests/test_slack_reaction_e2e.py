from __future__ import annotations

import hashlib
import hmac
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import dragback.services.agent_api as agent_api
import dragback.services.authority_api as authority_api
import httpx
import pytest
from dragback.auth.hexclave import HexclavePermissionError
from dragback.domain import (
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    DecisionMutation,
)
from dragback.hashing import stable_hash
from dragback.intake.approval import (
    ApprovalChannel,
    PendingApproval,
    actual_partition_from_workspace,
    pending_from_workspace,
)
from dragback.intake.slack import (
    SlackWebhookError,
    VerifiedSlackMessage,
    VerifiedSlackReaction,
)
from dragback.llm.extractor import FixtureDecisionExtractor
from dragback.notify.email import (
    ApprovalDeliveryProvider,
    ApprovalLinkSigner,
    ChannelApprovalAssertionSigner,
)
from dragback.notify.slack import (
    JsonSlackApprovalThreads,
    SlackDeliveryMode,
    SlackDeliveryReceipt,
    SlackThreadMessage,
)
from dragback.services.support import (
    INTERNAL_SERVICE_AUTH_HEADER,
    internal_service_token,
)
from dragback.workspaces.models import (
    SlackUserIdentityBinding,
    WorkspaceApprovalPreview,
    WorkspaceApprovalRequest,
    WorkspaceProposalRequest,
    WorkspaceSlackBinding,
)
from fastapi.testclient import TestClient
from pydantic import BaseModel

WORKSPACE_ID = "slack-e2e"
OTHER_WORKSPACE_ID = "slack-other"
SLACK_TEAM_ID = "T-E2E"
CONNECTION_USER_ID = "dragback-slack-e2e"
SOURCE_CHANNEL_ID = "C-E2E"
SOURCE_MESSAGE_TS = "1784952300.000001"
APPROVAL_MESSAGE_TS = "1784952301.000002"
SCOPE = "export.authorization"
PERMISSION_ID = "approve_compliance"
SIGNING_SECRET = b"test-composio-webhook-secret"
APPROVAL_ASSERTION_SECRET = (
    "slack-e2e-approval-assertion-secret-over-32-bytes"
)
APPROVAL_RECIPIENT_BINDINGS = (
    '{"schema_version":1,"recipients":[{'
    f'"workspace_id":"{WORKSPACE_ID}",'
    '"approver_user_id":"hex-email-only",'
    '"email":"email-only@example.com",'
    '"ntfy_topic":"dragback-7F3k9Q2mR8xP4vN6cL1s"}]}'
)


def _headers(body: bytes, event_id: str) -> dict[str, str]:
    return {
        "webhook-id": event_id,
        "x-test-signature": hmac.new(
            SIGNING_SECRET,
            body,
            hashlib.sha256,
        ).hexdigest(),
    }


def _message_body() -> bytes:
    return json.dumps(
        {
            "kind": "message",
            "connection_user_id": CONNECTION_USER_ID,
            "connected_account_id": "ca-e2e",
            "author_user_id": "U-AUTHOR",
            "team_id": SLACK_TEAM_ID,
            "channel_id": SOURCE_CHANNEL_ID,
            "message_ts": SOURCE_MESSAGE_TS,
            "text": "Approved: exports must be admin-only.",
        },
        sort_keys=True,
    ).encode()


def _reaction_body(
    *,
    user_id: str,
    message_ts: str = APPROVAL_MESSAGE_TS,
    team_id: str = SLACK_TEAM_ID,
    connection_user_id: str = CONNECTION_USER_ID,
    kind: str = "reaction-added",
) -> bytes:
    return json.dumps(
        {
            "kind": kind,
            "connection_user_id": connection_user_id,
            "reacting_user_id": user_id,
            "team_id": team_id,
            "channel_id": SOURCE_CHANNEL_ID,
            "message_ts": message_ts,
            "reaction": "white_check_mark",
            "event_ts": "1784952302.000003",
        },
        sort_keys=True,
    ).encode()


class SignedFixtureVerifier:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace

    def _payload(
        self,
        *,
        body: bytes,
        headers: Any,
        operation: str,
    ) -> tuple[dict[str, Any], str]:
        self.trace.append(f"verify-{operation}")
        expected = hmac.new(SIGNING_SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(
            str(headers.get("x-test-signature", "")),
            expected,
        ):
            raise SlackWebhookError("signature verification failed")
        event_id = str(headers.get("webhook-id", "")).strip()
        if not event_id:
            raise SlackWebhookError("missing webhook ID")
        return json.loads(body), event_id

    def parse_message(
        self,
        *,
        body: bytes,
        headers: Any,
    ) -> VerifiedSlackMessage:
        payload, event_id = self._payload(
            body=body,
            headers=headers,
            operation="message",
        )
        if payload.get("kind") != "message":
            raise SlackWebhookError("wrong trigger type")
        return VerifiedSlackMessage(
            event_id=event_id,
            connection_user_id=payload["connection_user_id"],
            author_user_id=payload["author_user_id"],
            team_id=payload["team_id"],
            channel_id=payload["channel_id"],
            message_ts=payload["message_ts"],
            delivered_at=datetime(2026, 7, 25, 4, 5, tzinfo=UTC),
            text=payload["text"],
            source_ref=(
                f"slack://{payload['team_id']}/{payload['channel_id']}/"
                f"{payload['message_ts']}"
            ),
        )

    def parse_reaction(
        self,
        *,
        body: bytes,
        headers: Any,
    ) -> VerifiedSlackReaction:
        payload, event_id = self._payload(
            body=body,
            headers=headers,
            operation="reaction",
        )
        if payload.get("kind") != "reaction-added":
            raise SlackWebhookError("wrong trigger type")
        return VerifiedSlackReaction(
            event_id=event_id,
            connection_user_id=payload["connection_user_id"],
            reacting_user_id=payload["reacting_user_id"],
            team_id=payload["team_id"],
            channel_id=payload["channel_id"],
            message_ts=payload["message_ts"],
            reaction=payload["reaction"],
            delivered_at=datetime(2026, 7, 25, 4, 5, 2, tzinfo=UTC),
            evidence_ref=(
                f"slack://{payload['team_id']}/{payload['channel_id']}/"
                f"{payload['message_ts']}#reaction-{payload['event_ts']}"
            ),
        )


class RecordingPermissionChecker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def has_permission(self, *, user_id: str, permission_id: str) -> bool:
        self.calls.append((user_id, permission_id))
        if user_id == "hex-unavailable":
            raise HexclavePermissionError("permission service unavailable")
        return user_id != "hex-denied"


class RecordingSender:
    delivery_mode = SlackDeliveryMode.LIVE

    def __init__(self) -> None:
        self.messages: list[SlackThreadMessage] = []

    def send_thread_message(
        self,
        message: SlackThreadMessage,
    ) -> SlackDeliveryReceipt:
        self.messages.append(message)
        return SlackDeliveryReceipt(
            thread=message.thread,
            message_id=APPROVAL_MESSAGE_TS,
            delivery_mode=SlackDeliveryMode.LIVE,
        )


class FakeWorkspaceView:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    @property
    def graph_version(self) -> str:
        return str(self._body["graph_version"])

    @property
    def pending_mutation(self) -> dict[str, Any] | None:
        value = self._body.get("pending_mutation")
        return value if isinstance(value, dict) else None

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        return deepcopy(self._body)


class FakeWorkspaceOrchestrator:
    def __init__(self, binding: WorkspaceSlackBinding) -> None:
        self.proposal_calls = 0
        self.mutation_calls = 0
        self.rejection_calls: list[dict[str, Any]] = []
        self.body: dict[str, Any] = {
            "id": binding.workspace_id,
            "status": "authorized",
            "graph_version": "graph-v17",
            "slack_binding": binding.model_dump(mode="json"),
            "authority_policy": {SCOPE: [PERMISSION_ID]},
            "pending_mutation": None,
            "pending_proposal_instance_id": None,
            "approved_mutations": [],
            "history": [],
            "supervisor": {
                "assignments": [
                    {"id": "ASSIGN-INVALIDATED", "state": "running"},
                    {"id": "ASSIGN-PRESERVED", "state": "running"},
                ]
            },
        }

    def get(self, workspace_id: str) -> FakeWorkspaceView:
        assert workspace_id == self.body["id"]
        return FakeWorkspaceView(self.body)

    def propose(self, proposal: WorkspaceProposalRequest) -> FakeWorkspaceView:
        self.proposal_calls += 1
        self.body["pending_mutation"] = proposal.mutation().model_dump(mode="json")
        self.body["pending_proposal_instance_id"] = (
            f"proposal-instance-{self.proposal_calls}"
        )
        self.body["history"].append(
            {
                "event_type": "decision.proposed",
                "data": {
                    "decision_id": proposal.decision.id,
                    "proposal_fingerprint": stable_hash(
                        proposal.mutation()
                    ),
                    "proposal_instance_id": (
                        self.body["pending_proposal_instance_id"]
                    ),
                    "permission_id": proposal.decision.authority_role,
                },
            }
        )
        self.body["status"] = "change-proposed"
        return FakeWorkspaceView(self.body)

    def record_approval_rejection(
        self,
        workspace_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert workspace_id == self.body["id"]
        self.rejection_calls.append(deepcopy(kwargs))
        return deepcopy(self.body)

    def preview_decision(
        self,
        workspace_id: str,
        decision_id: str,
    ) -> WorkspaceApprovalPreview:
        pending = pending_from_workspace(self.get(workspace_id).model_dump())
        assert pending is not None
        assert pending.decision_id == decision_id
        return WorkspaceApprovalPreview(
            pending=pending,
            interrupted_assignment_ids=("ASSIGN-INVALIDATED",),
            preserved_assignment_ids=("ASSIGN-PRESERVED",),
            interrupted_count=1,
            preserved_count=1,
            total_assignment_count=2,
            assignment_provenance_paths={
                "ASSIGN-INVALIDATED": (
                    pending.decision_id,
                    pending.supersedes_id,
                    "SPEC-1",
                    "TASK-INVALIDATED",
                )
            },
        )

    def approve_decision(
        self,
        workspace_id: str,
        decision_id: str,
        request: WorkspaceApprovalRequest,
    ) -> FakeWorkspaceView:
        assert workspace_id == self.body["id"]
        mutation = self.body["pending_mutation"]
        assert isinstance(mutation, dict)
        assert mutation["decision"]["id"] == decision_id
        assert request.proposal_instance_id == (
            self.body["pending_proposal_instance_id"]
        )
        assert request.approval_evidence is not None
        self.mutation_calls += 1
        self.body["approved_mutations"].append(
            {
                "mutation": deepcopy(mutation),
                "actor_role": request.actor_role,
                "approval_evidence": request.approval_evidence.model_dump(
                    mode="json"
                ),
            }
        )
        self.body["pending_mutation"] = None
        self.body["pending_proposal_instance_id"] = None
        self.body["status"] = "change-applied"
        self.body["graph_version"] = "graph-v18"
        self.body["supervisor"]["assignments"][0]["state"] = "interrupted"
        return FakeWorkspaceView(self.body)

    def replace_pending_without_reusing_confirmation(self) -> None:
        mutation = DecisionMutation.model_validate(self.body["pending_mutation"])
        mutation.decision.title = "Replacement proposal"
        mutation.decision.attributes["requirements"][SCOPE] = {
            "audience": "employees"
        }
        self.body["pending_mutation"] = mutation.model_dump(mode="json")
        self.body["pending_proposal_instance_id"] = "replacement-instance"


class Contexts:
    def __init__(
        self,
        *,
        binding: WorkspaceSlackBinding,
        baseline: Artifact,
        trace: list[str],
    ) -> None:
        self.bindings = {binding.workspace_id: binding}
        self.baseline = baseline
        self.trace = trace
        self.calls: list[str] = []

    def state(self, context_id: str) -> SimpleNamespace:
        self.trace.append("context")
        self.calls.append(context_id)
        workspace_id = context_id.removeprefix("live-")
        binding = self.bindings[workspace_id]
        return SimpleNamespace(
            slack_binding=binding,
            baseline_decision_id=self.baseline.id,
            authority_policy={SCOPE: {PERMISSION_ID}},
            artifacts=[self.baseline],
        )


@dataclass
class Harness:
    authority: TestClient
    agent: TestClient
    orchestrator: FakeWorkspaceOrchestrator
    contexts: Contexts
    trace: list[str]
    sender: RecordingSender
    authority_checker: RecordingPermissionChecker
    agent_checker: RecordingPermissionChecker
    internal_requests: list[dict[str, Any]]
    internal_headers: list[dict[str, str]]
    workspace_store: Path
    binding: WorkspaceSlackBinding


def _binding(workspace_id: str = WORKSPACE_ID) -> WorkspaceSlackBinding:
    return WorkspaceSlackBinding(
        workspace_id=workspace_id,
        slack_team_id=(
            SLACK_TEAM_ID if workspace_id == WORKSPACE_ID else "T-OTHER"
        ),
        composio_connection_user_id=(
            CONNECTION_USER_ID
            if workspace_id == WORKSPACE_ID
            else "dragback-slack-other"
        ),
        hexclave_team_id=f"hex-team-{workspace_id}",
        user_identities=(
            SlackUserIdentityBinding(
                slack_user_id="U-AUTHOR",
                hexclave_user_id="hex-author",
                evidence_ref="fixture://identity/author",
            ),
            SlackUserIdentityBinding(
                slack_user_id="U-APPROVER",
                hexclave_user_id="hex-approver",
                evidence_ref="fixture://identity/approver",
            ),
            SlackUserIdentityBinding(
                slack_user_id="U-DENIED",
                hexclave_user_id="hex-denied",
                evidence_ref="fixture://identity/denied",
            ),
            SlackUserIdentityBinding(
                slack_user_id="U-UNAVAILABLE",
                hexclave_user_id="hex-unavailable",
                evidence_ref="fixture://identity/unavailable",
            ),
        ),
    )


def _baseline() -> Artifact:
    return Artifact(
        id="DEC-BASE",
        kind=ArtifactKind.DECISION,
        title="Existing export policy",
        scopes={SCOPE},
        approval_status=ApprovalStatus.APPROVED,
        authority_role=PERMISSION_ID,
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        attributes={
            "requirements": {
                SCOPE: {"audience": "all_authenticated_users"}
            }
        },
    )


def _extractor() -> FixtureDecisionExtractor:
    return FixtureDecisionExtractor(
        DecisionMutation(
            decision=Artifact(
                id="DEC-SLACK-1",
                kind=ArtifactKind.DECISION,
                title="Admin-only exports",
                text="Approved: exports must be admin-only.",
                scopes={SCOPE},
                approval_status=ApprovalStatus.APPROVED,
                authority_role="model-output-is-not-authority",
                confidence=0.98,
                attributes={
                    "requirements": {
                        SCOPE: {"audience": "administrators"}
                    }
                },
            ),
            supersedes_id="model-supersession-is-not-trusted",
            affected_scopes={SCOPE},
        )
    )


def _httpx_response(response: Any) -> httpx.Response:
    return httpx.Response(
        status_code=response.status_code,
        content=response.content,
        headers=response.headers,
    )


def _install_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Harness:
    trace: list[str] = []
    binding = _binding()
    baseline = _baseline()
    contexts = Contexts(binding=binding, baseline=baseline, trace=trace)
    orchestrator = FakeWorkspaceOrchestrator(binding)
    sender = RecordingSender()
    authority_checker = RecordingPermissionChecker()
    agent_checker = RecordingPermissionChecker()
    internal_requests: list[dict[str, Any]] = []
    internal_headers: list[dict[str, str]] = []
    workspace_store = tmp_path / "live-workspaces.json"
    test_secret = "slack-e2e-internal-secret"

    monkeypatch.setattr(
        authority_api,
        "settings",
        replace(
            authority_api.settings,
            agent_url="http://agent.test",
            grant_secret=test_secret,
            approval_link_secret=APPROVAL_ASSERTION_SECRET,
            approval_recipient_bindings=APPROVAL_RECIPIENT_BINDINGS,
            approval_assertion_store=str(
                tmp_path / "approval-assertions.sqlite3"
            ),
            workspace_store=str(workspace_store),
        ),
    )
    monkeypatch.setattr(
        agent_api,
        "settings",
        replace(
            agent_api.settings,
            grant_secret=test_secret,
            approval_link_secret=APPROVAL_ASSERTION_SECRET,
            approval_recipient_bindings=APPROVAL_RECIPIENT_BINDINGS,
            approval_assertion_store=str(
                tmp_path / "approval-assertions.sqlite3"
            ),
            workspace_store=str(workspace_store),
        ),
    )
    authority_api.slack_approval_thread_stores.clear()
    authority_api.slack_delivery_replay_stores.clear()
    agent_api.approval_assertion_use_stores.clear()
    monkeypatch.setattr(
        authority_api,
        "_live_slack_verifier",
        lambda: SignedFixtureVerifier(trace),
    )
    monkeypatch.setattr(authority_api, "workspace_contexts", contexts)
    monkeypatch.setattr(
        authority_api,
        "build_decision_extractor",
        lambda _settings: _extractor(),
    )
    monkeypatch.setattr(
        authority_api,
        "_hexclave_permission_checker",
        lambda _team_id: authority_checker,
    )
    monkeypatch.setattr(
        authority_api,
        "_slack_notification_sender",
        lambda **_kwargs: sender,
    )
    monkeypatch.setattr(agent_api, "workspace_orchestrator", orchestrator)
    monkeypatch.setattr(
        agent_api,
        "_workspace_permission_checker",
        lambda _workspace: agent_checker,
    )

    def fake_post_model(**kwargs: Any) -> FakeWorkspaceView:
        proposal = kwargs["payload"]
        assert isinstance(proposal, WorkspaceProposalRequest)
        assert kwargs["url"].endswith(
            f"/live-workspaces/{WORKSPACE_ID}/decisions/propose"
        )
        return orchestrator.propose(proposal)

    monkeypatch.setattr(authority_api, "post_model", fake_post_model)
    agent = TestClient(agent_api.app)

    def fake_http_get(url: str, **_kwargs: Any) -> httpx.Response:
        response = agent.get(urlsplit(url).path)
        return _httpx_response(response)

    def fake_http_post(
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        **_kwargs: Any,
    ) -> httpx.Response:
        internal_requests.append(deepcopy(json))
        internal_headers.append(dict(headers))
        response = agent.post(
            urlsplit(url).path,
            json=json,
            headers=headers,
        )
        return _httpx_response(response)

    monkeypatch.setattr(authority_api.httpx, "get", fake_http_get)
    monkeypatch.setattr(authority_api.httpx, "post", fake_http_post)
    return Harness(
        authority=TestClient(authority_api.app),
        agent=agent,
        orchestrator=orchestrator,
        contexts=contexts,
        trace=trace,
        sender=sender,
        authority_checker=authority_checker,
        agent_checker=agent_checker,
        internal_requests=internal_requests,
        internal_headers=internal_headers,
        workspace_store=workspace_store,
        binding=binding,
    )


def _post_message(harness: Harness, event_id: str = "msg-delivery-1") -> Any:
    body = _message_body()
    return harness.authority.post(
        f"/intake/slack/{WORKSPACE_ID}",
        content=body,
        headers=_headers(body, event_id),
    )


def _post_reaction(
    harness: Harness,
    *,
    event_id: str,
    user_id: str,
    body: bytes | None = None,
) -> Any:
    raw = body or _reaction_body(user_id=user_id)
    return harness.authority.post(
        f"/intake/slack/{WORKSPACE_ID}/reaction",
        content=raw,
        headers=_headers(raw, event_id),
    )


def _notification_assertion(
    pending: PendingApproval,
    *,
    channel: ApprovalChannel,
) -> str:
    provider = (
        ApprovalDeliveryProvider.EMAIL
        if channel is ApprovalChannel.EMAIL
        else ApprovalDeliveryProvider.NTFY
    )
    recipient_ref = (
        "email-only@example.com"
        if channel is ApprovalChannel.EMAIL
        else "https://ntfy.sh/dragback-7F3k9Q2mR8xP4vN6cL1s"
    )
    signer = ApprovalLinkSigner(
        secret=APPROVAL_ASSERTION_SECRET,
        ttl_seconds=900,
    )
    token = signer.issue(
        pending=pending,
        approver_user_id="hex-email-only",
        channel=channel,
        delivery_provider=provider,
        recipient_ref=recipient_ref,
    )
    claims = signer.decode(token, expected_channel=channel)
    return (
        ChannelApprovalAssertionSigner(
            secret=APPROVAL_ASSERTION_SECRET
        )
        .issue_notification(claims)
        .get_secret_value()
    )


def test_signed_message_notification_and_reaction_apply_exactly_once_across_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = _install_harness(monkeypatch, tmp_path)

    message = _post_message(harness)

    assert message.status_code == 200
    message_result = message.json()
    assert message_result["status"] == "pending-human-confirmation"
    assert message_result["notification_status"] == "posted"
    assert message_result["approval_message_ts"] == APPROVAL_MESSAGE_TS
    assert message_result["would_interrupt_assignment_ids"] == [
        "ASSIGN-INVALIDATED"
    ]
    assert message_result["would_preserve_assignment_ids"] == [
        "ASSIGN-PRESERVED"
    ]
    assert harness.orchestrator.proposal_calls == 1
    assert harness.orchestrator.mutation_calls == 0
    assert len(harness.sender.messages) == 1
    authority_calls_after_message = list(harness.authority_checker.calls)
    authority_api.slack_delivery_replay_stores.clear()
    duplicate_message = _post_message(harness)
    assert duplicate_message.status_code == 200
    assert duplicate_message.json()["duplicate"] is True
    assert "token" not in duplicate_message.text.casefold()
    assert harness.orchestrator.proposal_calls == 1
    assert len(harness.sender.messages) == 1
    assert harness.authority_checker.calls == authority_calls_after_message

    thread_path = authority_api._workspace_store_sibling(
        "slack-approval-threads"
    )
    pending_before_restart = JsonSlackApprovalThreads(thread_path).resolve(
        team_id=SLACK_TEAM_ID,
        channel_id=SOURCE_CHANNEL_ID,
        message_ts=APPROVAL_MESSAGE_TS,
    )
    assert pending_before_restart is not None
    authority_api.slack_approval_thread_stores.clear()

    removed = _reaction_body(
        user_id="U-APPROVER",
        kind="reaction-removed",
    )
    context_calls_before_removed = len(harness.contexts.calls)
    removed_response = _post_reaction(
        harness,
        event_id="reaction-removed-1",
        user_id="U-APPROVER",
        body=removed,
    )
    assert removed_response.status_code == 401
    assert len(harness.contexts.calls) == context_calls_before_removed
    assert harness.internal_requests == []
    assert harness.orchestrator.mutation_calls == 0

    unmapped = _post_reaction(
        harness,
        event_id="reaction-unmapped-1",
        user_id="U-UNMAPPED",
    )
    assert unmapped.status_code == 200
    assert unmapped.json()["reason"] == "unmapped-slack-identity"
    assert harness.internal_requests == []
    assert harness.orchestrator.mutation_calls == 0

    denied = _post_reaction(
        harness,
        event_id="reaction-denied-1",
        user_id="U-DENIED",
    )
    assert denied.status_code == 200
    assert denied.json()["reason"] == "IGNORED_NOT_AUTHORIZED"
    assert harness.orchestrator.mutation_calls == 0
    assert harness.agent_checker.calls == [
        ("hex-denied", PERMISSION_ID)
    ]
    assert harness.orchestrator.rejection_calls[-1]["disposition"] == (
        "IGNORED_NOT_AUTHORIZED"
    )
    assert harness.orchestrator.rejection_calls[-1]["permission_id"] == (
        PERMISSION_ID
    )

    unavailable = _post_reaction(
        harness,
        event_id="reaction-unavailable-1",
        user_id="U-UNAVAILABLE",
    )
    assert unavailable.status_code == 200
    assert unavailable.json()["reason"] == "PERMISSION_CHECK_UNAVAILABLE"
    assert harness.orchestrator.mutation_calls == 0
    assert harness.orchestrator.rejection_calls[-1]["disposition"] == (
        "PERMISSION_CHECK_UNAVAILABLE"
    )

    approved = _post_reaction(
        harness,
        event_id="reaction-approved-1",
        user_id="U-APPROVER",
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["graph_mutated"] is True
    assert harness.orchestrator.mutation_calls == 1
    actual_partition = actual_partition_from_workspace(
        harness.orchestrator.body
    )
    assert list(actual_partition.interrupted_assignment_ids) == (
        message_result["would_interrupt_assignment_ids"]
    )
    assert list(actual_partition.preserved_assignment_ids) == (
        message_result["would_preserve_assignment_ids"]
    )
    assert harness.trace.index("verify-reaction") < harness.trace.index(
        "context",
        harness.trace.index("verify-reaction"),
    )

    approved_request = harness.internal_requests[-1]
    assert set(approved_request) == {"assertion"}
    approved_claims = ChannelApprovalAssertionSigner(
        secret=APPROVAL_ASSERTION_SECRET
    ).decode(
        approved_request["assertion"],
        expected_workspace_id=WORKSPACE_ID,
        expected_decision_id=pending_before_restart.decision_id,
    )
    assert approved_claims.approver_user_id == "hex-approver"
    assert approved_claims.channel is ApprovalChannel.SLACK_REACTION
    assert approved_claims.evidence_ref.endswith(
        f"{APPROVAL_MESSAGE_TS}#reaction-1784952302.000003"
    )
    assert "actor_role" not in approved_request
    assert "approval_token" not in approved_request
    assert harness.internal_headers[-1][INTERNAL_SERVICE_AUTH_HEADER] == (
        internal_service_token(agent_api.settings.grant_secret)
    )

    evidence = harness.orchestrator.body["approved_mutations"][0][
        "approval_evidence"
    ]
    assert evidence["approver_user_id"] == "hex-approver"
    assert evidence["channel"] == "slack-reaction"
    assert evidence["evidence_ref"].endswith(
        f"{APPROVAL_MESSAGE_TS}#reaction-1784952302.000003"
    )
    assert JsonSlackApprovalThreads(thread_path).resolve(
        team_id=SLACK_TEAM_ID,
        channel_id=SOURCE_CHANNEL_ID,
        message_ts=APPROVAL_MESSAGE_TS,
    ) is None

    internal_calls_after_approval = len(harness.internal_requests)
    authority_api.slack_delivery_replay_stores.clear()
    replay = _post_reaction(
        harness,
        event_id="reaction-approved-1",
        user_id="U-APPROVER",
    )
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert len(harness.internal_requests) == internal_calls_after_approval
    assert harness.orchestrator.mutation_calls == 1

    second_reaction = _post_reaction(
        harness,
        event_id="reaction-approved-2",
        user_id="U-APPROVER",
    )
    assert second_reaction.status_code == 200
    assert second_reaction.json()["status"] == "ignored"
    assert len(harness.internal_requests) == internal_calls_after_approval
    assert harness.orchestrator.mutation_calls == 1


def test_stale_thread_fingerprint_and_instance_never_mutate_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = _install_harness(monkeypatch, tmp_path)
    assert _post_message(harness).status_code == 200
    thread_path = authority_api._workspace_store_sibling(
        "slack-approval-threads"
    )
    original = JsonSlackApprovalThreads(thread_path).resolve(
        team_id=SLACK_TEAM_ID,
        channel_id=SOURCE_CHANNEL_ID,
        message_ts=APPROVAL_MESSAGE_TS,
    )
    assert original is not None

    harness.orchestrator.replace_pending_without_reusing_confirmation()
    stale = _post_reaction(
        harness,
        event_id="reaction-stale-1",
        user_id="U-APPROVER",
    )

    assert stale.status_code == 200
    assert stale.json()["status"] == "ignored"
    assert stale.json()["reason"] == "STALE_CONFIRMATION"
    assert harness.orchestrator.mutation_calls == 0
    assert harness.orchestrator.rejection_calls[-1]["disposition"] == (
        "STALE_CONFIRMATION"
    )
    assert harness.orchestrator.rejection_calls[-1]["permission_id"] == (
        PERMISSION_ID
    )
    assert JsonSlackApprovalThreads(thread_path).resolve(
        team_id=SLACK_TEAM_ID,
        channel_id=SOURCE_CHANNEL_ID,
        message_ts=APPROVAL_MESSAGE_TS,
    ) == original


@pytest.mark.parametrize(
    ("workspace_id", "team_id", "connection_user_id"),
    [
        (WORKSPACE_ID, "T-WRONG", CONNECTION_USER_ID),
        (WORKSPACE_ID, SLACK_TEAM_ID, "dragback-wrong-connection"),
        (OTHER_WORKSPACE_ID, SLACK_TEAM_ID, CONNECTION_USER_ID),
    ],
)
def test_signed_cross_tenant_reaction_is_rejected_before_thread_or_agent_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workspace_id: str,
    team_id: str,
    connection_user_id: str,
) -> None:
    harness = _install_harness(monkeypatch, tmp_path)
    harness.contexts.bindings[OTHER_WORKSPACE_ID] = _binding(
        OTHER_WORKSPACE_ID
    )
    monkeypatch.setattr(
        authority_api,
        "_slack_approval_threads",
        lambda: pytest.fail("binding mismatch must precede thread lookup"),
    )
    body = _reaction_body(
        user_id="U-APPROVER",
        team_id=team_id,
        connection_user_id=connection_user_id,
    )

    response = harness.authority.post(
        f"/intake/slack/{workspace_id}/reaction",
        content=body,
        headers=_headers(body, f"cross-tenant-{workspace_id}-{team_id}"),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == (
        "SLACK_WORKSPACE_BINDING_MISMATCH"
    )
    assert harness.trace[-2:] == ["verify-reaction", "context"]
    assert harness.internal_requests == []
    assert harness.orchestrator.mutation_calls == 0


def test_internal_slack_approval_requires_hmac_and_forbids_role_or_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = _install_harness(monkeypatch, tmp_path)
    assert _post_message(harness).status_code == 200
    pending = pending_from_workspace(harness.orchestrator.body)
    assert pending is not None
    raw_identity_payload = {
        "approver_user_id": "hex-approver",
        "evidence_ref": "slack://signed/reaction",
        "confirmed_proposal_fingerprint": pending.proposal_fingerprint,
        "confirmed_proposal_instance_id": pending.proposal_instance_id,
    }
    assertion = (
        ChannelApprovalAssertionSigner(
            secret=APPROVAL_ASSERTION_SECRET
        )
        .issue_slack(
            pending=pending,
            approver_user_id="hex-approver",
            evidence_ref="slack://signed/reaction",
        )
        .get_secret_value()
    )
    path = (
        f"/internal/live-workspaces/{WORKSPACE_ID}/decisions/"
        f"{pending.decision_id}/approve-from-slack"
    )

    unauthenticated = harness.agent.post(
        path,
        json={"assertion": assertion},
    )
    assert unauthenticated.status_code == 403
    assert harness.orchestrator.mutation_calls == 0

    forbidden = {
        **raw_identity_payload,
        "actor_role": PERMISSION_ID,
        "approval_token": "must-never-cross-this-boundary",
    }
    response = harness.agent.post(
        path,
        json=forbidden,
        headers={
            INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
                agent_api.settings.grant_secret
            )
        },
    )
    assert response.status_code == 422
    assert "must-never-cross-this-boundary" not in response.text
    raw_identity = harness.agent.post(
        path,
        json=raw_identity_payload,
        headers={
            INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
                agent_api.settings.grant_secret
            )
        },
    )
    assert raw_identity.status_code == 422
    assert harness.orchestrator.mutation_calls == 0


@pytest.mark.parametrize("channel", ["email", "push"])
def test_internal_notification_approval_reloads_binding_and_uses_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    channel: str,
) -> None:
    harness = _install_harness(monkeypatch, tmp_path)
    assert _post_message(harness).status_code == 200
    pending = pending_from_workspace(harness.orchestrator.body)
    assert pending is not None
    path = (
        f"/internal/live-workspaces/{WORKSPACE_ID}/decisions/"
        f"{pending.decision_id}/approve-from-notification"
    )
    assertion = _notification_assertion(
        pending,
        channel=ApprovalChannel(channel),
    )
    payload = {"assertion": assertion}

    unauthenticated = harness.agent.post(path, json=payload)
    assert unauthenticated.status_code == 403
    assert harness.orchestrator.mutation_calls == 0

    approved = harness.agent.post(
        path,
        json=payload,
        headers={
            INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
                agent_api.settings.grant_secret
            )
        },
    )
    replay = harness.agent.post(
        path,
        json=payload,
        headers={
            INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
                agent_api.settings.grant_secret
            )
        },
    )

    assert approved.status_code == 200
    assert approved.json()["disposition"] == "APPROVED"
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == (
        "APPROVAL_ASSERTION_ALREADY_USED"
    )
    assert harness.agent_checker.calls == [
        ("hex-email-only", PERMISSION_ID)
    ]
    assert harness.orchestrator.mutation_calls == 1
    evidence = harness.orchestrator.body["approved_mutations"][0][
        "approval_evidence"
    ]
    assert evidence["channel"] == channel
    assert evidence["approver_user_id"] == "hex-email-only"
    assert evidence["confirmed_proposal_fingerprint"] == (
        pending.proposal_fingerprint
    )
    assert evidence["confirmed_proposal_instance_id"] == (
        pending.proposal_instance_id
    )


def test_internal_notification_approval_rejects_stale_and_non_link_channels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = _install_harness(monkeypatch, tmp_path)
    assert _post_message(harness).status_code == 200
    pending = pending_from_workspace(harness.orchestrator.body)
    assert pending is not None
    path = (
        f"/internal/live-workspaces/{WORKSPACE_ID}/decisions/"
        f"{pending.decision_id}/approve-from-notification"
    )
    headers = {
        INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
            agent_api.settings.grant_secret
        )
    }
    stale_pending = pending.model_copy(
        update={"proposal_fingerprint": "sha256:" + ("b" * 64)}
    )
    stale = harness.agent.post(
        path,
        json={
            "assertion": _notification_assertion(
                stale_pending,
                channel=ApprovalChannel.EMAIL,
            )
        },
        headers=headers,
    )
    bypass = harness.agent.post(
        path,
        json={
            "approver_user_id": "hex-email-only",
            "channel": "cli",
            "evidence_ref": "cli://approval/bypass",
            "confirmed_proposal_fingerprint": pending.proposal_fingerprint,
            "confirmed_proposal_instance_id": pending.proposal_instance_id,
        },
        headers=headers,
    )
    forged = harness.agent.post(
        path,
        json={"assertion": "daf1.forged.signature"},
        headers=headers,
    )

    assert stale.status_code == 200
    assert stale.json()["disposition"] == "STALE_CONFIRMATION"
    assert bypass.status_code == 422
    assert forged.status_code == 401
    assert forged.json()["error"]["code"] == "INVALID_APPROVAL_ASSERTION"
    assert harness.agent_checker.calls == []
    assert harness.orchestrator.mutation_calls == 0


def test_public_authority_context_redacts_identity_bindings_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SensitiveContext(BaseModel):
        context_id: str
        graph_version: str
        slack_binding: dict[str, Any]
        approval_evidence: dict[str, Any]

    state = SensitiveContext(
        context_id=f"live-{WORKSPACE_ID}",
        graph_version="graph-v18",
        slack_binding={
            "workspace_id": WORKSPACE_ID,
            "user_identities": [
                {
                    "slack_user_id": "U-SECRET",
                    "hexclave_user_id": "hex-secret",
                }
            ],
        },
        approval_evidence={
            "DEC-SECRET": {
                "approver_user_id": "hex-secret",
                "evidence_ref": "slack://secret/evidence",
            }
        },
    )
    monkeypatch.setattr(
        authority_api,
        "workspace_contexts",
        SimpleNamespace(state=lambda _context_id: state),
    )
    client = TestClient(authority_api.app)

    public = client.get(
        f"/live-workspaces/authority/contexts/live-{WORKSPACE_ID}"
    )
    assert public.status_code == 200
    assert public.json()["slack_binding"] is None
    assert public.json()["approval_evidence"] == {}
    assert "hex-secret" not in public.text
    assert "slack://secret/evidence" not in public.text

    internal = client.get(
        f"/live-workspaces/authority/contexts/live-{WORKSPACE_ID}",
        headers={
            INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
                authority_api.settings.grant_secret
            )
        },
    )
    assert internal.status_code == 200
    assert internal.json()["slack_binding"]["user_identities"][0][
        "hexclave_user_id"
    ] == "hex-secret"
    assert internal.json()["approval_evidence"]["DEC-SECRET"][
        "evidence_ref"
    ] == "slack://secret/evidence"
