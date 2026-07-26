from __future__ import annotations

from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from dragback.config import Settings, settings
from dragback.domain import (
    AgentPlan,
    AuthorizationRequest,
    AuthorizationResult,
    MutationResult,
)
from dragback.scenarios.authority_contexts import (
    ScenarioAuthorityContextCreateRequest,
    ScenarioAuthorityContextState,
)
from dragback.scenarios.run_models import ScenarioExecutionResult
from dragback.services.support import (
    CORRELATION_ID_HEADER,
    ApiError,
    current_correlation_id,
    post_model,
)


class EmptyScenarioRequest(BaseModel):
    pass


class ScenarioExecuteRequest(BaseModel):
    token: str
    run_id: str
    task_id: str
    plan: AgentPlan
    context_id: str = Field(min_length=8, max_length=128)


class ScenarioTransport(Protocol):
    def create_context(
        self, request: ScenarioAuthorityContextCreateRequest
    ) -> ScenarioAuthorityContextState: ...

    def delete_context(self, context_id: str) -> None: ...

    def apply_mutation(self, context_id: str) -> MutationResult: ...

    def authorize(self, context_id: str, request: AuthorizationRequest) -> AuthorizationResult: ...

    def execute(
        self,
        *,
        context_id: str,
        token: str,
        run_id: str,
        task_id: str,
        plan: AgentPlan,
    ) -> ScenarioExecutionResult: ...


class HttpScenarioTransport:
    """Cross-service adapter; orchestration never bypasses authority or executor."""

    def __init__(self, config: Settings = settings) -> None:
        self._settings = config

    @property
    def _authority_base(self) -> str:
        return f"{self._settings.authority_url}/scenario-lab/authority/contexts"

    def create_context(
        self, request: ScenarioAuthorityContextCreateRequest
    ) -> ScenarioAuthorityContextState:
        return post_model(
            url=self._authority_base,
            payload=request,
            response_model=ScenarioAuthorityContextState,
            upstream_name="Intent authority",
            upstream_code="AUTHORITY",
            timeout_seconds=self._settings.service_timeout_seconds,
        )

    def delete_context(self, context_id: str) -> None:
        correlation_id = current_correlation_id()
        try:
            response = httpx.delete(
                f"{self._authority_base}/{context_id}",
                headers={
                    CORRELATION_ID_HEADER: correlation_id,
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(self._settings.service_timeout_seconds),
            )
        except httpx.TimeoutException as exc:
            raise ApiError(
                status_code=504,
                code="AUTHORITY_TIMEOUT",
                message="Intent authority timed out.",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise ApiError(
                status_code=503,
                code="AUTHORITY_UNAVAILABLE",
                message="Intent authority is unavailable.",
                retryable=True,
            ) from exc
        if response.is_error and response.status_code != 404:
            raise ApiError(
                status_code=502,
                code="AUTHORITY_ERROR",
                message=f"Intent authority returned HTTP {response.status_code}.",
                retryable=response.status_code >= 500,
            )

    def apply_mutation(self, context_id: str) -> MutationResult:
        return post_model(
            url=f"{self._authority_base}/{context_id}/mutation",
            payload=EmptyScenarioRequest(),
            response_model=MutationResult,
            upstream_name="Intent authority",
            upstream_code="AUTHORITY",
            timeout_seconds=self._settings.service_timeout_seconds,
        )

    def authorize(self, context_id: str, request: AuthorizationRequest) -> AuthorizationResult:
        return post_model(
            url=f"{self._authority_base}/{context_id}/authorize",
            payload=request,
            response_model=AuthorizationResult,
            upstream_name="Intent authority",
            upstream_code="AUTHORITY",
            timeout_seconds=self._settings.service_timeout_seconds,
        )

    def execute(
        self,
        *,
        context_id: str,
        token: str,
        run_id: str,
        task_id: str,
        plan: AgentPlan,
    ) -> ScenarioExecutionResult:
        return post_model(
            url=f"{self._settings.executor_url}/execute",
            payload=ScenarioExecuteRequest(
                context_id=context_id,
                token=token,
                run_id=run_id,
                task_id=task_id,
                plan=plan,
            ),
            response_model=ScenarioExecutionResult,
            upstream_name="Executor",
            upstream_code="EXECUTOR",
            timeout_seconds=self._settings.service_timeout_seconds,
        )
