from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Protocol

from writai.auth.hexclave import HexclavePermissionError


class PermissionChecker(Protocol):
    def has_permission(self, *, user_id: str, permission_id: str) -> bool: ...


class IntakeGateDisposition(StrEnum):
    PENDING = "PENDING"
    PARKED = "PARKED"


class IntakeGateReason(StrEnum):
    MISSING_AUTHOR = "MISSING_AUTHOR"
    NO_AFFECTED_SCOPES = "NO_AFFECTED_SCOPES"
    UNKNOWN_SCOPES = "UNKNOWN_SCOPES"
    INVALID_CONFIDENCE = "INVALID_CONFIDENCE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    NO_COMMON_PERMISSION = "NO_COMMON_PERMISSION"
    AUTHOR_NOT_AUTHORIZED = "AUTHOR_NOT_AUTHORIZED"
    PERMISSION_CHECK_UNAVAILABLE = "PERMISSION_CHECK_UNAVAILABLE"


@dataclass(frozen=True)
class GateResult:
    """Routing result only; authority verdicts remain owned by IntentAuthority."""

    disposition: IntakeGateDisposition
    reasons: tuple[IntakeGateReason, ...]
    affected_scopes: frozenset[str]
    permission_id: str | None = None
    checked_permissions: tuple[str, ...] = ()

    @property
    def eligible_for_confirmation(self) -> bool:
        return self.disposition is IntakeGateDisposition.PENDING


IntakeGateResult = GateResult


class DeterministicIntakeGate:
    """Parks noisy candidates before human review using trusted governance inputs."""

    def __init__(
        self,
        *,
        known_scopes: Set[str],
        authority_policy: Mapping[str, Set[str]],
        permission_checker: PermissionChecker,
        confidence_threshold: float,
    ) -> None:
        if (
            not isfinite(confidence_threshold)
            or confidence_threshold < 0
            or confidence_threshold > 1
        ):
            raise ValueError("confidence_threshold must be between zero and one")
        normalized_scopes = frozenset(_normalized_values("known scope", known_scopes))
        if not normalized_scopes:
            raise ValueError("known_scopes must contain at least one scope")
        normalized_policy: dict[str, frozenset[str]] = {}
        for scope, permissions in authority_policy.items():
            normalized_scope = scope.strip()
            if not normalized_scope:
                raise ValueError("authority_policy scope names must not be blank")
            if normalized_scope in normalized_policy:
                raise ValueError(
                    "authority_policy scope names must be unique after normalization"
                )
            normalized_permissions = frozenset(
                _normalized_values(
                    f"permission for scope {scope!r}",
                    permissions,
                )
            )
            if not normalized_permissions:
                raise ValueError(
                    f"authority_policy has no permissions for scope {normalized_scope!r}"
                )
            normalized_policy[normalized_scope] = normalized_permissions
        missing_policy = normalized_scopes - set(normalized_policy)
        extra_policy = set(normalized_policy) - normalized_scopes
        if missing_policy or extra_policy:
            raise ValueError(
                "authority_policy scopes must exactly match known_scopes"
            )

        self._known_scopes = normalized_scopes
        self._authority_policy = normalized_policy
        self._permission_checker = permission_checker
        self._confidence_threshold = confidence_threshold

    @property
    def authority_policy(self) -> dict[str, set[str]]:
        return {
            scope: set(permission_ids)
            for scope, permission_ids in self._authority_policy.items()
        }

    @property
    def known_scopes(self) -> set[str]:
        return set(self._known_scopes)

    def evaluate(
        self,
        *,
        author_id: str,
        affected_scopes: Set[str],
        confidence: float,
    ) -> GateResult:
        normalized_author_id = author_id.strip()
        normalized_scopes = frozenset(
            scope.strip() for scope in affected_scopes if scope.strip()
        )
        reasons: list[IntakeGateReason] = []
        if not normalized_author_id:
            reasons.append(IntakeGateReason.MISSING_AUTHOR)
        if not normalized_scopes:
            reasons.append(IntakeGateReason.NO_AFFECTED_SCOPES)

        unknown_scopes = normalized_scopes - self._known_scopes
        if unknown_scopes:
            reasons.append(IntakeGateReason.UNKNOWN_SCOPES)
        if not isfinite(confidence) or confidence < 0 or confidence > 1:
            reasons.append(IntakeGateReason.INVALID_CONFIDENCE)
        elif confidence < self._confidence_threshold:
            reasons.append(IntakeGateReason.LOW_CONFIDENCE)

        common_permissions = self._common_permissions(normalized_scopes)
        if normalized_scopes and not unknown_scopes and not common_permissions:
            reasons.append(IntakeGateReason.NO_COMMON_PERMISSION)
        if reasons:
            return GateResult(
                disposition=IntakeGateDisposition.PARKED,
                reasons=tuple(reasons),
                affected_scopes=normalized_scopes,
            )

        checked_permissions: list[str] = []
        for permission_id in sorted(common_permissions):
            checked_permissions.append(permission_id)
            try:
                allowed = self._permission_checker.has_permission(
                    user_id=normalized_author_id,
                    permission_id=permission_id,
                )
            except HexclavePermissionError:
                return GateResult(
                    disposition=IntakeGateDisposition.PARKED,
                    reasons=(IntakeGateReason.PERMISSION_CHECK_UNAVAILABLE,),
                    affected_scopes=normalized_scopes,
                    checked_permissions=tuple(checked_permissions),
                )
            if allowed:
                return GateResult(
                    disposition=IntakeGateDisposition.PENDING,
                    reasons=(),
                    affected_scopes=normalized_scopes,
                    permission_id=permission_id,
                    checked_permissions=tuple(checked_permissions),
                )

        return GateResult(
            disposition=IntakeGateDisposition.PARKED,
            reasons=(IntakeGateReason.AUTHOR_NOT_AUTHORIZED,),
            affected_scopes=normalized_scopes,
            checked_permissions=tuple(checked_permissions),
        )

    def _common_permissions(self, scopes: frozenset[str]) -> frozenset[str]:
        if not scopes:
            return frozenset()
        permissions_by_scope = [
            self._authority_policy.get(scope, frozenset()) for scope in sorted(scopes)
        ]
        common = set(permissions_by_scope[0])
        for permissions in permissions_by_scope[1:]:
            common.intersection_update(permissions)
        return frozenset(common)


def _normalized_values(label: str, values: Set[str]) -> set[str]:
    normalized = {value.strip() for value in values}
    if "" in normalized:
        raise ValueError(f"{label} values must not be blank")
    return normalized
