from __future__ import annotations

import pytest
from writai.auth.hexclave import HexclavePermissionError
from writai.intake.gate import (
    DeterministicIntakeGate,
    IntakeGateDisposition,
    IntakeGateReason,
)


class StubPermissionChecker:
    def __init__(
        self,
        permissions: dict[tuple[str, str], bool],
        *,
        fail: bool = False,
    ) -> None:
        self.permissions = permissions
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def has_permission(self, *, user_id: str, permission_id: str) -> bool:
        self.calls.append((user_id, permission_id))
        if self.fail:
            raise HexclavePermissionError("permission service unavailable")
        return self.permissions.get((user_id, permission_id), False)


def make_gate(
    checker: StubPermissionChecker,
    *,
    authority_policy: dict[str, set[str]] | None = None,
) -> DeterministicIntakeGate:
    selected_policy = authority_policy or {
        "export.authorization": {"approve_compliance"},
        "export.generation": {"approve_engineering"},
        "billing.refunds": {"approve_finance"},
    }
    return DeterministicIntakeGate(
        known_scopes=set(selected_policy),
        authority_policy=selected_policy,
        permission_checker=checker,
        confidence_threshold=0.75,
    )


def test_authoritative_author_routes_to_pending_with_common_permission_id() -> None:
    checker = StubPermissionChecker(
        {("user-001", "approve_security"): True}
    )
    gate = make_gate(
        checker,
        authority_policy={
            "export.authorization": {"approve_security"},
            "export.generation": {"approve_security"},
        },
    )

    result = gate.evaluate(
        author_id="user-001",
        affected_scopes={"export.authorization", "export.generation"},
        confidence=0.75,
    )

    assert result.disposition is IntakeGateDisposition.PENDING
    assert result.eligible_for_confirmation is True
    assert result.permission_id == "approve_security"
    assert result.affected_scopes == frozenset(
        {"export.authorization", "export.generation"}
    )
    assert result.reasons == ()
    assert result.checked_permissions == ("approve_security",)
    assert not hasattr(result, "verdict")


def test_non_authoritative_author_is_parked_and_never_escalated() -> None:
    checker = StubPermissionChecker({})
    gate = make_gate(checker)

    result = gate.evaluate(
        author_id="user-002",
        affected_scopes={"export.authorization"},
        confidence=0.9,
    )

    assert result.disposition is IntakeGateDisposition.PARKED
    assert result.eligible_for_confirmation is False
    assert result.reasons == (IntakeGateReason.AUTHOR_NOT_AUTHORIZED,)
    assert result.permission_id is None
    assert checker.calls == [("user-002", "approve_compliance")]


def test_multiple_common_permissions_are_checked_in_stable_order() -> None:
    checker = StubPermissionChecker(
        {
            ("user-001", "approve_compliance"): False,
            ("user-001", "approve_security"): True,
        }
    )
    gate = make_gate(
        checker,
        authority_policy={
            "export.authorization": {
                "approve_compliance",
                "approve_security",
            }
        },
    )

    result = gate.evaluate(
        author_id="user-001",
        affected_scopes={"export.authorization"},
        confidence=0.9,
    )

    assert result.disposition is IntakeGateDisposition.PENDING
    assert result.permission_id == "approve_security"
    assert result.checked_permissions == (
        "approve_compliance",
        "approve_security",
    )
    assert checker.calls == [
        ("user-001", "approve_compliance"),
        ("user-001", "approve_security"),
    ]


@pytest.mark.parametrize(
    ("known_scopes", "authority_policy"),
    [
        (
            {"export.authorization"},
            {},
        ),
        (
            {"export.authorization"},
            {
                "export.authorization": {"approve_compliance"},
                "export.generation": {"approve_engineering"},
            },
        ),
        (
            {"export.authorization", "export.generation"},
            {"export.authorization": {"approve_compliance"}},
        ),
        (
            {"export.authorization"},
            {"export.authorization": set()},
        ),
    ],
)
def test_gate_configuration_requires_complete_nonempty_policy_coverage(
    known_scopes: set[str],
    authority_policy: dict[str, set[str]],
) -> None:
    checker = StubPermissionChecker({})

    with pytest.raises(ValueError):
        DeterministicIntakeGate(
            known_scopes=known_scopes,
            authority_policy=authority_policy,
            permission_checker=checker,
            confidence_threshold=0.75,
        )


def test_unknown_scope_and_low_confidence_park_without_network_calls() -> None:
    checker = StubPermissionChecker({})
    gate = make_gate(checker)

    result = gate.evaluate(
        author_id="user-001",
        affected_scopes={"unknown.scope"},
        confidence=0.5,
    )

    assert result.disposition is IntakeGateDisposition.PARKED
    assert result.reasons == (
        IntakeGateReason.UNKNOWN_SCOPES,
        IntakeGateReason.LOW_CONFIDENCE,
    )
    assert checker.calls == []


def test_scopes_without_one_shared_engine_permission_are_parked() -> None:
    checker = StubPermissionChecker(
        {
            ("user-001", "approve_compliance"): True,
            ("user-001", "approve_finance"): True,
        }
    )
    gate = make_gate(checker)

    result = gate.evaluate(
        author_id="user-001",
        affected_scopes={"export.authorization", "billing.refunds"},
        confidence=0.9,
    )

    assert result.disposition is IntakeGateDisposition.PARKED
    assert result.reasons == (IntakeGateReason.NO_COMMON_PERMISSION,)
    assert checker.calls == []


def test_permission_service_failure_parks_candidate() -> None:
    checker = StubPermissionChecker({}, fail=True)
    gate = make_gate(checker)

    result = gate.evaluate(
        author_id="user-001",
        affected_scopes={"export.authorization"},
        confidence=0.9,
    )

    assert result.disposition is IntakeGateDisposition.PARKED
    assert result.reasons == (IntakeGateReason.PERMISSION_CHECK_UNAVAILABLE,)
    assert result.checked_permissions == ("approve_compliance",)


def test_gate_policy_is_a_copy_suitable_for_runtime_injection() -> None:
    checker = StubPermissionChecker({})
    source_policy = {"export.authorization": {"approve_compliance"}}
    gate = make_gate(checker, authority_policy=source_policy)

    copied_policy = gate.authority_policy
    source_policy["export.authorization"].add("untrusted-role")
    copied_policy["export.authorization"].add("another-role")

    assert gate.authority_policy == {
        "export.authorization": {"approve_compliance"}
    }
