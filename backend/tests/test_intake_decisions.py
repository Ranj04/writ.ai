from __future__ import annotations

from datetime import UTC, datetime

import pytest
from dragback.authority.engine import IntentAuthority
from dragback.domain import (
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    DecisionMutation,
)
from dragback.fixtures import load_decision_v18, load_graph_fixture
from dragback.grants import GrantSigner
from dragback.graph.memory import MemoryGraphStore
from dragback.intake.decisions import (
    DecisionDraftError,
    build_workspace_proposal,
    select_seeded_supersession_root,
    select_supersession_target,
)
from dragback.llm.extractor import (
    DecisionExtractionCandidate,
    EvidenceSpan,
)

SOURCE = "Approved: exports must be admin-only, effective immediately."
DELIVERED_AT = datetime(2026, 7, 25, 4, 5, tzinfo=UTC)


def _baseline() -> Artifact:
    return Artifact(
        id="DEC-004",
        kind=ArtifactKind.DECISION,
        title="Exports are available",
        scopes={"export.authorization", "export.generation"},
        approval_status=ApprovalStatus.APPROVED,
        authority_role="approve_product",
        attributes={
            "requirements": {
                "export.authorization": {"audience": "all_users"},
                "export.generation": {"format": "csv"},
            }
        },
    )


def _candidate(
    *,
    scopes: set[str] | None = None,
    requirements: dict[str, object] | None = None,
) -> DecisionExtractionCandidate:
    affected = scopes or {"export.authorization"}
    return DecisionExtractionCandidate(
        mutation=DecisionMutation(
            decision=Artifact(
                id="DEC-018",
                kind=ArtifactKind.DECISION,
                title="Admin-only exports",
                text=SOURCE,
                scopes={"model.controlled.scope"},
                approval_status=ApprovalStatus.APPROVED,
                authority_role="model-controlled-role",
                confidence=0.93,
                attributes={
                    "requirements": requirements
                    or {"export.authorization": {"audience": "admin_only"}}
                },
            ),
            supersedes_id="MODEL-CONTROLLED-TARGET",
            affected_scopes=affected,
        ),
        evidence_spans=[
            EvidenceSpan(start=0, end=len(SOURCE), text=SOURCE)
        ],
    )


def test_build_workspace_proposal_overwrites_governance_and_sets_effective_at() -> None:
    proposal = build_workspace_proposal(
        candidate=_candidate(),
        raw_text=SOURCE,
        source_ref="slack://T1/C1/1721880300.000001",
        author_user_id="U-APPROVER",
        authority_user_id="hex-user-compliance",
        delivered_at=DELIVERED_AT,
        superseded_decision=_baseline(),
        authority_permission_id="approve_compliance",
    )

    assert proposal.supersedes_id == "DEC-004"
    assert proposal.affected_scopes == {"export.authorization"}
    assert proposal.decision.scopes == proposal.affected_scopes
    assert proposal.decision.approval_status is ApprovalStatus.PROPOSAL
    assert proposal.decision.authority_role == "approve_compliance"
    assert proposal.decision.effective_at == DELIVERED_AT
    assert proposal.decision.invalidated_scopes == set()
    assert proposal.decision.attributes["extraction"] == {
        "source": "composio-slack",
        "author_user_id": "U-APPROVER",
        "authority_user_id": "hex-user-compliance",
        "delivered_at": DELIVERED_AT.isoformat(),
        "extraction_confidence": 0.93,
        "human_reviewed": False,
        "review_required": True,
        "validated_evidence_spans": [
            {
                "source_ref": "slack://T1/C1/1721880300.000001",
                "start": 0,
                "end": len(SOURCE),
                "text": SOURCE,
            }
        ],
    }


def test_build_workspace_proposal_rejects_scope_outside_baseline() -> None:
    with pytest.raises(DecisionDraftError, match="absent"):
        build_workspace_proposal(
            candidate=_candidate(
                scopes={"new.scope"},
                requirements={"new.scope": {"mode": "blocked"}},
            ),
            raw_text=SOURCE,
            source_ref="slack://T1/C1/1",
            author_user_id="U1",
            authority_user_id="hex-user-1",
            delivered_at=DELIVERED_AT,
            superseded_decision=_baseline(),
            authority_permission_id="approve_compliance",
        )


def test_build_workspace_proposal_rejects_non_exact_requirement_keys() -> None:
    with pytest.raises(DecisionDraftError, match="exactly match"):
        build_workspace_proposal(
            candidate=_candidate(
                requirements={"export.generation": {"format": "csv"}},
            ),
            raw_text=SOURCE,
            source_ref="slack://T1/C1/1",
            author_user_id="U1",
            authority_user_id="hex-user-1",
            delivered_at=DELIVERED_AT,
            superseded_decision=_baseline(),
            authority_permission_id="approve_compliance",
        )


def test_build_workspace_proposal_rejects_empty_requirement_object() -> None:
    with pytest.raises(DecisionDraftError, match="non-empty object"):
        build_workspace_proposal(
            candidate=_candidate(
                requirements={"export.authorization": {}},
            ),
            raw_text=SOURCE,
            source_ref="slack://T1/C1/1",
            author_user_id="U1",
            authority_user_id="hex-user-1",
            delivered_at=DELIVERED_AT,
            superseded_decision=_baseline(),
            authority_permission_id="approve_compliance",
        )


def test_build_workspace_proposal_rejects_naive_delivery_timestamp() -> None:
    with pytest.raises(DecisionDraftError, match="timezone-aware"):
        build_workspace_proposal(
            candidate=_candidate(),
            raw_text=SOURCE,
            source_ref="slack://T1/C1/1",
            author_user_id="U1",
            authority_user_id="hex-user-1",
            delivered_at=datetime(2026, 7, 25, 4, 5),
            superseded_decision=_baseline(),
            authority_permission_id="approve_compliance",
        )


def test_supersession_target_is_current_approved_decision() -> None:
    baseline = _baseline()
    baseline.invalidated_scopes = {"export.authorization"}
    current = _baseline().model_copy(
        update={
            "id": "DEC-018",
            "scopes": {"export.authorization"},
            "effective_at": DELIVERED_AT,
        }
    )

    selected = select_supersession_target(
        decisions=[baseline, current],
        affected_scopes={"export.authorization"},
    )

    assert selected.id == "DEC-018"


def test_second_change_uses_seeded_root_and_preserves_graph_invalidation() -> None:
    version, artifacts, edges, _ = load_graph_fixture()
    graph = MemoryGraphStore()
    graph.reset(version=version, artifacts=artifacts, edges=edges)
    authority = IntentAuthority(
        graph=graph,
        signer=GrantSigner("test-grant-secret", ttl_seconds=60),
        authority_threshold=0.75,
    )
    first = load_decision_v18().model_copy(deep=True)
    first_result = authority.apply_decision_change(first)
    assert first_result.applied is True

    root = select_seeded_supersession_root(
        decisions=[
            artifact
            for artifact in graph.list_artifacts()
            if artifact.kind is ArtifactKind.DECISION
        ],
        affected_scopes={"export.authorization"},
        root_decision_id="DEC-004",
    )
    assert root.id == "DEC-004"
    assert root.invalidated_scopes == {"export.authorization"}

    second = first.model_copy(deep=True)
    second.decision.id = "DEC-019"
    second.decision.title = "Exports require a security review"
    second.decision.effective_at = datetime(2026, 7, 26, tzinfo=UTC)
    second.decision.attributes["requirements"] = {
        "export.authorization": {"audience": "security_reviewed"}
    }
    second.supersedes_id = root.id

    second_result = authority.apply_decision_change(second)

    assert second_result.applied is True
    assert second_result.report is not None
    assert second_result.report.invalidated_task_ids == ["TASK-102"]
    assert second_result.report.preserved_task_ids == ["TASK-101"]
    task_path = next(
        path
        for path in second_result.report.paths
        if path.artifact_id == "TASK-102"
    )
    assert task_path.node_ids == [
        "DEC-019",
        "DEC-004",
        "SPEC-009",
        "TICKET-100",
        "TASK-102",
    ]
    assignments_by_task = {
        "TASK-101": "ASSIGNMENT-TASK-101",
        "TASK-102": "ASSIGNMENT-TASK-102",
    }
    assert tuple(
        assignments_by_task[task_id]
        for task_id in second_result.report.invalidated_task_ids
    ) == ("ASSIGNMENT-TASK-102",)
    assert tuple(
        assignments_by_task[task_id]
        for task_id in second_result.report.preserved_task_ids
    ) == ("ASSIGNMENT-TASK-101",)
    assert authority.current_requirements()["export.authorization"] == {
        "audience": "security_reviewed"
    }
