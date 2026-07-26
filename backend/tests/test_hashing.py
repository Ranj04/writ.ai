from __future__ import annotations

from datetime import UTC, datetime

from dragback.domain import ApprovalStatus, Artifact, ArtifactKind
from dragback.hashing import stable_hash


def _decision(scopes: set[str]) -> Artifact:
    return Artifact(
        id="DEC-HASH",
        kind=ArtifactKind.DECISION,
        title="Canonical hash",
        scopes=scopes,
        approval_status=ApprovalStatus.PROPOSAL,
        authority_role="approve_hash",
        effective_at=datetime(2026, 7, 25, tzinfo=UTC),
        attributes={
            "requirements": {
                "scope.a": {"mode": "one"},
                "scope.b": {"mode": "two"},
            }
        },
    )


def test_stable_hash_canonicalizes_sets_and_json_round_trips() -> None:
    first = _decision({"scope.a", "scope.b"})
    reconstructed = Artifact.model_validate(
        first.model_dump(mode="json")
    )
    reversed_input = _decision(set(reversed(["scope.a", "scope.b"])))

    assert stable_hash(first) == stable_hash(reconstructed)
    assert stable_hash(first) == stable_hash(reversed_input)


def test_stable_hash_preserves_ordered_list_semantics() -> None:
    assert stable_hash({"steps": ["one", "two"]}) != stable_hash(
        {"steps": ["two", "one"]}
    )
