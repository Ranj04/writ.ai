from __future__ import annotations

from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel

from writai.config import Settings, settings
from writai.domain import (
    AgentPlan,
    AuthorizationRequest,
    AuthorizationResult,
    MutationResult,
)
from writai.services.support import (
    CORRELATION_ID_HEADER,
    INTERNAL_SERVICE_AUTH_HEADER,
    ApiError,
    current_correlation_id,
    internal_service_token,
    post_model,
)
from writai.workspaces.authority_contexts import (
    DynamicAuthorityContextCreateRequest,
    DynamicAuthorityContextState,
    DynamicMutationApprovalRequest,
)
from writai.workspaces.models import (
    WorkspaceApprovalRequest,
    WorkspaceExecutionResult,
)

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class _WorkspaceExecuteRequest(BaseModel):
    token: str
    run_id: str
    task_id: str
    plan: AgentPlan
    context_id: str
    context_kind: str = "workspace"


class LiveWorkspaceTransport(Protocol):
    def context_state(
        self, context_id: str
    ) -> DynamicAuthorityContextState | None: ...

    def create_context(
        self, request: DynamicAuthorityContextCreateRequest
    ) -> DynamicAuthorityContextState: ...

    def delete_context(self, context_id: str) -> None: ...

    def approve_baseline(
        self, context_id: str, request: WorkspaceApprovalRequest
    ) -> DynamicAuthorityContextState: ...

    def approve_mutation(
        self, context_id: str, request: DynamicMutationApprovalRequest
    ) -> MutationResult: ...

    def authorize(
        self, context_id: str, request: AuthorizationRequest
    ) -> AuthorizationResult: ...

    def execute(
        self,
        *,
        context_id: str,
        token: str,
        run_id: str,
        task_id: str,
        plan: AgentPlan,
    ) -> WorkspaceExecutionResult: ...


class HttpLiveWorkspaceTransport:
    """Agent-side adapter that preserves authority and executor boundaries."""

    def __init__(self, config: Settings = settings) -> None:
        self._settings = config

    @property
    def _authority_base(self) -> str:
        return f"{self._settings.authority_url}/live-workspaces/authority/contexts"

    def _headers(self) -> dict[str, str]:
        return {
            CORRELATION_ID_HEADER: current_correlation_id(),
            INTERNAL_SERVICE_AUTH_HEADER: internal_service_token(
                self._settings.grant_secret
            ),
            "Accept": "application/json",
        }

    def _post_authority_model(
        self,
        *,
        url: str,
        payload: BaseModel,
        response_model: type[ResponseModel],
    ) -> ResponseModel:
        try:
            response = httpx.post(
                url,
                json=payload.model_dump(mode="json"),
                headers=self._headers(),
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
        if response.is_error:
            if response.status_code in {404, 409, 422}:
                body: dict[str, Any]
                try:
                    body = response.json()
                except (TypeError, ValueError):
                    body = {}
                error = body.get("error")
                raw_message = error.get("message") if isinstance(error, dict) else None
                message = (
                    raw_message
                    if isinstance(raw_message, str)
                    else "Intent authority rejected the operation."
                )
                raw_code = error.get("code") if isinstance(error, dict) else None
                code = (
                    raw_code
                    if isinstance(raw_code, str)
                    else "AUTHORITY_REJECTED"
                )
                raise ApiError(
                    status_code=response.status_code,
                    code=code,
                    message=message,
                )
            raise ApiError(
                status_code=502,
                code="AUTHORITY_ERROR",
                message=f"Intent authority returned HTTP {response.status_code}.",
                retryable=response.status_code >= 500,
            )
        try:
            return response_model.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise ApiError(
                status_code=502,
                code="AUTHORITY_INVALID_RESPONSE",
                message="Intent authority returned an invalid response.",
            ) from exc

    def context_state(
        self, context_id: str
    ) -> DynamicAuthorityContextState | None:
        try:
            response = httpx.get(
                f"{self._authority_base}/{context_id}",
                headers=self._headers(),
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
        if response.status_code == 404:
            return None
        if response.is_error:
            raise ApiError(
                status_code=502,
                code="AUTHORITY_ERROR",
                message=f"Intent authority returned HTTP {response.status_code}.",
                retryable=response.status_code >= 500,
            )
        try:
            return DynamicAuthorityContextState.model_validate(response.json())
        except (TypeError, ValueError) as exc:
            raise ApiError(
                status_code=502,
                code="AUTHORITY_INVALID_RESPONSE",
                message="Intent authority returned an invalid context state.",
            ) from exc

    def create_context(
        self, request: DynamicAuthorityContextCreateRequest
    ) -> DynamicAuthorityContextState:
        return self._post_authority_model(
            url=self._authority_base,
            payload=request,
            response_model=DynamicAuthorityContextState,
        )

    def delete_context(self, context_id: str) -> None:
        try:
            response = httpx.delete(
                f"{self._authority_base}/{context_id}",
                headers=self._headers(),
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

    def approve_baseline(
        self, context_id: str, request: WorkspaceApprovalRequest
    ) -> DynamicAuthorityContextState:
        return self._post_authority_model(
            url=f"{self._authority_base}/{context_id}/baseline/approve",
            payload=request,
            response_model=DynamicAuthorityContextState,
        )

    def approve_mutation(
        self, context_id: str, request: DynamicMutationApprovalRequest
    ) -> MutationResult:
        return self._post_authority_model(
            url=f"{self._authority_base}/{context_id}/mutations/approve",
            payload=request,
            response_model=MutationResult,
        )

    def authorize(
        self, context_id: str, request: AuthorizationRequest
    ) -> AuthorizationResult:
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
    ) -> WorkspaceExecutionResult:
        return post_model(
            url=f"{self._settings.executor_url}/execute",
            payload=_WorkspaceExecuteRequest(
                context_id=context_id,
                token=token,
                run_id=run_id,
                task_id=task_id,
                plan=plan,
            ),
            response_model=WorkspaceExecutionResult,
            upstream_name="Executor",
            upstream_code="EXECUTOR",
            timeout_seconds=self._settings.service_timeout_seconds,
        )
