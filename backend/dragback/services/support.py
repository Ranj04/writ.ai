from __future__ import annotations

import hashlib
import hmac
import logging
import re
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any, TypeVar, cast

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_ID_HEADER = "X-Correlation-ID"
INTERNAL_SERVICE_AUTH_HEADER = "X-Dragback-Internal-Authorization"
DEMO_FRONTEND_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
_CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_correlation_id: ContextVar[str | None] = ContextVar("dragback_correlation_id", default=None)
logger = logging.getLogger(__name__)

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


def internal_service_token(secret: str) -> str:
    """Derive a non-secret-bearing capability for authority write routes."""

    return hmac.new(
        secret.encode("utf-8"),
        b"dragback:internal-authority-write:v1",
        hashlib.sha256,
    ).hexdigest()


def require_internal_service(request: Request, *, secret: str) -> None:
    supplied = request.headers.get(INTERNAL_SERVICE_AUTH_HEADER, "")
    expected = internal_service_token(secret)
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise ApiError(
            status_code=403,
            code="INTERNAL_SERVICE_AUTH_REQUIRED",
            message="This authority write route is restricted to a trusted service.",
        )


class ApiError(Exception):
    """A safe, deterministic error that may cross a Dragback API boundary."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def _validated_correlation_id(candidate: str | None) -> str:
    if candidate and _CORRELATION_ID_PATTERN.fullmatch(candidate):
        return candidate
    return new_correlation_id()


def current_correlation_id() -> str:
    """Return the active request correlation ID, creating one for local events if needed."""

    correlation_id = _correlation_id.get()
    if correlation_id is None:
        correlation_id = new_correlation_id()
        _correlation_id.set(correlation_id)
    return correlation_id


def correlated_payload(
    payload: BaseModel | Mapping[str, Any], *, correlation_id: str | None = None
) -> dict[str, Any]:
    """Create a JSON-ready API payload with a top-level correlation ID."""

    if isinstance(payload, BaseModel):
        body = payload.model_dump(mode="json")
    else:
        body = dict(payload)
    body["correlation_id"] = correlation_id or current_correlation_id()
    return body


def event_payload(
    event_type: str,
    data: BaseModel | Mapping[str, Any],
    *,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Build the shared envelope used by service event streams."""

    if isinstance(data, BaseModel):
        event_data = data.model_dump(mode="json")
    else:
        event_data = dict(data)
    return {
        "event": event_type,
        "data": event_data,
        "correlation_id": correlation_id or current_correlation_id(),
    }


def error_payload(error: ApiError, *, correlation_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
        },
        "correlation_id": correlation_id or current_correlation_id(),
    }
    if error.details is not None:
        body["error"]["details"] = error.details
    return body


def _error_response(error: ApiError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error_payload(error))


async def _api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    return _error_response(cast(ApiError, exc))


async def _validation_error_handler(
    _request: Request, exc: Exception
) -> JSONResponse:
    validation_error = cast(RequestValidationError, exc)
    issues = [
        {
            "location": ".".join(str(item) for item in issue["loc"]),
            "type": issue["type"],
        }
        for issue in validation_error.errors()
    ]
    return _error_response(
        ApiError(
            status_code=422,
            code="INVALID_REQUEST",
            message="The request payload is invalid.",
            details={"issues": issues},
        )
    )


async def _http_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    http_error = cast(StarletteHTTPException, exc)
    codes = {
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        409: "INVALID_STATE",
    }
    message = (
        http_error.detail
        if isinstance(http_error.detail, str)
        else "The request could not be completed."
    )
    return _error_response(
        ApiError(
            status_code=http_error.status_code,
            code=codes.get(http_error.status_code, "HTTP_ERROR"),
            message=message,
            retryable=http_error.status_code >= 500,
        )
    )


class CorrelationIdMiddleware:
    """Bind one safe correlation ID to a request, its response, and downstream calls."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_headers = Headers(scope=scope)
        correlation_id = _validated_correlation_id(
            request_headers.get(CORRELATION_ID_HEADER)
        )
        context_token = _correlation_id.set(correlation_id)
        response_started = False

        async def send_with_correlation_id(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                response_headers = MutableHeaders(scope=message)
                response_headers[CORRELATION_ID_HEADER] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation_id)
        except Exception:
            if response_started:
                raise
            logger.exception(
                "Unhandled Dragback API error",
                extra={"correlation_id": correlation_id},
            )
            await _error_response(
                ApiError(
                    status_code=500,
                    code="INTERNAL_ERROR",
                    message="The service could not complete the request.",
                    retryable=True,
                )
            )(scope, receive, send_with_correlation_id)
        finally:
            _correlation_id.reset(context_token)


def install_api_support(app: FastAPI) -> None:
    """Install the same correlation and error contract on every service."""

    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_middleware(CorrelationIdMiddleware)


def post_model(
    *,
    url: str,
    payload: BaseModel,
    response_model: type[ResponseModel],
    upstream_name: str,
    upstream_code: str,
    timeout_seconds: float,
) -> ResponseModel:
    """POST to another service and map transport/protocol failures deterministically."""

    correlation_id = current_correlation_id()
    try:
        response = httpx.post(
            url,
            json=payload.model_dump(mode="json"),
            headers={
                CORRELATION_ID_HEADER: correlation_id,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout_seconds),
        )
    except httpx.TimeoutException as exc:
        raise ApiError(
            status_code=504,
            code=f"{upstream_code}_TIMEOUT",
            message=f"{upstream_name} timed out.",
            retryable=True,
        ) from exc
    except httpx.RequestError as exc:
        raise ApiError(
            status_code=503,
            code=f"{upstream_code}_UNAVAILABLE",
            message=f"{upstream_name} is unavailable.",
            retryable=True,
        ) from exc

    if response.is_error:
        raise ApiError(
            status_code=502,
            code=f"{upstream_code}_ERROR",
            message=f"{upstream_name} returned HTTP {response.status_code}.",
            retryable=response.status_code >= 500 or response.status_code in {408, 429},
        )

    try:
        return response_model.model_validate(response.json())
    except (TypeError, ValueError) as exc:
        raise ApiError(
            status_code=502,
            code=f"{upstream_code}_INVALID_RESPONSE",
            message=f"{upstream_name} returned an invalid response.",
        ) from exc
