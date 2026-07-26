from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from dragback.domain import ApprovalStatus, ArtifactKind
from dragback.fixtures import load_decision_v18, load_graph_fixture
from dragback.intake.approval import (
    ApprovalChannel,
    ApprovalCoordinator,
    ApprovalDisposition,
    ApprovalEvidence,
    PendingApproval,
    pending_from_workspace,
)
from dragback.intake.decisions import build_workspace_proposal
from dragback.llm import (
    GeminiDecisionExtractor,
    GeminiExtractionError,
    evidence_span_error,
)
from dragback.workspaces.authority_contexts import (
    DynamicAuthorityContextCreateRequest,
    DynamicAuthorityContextRegistry,
    DynamicMutationApprovalRequest,
)
from dragback.workspaces.models import WorkspaceApprovalRequest

SOURCE_TEXT = "Compliance approved this change: exports must be admin-only for every account."
QUOTE = "exports must be admin-only"
_UNCHANGED = object()
_MISSING = object()


def _candidate_json(
    *,
    quote: str = QUOTE,
    requirements: object = _UNCHANGED,
    decision_scopes: object = _UNCHANGED,
) -> str:
    mutation = json.loads(load_decision_v18().model_dump_json())
    attributes = mutation["decision"]["attributes"]
    if requirements is _MISSING:
        attributes.pop("requirements")
    elif requirements is not _UNCHANGED:
        attributes["requirements"] = requirements
    if decision_scopes is not _UNCHANGED:
        mutation["decision"]["scopes"] = decision_scopes
    return json.dumps(
        {
            "mutation": mutation,
            "evidence_spans": [{"start": 0, "end": 0, "text": quote}],
        }
    )


def _response(payload_text: str) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json={
            "candidates": [
                {"content": {"parts": [{"text": payload_text}]}}
            ]
        },
        request=httpx.Request(
            "POST",
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent",
        ),
    )


def test_prompt_requests_quotes_and_mandatory_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []

    def fake_post(url, *, headers, json, timeout):  # noqa: A002
        prompts.append(json["contents"][0]["parts"][0]["text"])
        return _response(_candidate_json())

    monkeypatch.setattr(httpx, "post", fake_post)
    GeminiDecisionExtractor(api_key="test-key").extract(
        SOURCE_TEXT,
        scope_vocabulary={"export.generation", "export.authorization"},
    )

    assert "Do not calculate character offsets" in prompts[0]
    assert "`mutation.decision.attributes.requirements` is mandatory" in prompts[0]
    assert "exactly one key for every value in `mutation.affected_scopes`" in prompts[0]
    assert (
        '["export.authorization", "export.generation"]'
        in prompts[0]
    )
    assert "Never invent, rename, or translate a scope identifier" in prompts[0]


def test_repairs_offsets_from_an_exact_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: _response(_candidate_json()),
    )

    candidate = GeminiDecisionExtractor(api_key="test-key").extract(SOURCE_TEXT)

    assert evidence_span_error(SOURCE_TEXT, candidate.evidence_spans) is None
    assert candidate.evidence_spans[0].start == SOURCE_TEXT.index(QUOTE)
    assert candidate.evidence_spans[0].end == SOURCE_TEXT.index(QUOTE) + len(QUOTE)


def test_retries_a_fabricated_quote_then_accepts_exact_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            _candidate_json(quote="legal signed off on this"),
            _candidate_json(),
        ]
    )
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: _response(next(responses)))

    candidate = GeminiDecisionExtractor(api_key="test-key").extract(SOURCE_TEXT)

    assert evidence_span_error(SOURCE_TEXT, candidate.evidence_spans) is None


def test_null_requirements_are_retried_then_valid_requirements_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []
    responses = iter(
        [
            _candidate_json(requirements=None),
            _candidate_json(),
        ]
    )

    def fake_post(url, *, headers, json, timeout):  # noqa: A002
        prompts.append(json["contents"][0]["parts"][0]["text"])
        return _response(next(responses))

    monkeypatch.setattr(httpx, "post", fake_post)

    candidate = GeminiDecisionExtractor(
        api_key="test-key",
        max_attempts=2,
    ).extract(SOURCE_TEXT)

    assert candidate.mutation.decision.attributes["requirements"] == {
        "export.authorization": {"audience": "admin_only"}
    }
    assert len(prompts) == 2
    assert "must be a non-empty object" in prompts[1]


def test_persistent_null_requirements_fail_after_configured_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_post(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _response(_candidate_json(requirements=None))

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(
        GeminiExtractionError,
        match=r"requirements.*must be a non-empty object",
    ):
        GeminiDecisionExtractor(
            api_key="test-key",
            max_attempts=2,
        ).extract(SOURCE_TEXT)

    assert calls == 2


def test_missing_requirements_fail_deterministic_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: _response(
            _candidate_json(requirements=_MISSING)
        ),
    )

    with pytest.raises(
        GeminiExtractionError,
        match=r"requirements.*must be a non-empty object",
    ):
        GeminiDecisionExtractor(
            api_key="test-key",
            max_attempts=1,
        ).extract(SOURCE_TEXT)


def test_mismatched_decision_scopes_are_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []
    responses = iter(
        [
            _candidate_json(decision_scopes=["export.generation"]),
            _candidate_json(),
        ]
    )

    def fake_post(url, *, headers, json, timeout):  # noqa: A002
        prompts.append(json["contents"][0]["parts"][0]["text"])
        return _response(next(responses))

    monkeypatch.setattr(httpx, "post", fake_post)

    candidate = GeminiDecisionExtractor(api_key="test-key").extract(SOURCE_TEXT)

    assert candidate.mutation.decision.scopes == {"export.authorization"}
    assert len(prompts) == 2
    assert "must exactly match" in prompts[1]


def test_valid_gemini_candidate_reaches_human_approved_graph_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: _response(_candidate_json()),
    )
    candidate = GeminiDecisionExtractor(api_key="test-key").extract(SOURCE_TEXT)

    version, artifacts, edges, _ = load_graph_fixture()
    baseline = next(artifact for artifact in artifacts if artifact.id == "DEC-004")
    baseline.approval_status = ApprovalStatus.PROPOSAL
    baseline.authority_role = "approve_compliance"
    registry = DynamicAuthorityContextRegistry(
        grant_secret="gemini-composed-test-secret",
        grant_ttl_seconds=300,
        authority_threshold=0.75,
    )
    registry.create(
        DynamicAuthorityContextCreateRequest(
            context_id="live-csv-exports",
            version=version,
            artifacts=artifacts,
            edges=edges,
            authority_policy={
                scope: {"approve_compliance"} for scope in baseline.scopes
            },
            baseline_decision_id=baseline.id,
        )
    )
    approved_baseline = registry.approve_baseline(
        "live-csv-exports",
        WorkspaceApprovalRequest(actor_role="approve_compliance"),
    )
    superseded = next(
        artifact
        for artifact in approved_baseline.artifacts
        if artifact.id == baseline.id
    )
    delivered_at = datetime(2026, 7, 25, 4, 5, tzinfo=UTC)
    proposal = build_workspace_proposal(
        candidate=candidate,
        raw_text=SOURCE_TEXT,
        source_ref="slack://T1/C1/1784952300.000001",
        author_user_id="U-COMPLIANCE",
        authority_user_id="hex-user-compliance",
        delivered_at=delivered_at,
        superseded_decision=superseded,
        authority_permission_id="approve_compliance",
    )
    mutation = proposal.mutation()
    pending = pending_from_workspace(
        {
            "id": "csv-exports",
            "pending_mutation": mutation.model_dump(mode="json"),
            "pending_proposal_instance_id": "csv-exports:proposal:1",
        }
    )
    assert pending is not None

    mutation_results = []

    class PermissionChecker:
        def has_permission(self, *, user_id: str, permission_id: str) -> bool:
            return (
                user_id == "hex-user-compliance"
                and permission_id == "approve_compliance"
            )

    class WorkspacePort:
        def approve_decision(
            self,
            *,
            pending: PendingApproval,
            evidence: ApprovalEvidence,
        ) -> dict[str, object]:
            result = registry.approve_mutation(
                "live-csv-exports",
                DynamicMutationApprovalRequest(
                    mutation=mutation,
                    actor_role=pending.permission_id,
                    proposal_fingerprint=pending.proposal_fingerprint,
                    approval_evidence=evidence,
                ),
            )
            mutation_results.append(result)
            return {
                "id": pending.workspace_id,
                "graph_version": result.graph_version,
            }

    approval = ApprovalCoordinator(
        permission_checker=PermissionChecker(),
        workspace_port=WorkspacePort(),
        clock=lambda: datetime(2026, 7, 25, 5, 0, tzinfo=UTC),
    ).approve(
        pending=pending,
        approver_user_id="hex-user-compliance",
        channel=ApprovalChannel.SLACK_REACTION,
        evidence_ref="slack://T1/C1/1784952300.000001#reaction-approved",
    )

    assert approval.disposition is ApprovalDisposition.APPROVED
    assert mutation_results[0].applied is True
    assert mutation_results[0].graph_version == "graph-v18"
    state = registry.state("live-csv-exports")
    stored = next(
        artifact for artifact in state.artifacts if artifact.id == "DEC-018"
    )
    assert stored.kind is ArtifactKind.DECISION
    assert stored.approval_status is ApprovalStatus.APPROVED
    assert stored.attributes["requirements"] == {
        "export.authorization": {"audience": "admin_only"}
    }
    assert stored.attributes["extraction"]["human_reviewed"] is True
