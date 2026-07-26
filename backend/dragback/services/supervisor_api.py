"""Agent-service router for Claude Code lifecycle hooks.

The main agent service owns construction of the repository-backed enforcement
service and includes this router. Keeping route construction here avoids a
second service or an executor dependency.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Header

from dragback.services.support import ApiError, correlated_payload
from dragback.workspaces.session_enforcement import (
    ClaudeCodeSessionEnforcement,
    ClaudePreToolUseRequest,
    ClaudeSessionEndRequest,
    ClaudeSessionStartRequest,
)

HOOK_API_KEY_HEADER = "X-Dragback-Hook-API-Key"


@dataclass(frozen=True)
class HookApiKeyVerifier:
    """Constant-time authentication for the organisation-managed hook."""

    expected_api_key: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> HookApiKeyVerifier:
        return cls(expected_api_key=os.getenv("DRAGBACK_HOOK_API_KEY", ""))

    def require(self, supplied_api_key: str | None) -> None:
        expected = self.expected_api_key.strip()
        if not expected:
            raise ApiError(
                status_code=503,
                code="HOOK_AUTHENTICATION_NOT_CONFIGURED",
                message="Claude Code hook authentication is not configured.",
                retryable=False,
            )
        supplied = supplied_api_key.strip() if supplied_api_key else ""
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise ApiError(
                status_code=401,
                code="HOOK_AUTHENTICATION_FAILED",
                message="Claude Code hook authentication failed.",
                retryable=False,
            )


def build_supervisor_session_router(
    enforcement: ClaudeCodeSessionEnforcement,
    *,
    api_key_verifier: HookApiKeyVerifier | None = None,
) -> APIRouter:
    verifier = api_key_verifier or HookApiKeyVerifier.from_environment()
    router = APIRouter(
        prefix="/supervisor/sessions",
        tags=["supervisor-sessions"],
    )

    @router.post("/start")
    def session_start(
        request: ClaudeSessionStartRequest,
        hook_api_key: Annotated[
            str | None,
            Header(alias=HOOK_API_KEY_HEADER),
        ] = None,
    ) -> dict[str, object]:
        verifier.require(hook_api_key)
        return correlated_payload(enforcement.start(request))

    @router.get("")
    def list_sessions(
        hook_api_key: Annotated[
            str | None,
            Header(alias=HOOK_API_KEY_HEADER),
        ] = None,
    ) -> dict[str, object]:
        """Read model for `dragback dev status` / `why` / `ack` and the stage check.

        Each entry carries the binding *and* the assignment state behind it —
        state, the assignment's decision snapshot, the workspace's current one,
        whether they match, and whether the deny has been spent. Without those a
        caller has to make a second round trip per workspace to find the state,
        and can reach a different answer than the hook would.

        Authenticated like the rest of the session routes: it discloses which
        machines are running which task, which is not public.
        """

        verifier.require(hook_api_key)
        return correlated_payload(
            {
                "sessions": [
                    session.to_payload() for session in enforcement.registered_sessions()
                ]
            }
        )

    @router.post("/{session_id}/check")
    def pre_tool_use_check(
        session_id: str,
        request: ClaudePreToolUseRequest,
        hook_api_key: Annotated[
            str | None,
            Header(alias=HOOK_API_KEY_HEADER),
        ] = None,
    ) -> dict[str, object]:
        verifier.require(hook_api_key)
        _require_matching_session(session_id, request.session_id)
        return correlated_payload(enforcement.check(request))

    @router.post("/{session_id}/end")
    def session_end(
        session_id: str,
        request: ClaudeSessionEndRequest,
        hook_api_key: Annotated[
            str | None,
            Header(alias=HOOK_API_KEY_HEADER),
        ] = None,
    ) -> dict[str, object]:
        verifier.require(hook_api_key)
        _require_matching_session(session_id, request.session_id)
        return correlated_payload(enforcement.end(request))

    @router.post("/{session_id}/acknowledge")
    def acknowledge(
        session_id: str,
        hook_api_key: Annotated[
            str | None,
            Header(alias=HOOK_API_KEY_HEADER),
        ] = None,
    ) -> dict[str, object]:
        verifier.require(hook_api_key)
        try:
            result = enforcement.acknowledge(session_id=session_id)
        except KeyError as exc:
            raise ApiError(
                status_code=404,
                code="SUPERVISOR_SESSION_NOT_FOUND",
                message="The Claude Code session has no bound assignment.",
            ) from exc
        return correlated_payload(result)

    return router


def _require_matching_session(path_session_id: str, body_session_id: str) -> None:
    if path_session_id != body_session_id:
        raise ApiError(
            status_code=400,
            code="SUPERVISOR_SESSION_MISMATCH",
            message="The path and request session IDs must match.",
        )
