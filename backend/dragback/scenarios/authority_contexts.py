from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock

from pydantic import BaseModel, Field

from dragback.authority.engine import IntentAuthority
from dragback.domain import (
    Artifact,
    AuthorizationRequest,
    AuthorizationResult,
    DecisionMutation,
    Edge,
    GrantVerificationRequest,
    GrantVerificationResult,
    InvalidationReport,
    MutationResult,
)
from dragback.grants import GrantSigner
from dragback.graph.memory import MemoryGraphStore
from dragback.scenarios import SCENARIO_AUTHORITY_POLICY, get_scenario

_CONTEXT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]+$"


class ScenarioAuthorityContextCreateRequest(BaseModel):
    context_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=_CONTEXT_ID_PATTERN,
    )
    scenario_id: str = Field(min_length=1, max_length=128)


class ScenarioAuthorityContextState(BaseModel):
    context_id: str
    scenario_id: str
    graph_version: str
    artifacts: list[Artifact]
    edges: list[Edge]
    last_report: InvalidationReport | None = None


class ScenarioAuthorityContextError(Exception):
    pass


class ScenarioDefinitionNotFound(ScenarioAuthorityContextError):
    def __init__(self, scenario_id: str) -> None:
        super().__init__(f"Unknown scenario: {scenario_id}")
        self.scenario_id = scenario_id


class ScenarioAuthorityContextNotFound(ScenarioAuthorityContextError):
    def __init__(self, context_id: str) -> None:
        super().__init__(f"Unknown scenario authority context: {context_id}")
        self.context_id = context_id


class ScenarioAuthorityContextConflict(ScenarioAuthorityContextError):
    pass


@dataclass
class _ScenarioAuthorityContext:
    context_id: str
    scenario_id: str
    graph: MemoryGraphStore
    authority: IntentAuthority
    mutation: DecisionMutation
    mutation_applied: bool = False
    lock: RLock = field(default_factory=RLock, repr=False)

    def state(self) -> ScenarioAuthorityContextState:
        report = self.authority.last_report
        return ScenarioAuthorityContextState(
            context_id=self.context_id,
            scenario_id=self.scenario_id,
            graph_version=self.graph.version_label,
            artifacts=self.graph.list_artifacts(),
            edges=self.graph.list_edges(),
            last_report=report.model_copy(deep=True) if report else None,
        )


class ScenarioAuthorityContextRegistry:
    """Own isolated, in-memory authority runtimes for Scenario Lab runs."""

    def __init__(
        self,
        *,
        grant_secret: str,
        grant_ttl_seconds: int,
        authority_threshold: float,
    ) -> None:
        self._grant_secret = grant_secret
        self._grant_ttl_seconds = grant_ttl_seconds
        self._authority_threshold = authority_threshold
        self._contexts: dict[str, _ScenarioAuthorityContext] = {}
        self._lock = RLock()

    def _context_signing_secret(self, context_id: str) -> str:
        return hmac.new(
            self._grant_secret.encode("utf-8"),
            context_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def create(
        self,
        request: ScenarioAuthorityContextCreateRequest,
    ) -> ScenarioAuthorityContextState:
        with self._lock:
            if request.context_id in self._contexts:
                raise ScenarioAuthorityContextConflict(
                    f"Scenario authority context already exists: {request.context_id}"
                )
            try:
                scenario = get_scenario(request.scenario_id)
            except KeyError as exc:
                raise ScenarioDefinitionNotFound(request.scenario_id) from exc

            graph = MemoryGraphStore()
            graph.reset(
                version=scenario.graph_seed.version,
                artifacts=scenario.graph_seed.artifacts,
                edges=scenario.graph_seed.edges,
            )
            authority_policy = {
                scope: set(roles) for scope, roles in SCENARIO_AUTHORITY_POLICY.items()
            }
            for scope, roles in scenario.authority_policy.items():
                authority_policy.setdefault(scope, set()).update(roles)
            authority = IntentAuthority(
                graph=graph,
                signer=GrantSigner(
                    self._context_signing_secret(request.context_id),
                    ttl_seconds=self._grant_ttl_seconds,
                ),
                authority_threshold=self._authority_threshold,
                authority_policy=authority_policy,
            )
            context = _ScenarioAuthorityContext(
                context_id=request.context_id,
                scenario_id=request.scenario_id,
                graph=graph,
                authority=authority,
                mutation=scenario.mutation,
            )
            self._contexts[request.context_id] = context
            return context.state()

    @contextmanager
    def _access(self, context_id: str) -> Iterator[_ScenarioAuthorityContext]:
        with self._lock:
            try:
                context = self._contexts[context_id]
            except KeyError as exc:
                raise ScenarioAuthorityContextNotFound(context_id) from exc
            context.lock.acquire()
        try:
            yield context
        finally:
            context.lock.release()

    def state(self, context_id: str) -> ScenarioAuthorityContextState:
        with self._access(context_id) as context:
            return context.state()

    def delete(self, context_id: str) -> None:
        with self._lock:
            try:
                context = self._contexts[context_id]
            except KeyError as exc:
                raise ScenarioAuthorityContextNotFound(context_id) from exc
            with context.lock:
                del self._contexts[context_id]

    def apply_mutation(self, context_id: str) -> MutationResult:
        with self._access(context_id) as context:
            if context.mutation_applied:
                raise ScenarioAuthorityContextConflict(
                    "The scenario mutation has already been applied."
                )
            result = context.authority.apply_decision_change(context.mutation.model_copy(deep=True))
            if not result.applied:
                raise ScenarioAuthorityContextConflict(result.reason)
            context.mutation_applied = True
            return result

    def authorize(
        self,
        context_id: str,
        request: AuthorizationRequest,
    ) -> AuthorizationResult:
        with self._access(context_id) as context:
            return context.authority.evaluate_plan(
                run_id=request.run_id,
                task_id=request.task_id,
                plan=request.plan,
            )

    def verify_grant(
        self,
        context_id: str,
        request: GrantVerificationRequest,
    ) -> GrantVerificationResult:
        with self._access(context_id) as context:
            return context.authority.verify_grant(
                token=request.token,
                run_id=request.run_id,
                task_id=request.task_id,
                plan=request.plan,
            )
