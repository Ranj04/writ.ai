"""FROZEN INTERFACE. Lane A implements it. Lane B calls it.
Do not change without both owners agreeing in the same conversation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class InterruptRequest:
    workspace_id: str
    decision_id: str
    affected_scopes: frozenset[str]
    provenance_path: tuple[str, ...]
    interrupt_reason: str
    redirect_instruction: str


@dataclass(frozen=True)
class InterruptResult:
    interrupted_assignment_ids: tuple[str, ...]
    preserved_assignment_ids: tuple[str, ...]


class SupervisorInterruptPort(Protocol):
    def preview(self, request: InterruptRequest) -> InterruptResult:
        """Blast radius. MUST NOT mutate anything."""
        ...

    def interrupt(self, request: InterruptRequest) -> InterruptResult:
        """Transition intersecting assignments to INTERRUPTED. Idempotent per decision_id."""
        ...


class NullSupervisorInterruptPort:
    """Stub so Lane B is unblocked from minute one. Lane A replaces the binding, not this file."""

    def preview(self, request: InterruptRequest) -> InterruptResult:
        return InterruptResult((), ())

    def interrupt(self, request: InterruptRequest) -> InterruptResult:
        return InterruptResult((), ())
