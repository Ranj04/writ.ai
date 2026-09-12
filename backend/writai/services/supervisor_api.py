"""Agent-service router for Claude Code lifecycle hooks.

The main agent service owns construction of the repository-backed enforcement
service and includes this router. Keeping route construction here avoids a
second service or an executor dependency.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Header

from writai.services.support import ApiError, correlated_payload
from writai.workspaces.session_binding import DEFAULT_HOOK_DEVELOPER_ID
from writai.workspaces.session_enforcement import (
    ClaudeCodeSessionEnforcement,
    ClaudePreToolUseRequest,
    ClaudeSessionEndRequest,
    ClaudeSessionStartRequest,
)

HOOK_API_KEY_HEADER = "X-writ.ai-Hook-API-Key"


#: ``developer_id:secret,developer_id:secret`` — one credential per developer.
HOOK_API_KEYS_ENV = "WRITAI_HOOK_API_KEYS"
#: Single-developer fallback; its holder is developer ``"default"``.
HOOK_API_KEY_ENV = "WRITAI_HOOK_API_KEY"


def parse_hook_credentials(raw: str) -> dict[str, str]:
    """Parse ``WRITAI_HOOK_API_KEYS`` into ``{secret: developer_id}``.

    Blank segments are ignored and whitespace around both halves is stripped. A
    segment without a ``:`` — or with an empty half, or a secret already used by
    another developer — is a configuration error, raised here so a misconfigured
    service fails at startup rather than authenticating nobody.
    """

    credentials: dict[str, str] = {}
    for segment in raw.split(","):
        entry = segment.strip()
        if not entry:
            continue
        developer_id, separator, secret = entry.partition(":")
        developer_id = developer_id.strip()
        secret = secret.strip()
        if not separator or not developer_id or not secret:
            raise ValueError(
                f"{HOOK_API_KEYS_ENV} entries must be developer_id:secret; "
                "got a segment with no usable separator."
            )
        if credentials.get(secret, developer_id) != developer_id:
            raise ValueError(
                f"{HOOK_API_KEYS_ENV} configures one secret for two developers."
            )
        credentials[secret] = developer_id
    return credentials


@dataclass(frozen=True)
class HookCredentialVerifier:
    """Constant-time authentication for the organisation-managed hook.

    A credential identifies exactly one developer. ``credentials`` maps
    secret -> developer id from ``WRITAI_HOOK_API_KEYS``; ``expected_api_key``
    is the single-developer ``WRITAI_HOOK_API_KEY`` fallback, mapped to
    developer ``"default"`` so an existing one-key setup keeps working.
    """

    credentials: Mapping[str, str] = field(default_factory=dict, repr=False)
    expected_api_key: str = field(default="", repr=False)

    @classmethod
    def from_environment(cls) -> HookCredentialVerifier:
        return cls(
            credentials=parse_hook_credentials(os.getenv(HOOK_API_KEYS_ENV, "")),
            expected_api_key=os.getenv(HOOK_API_KEY_ENV, ""),
        )

    def _configured(self) -> list[tuple[str, str]]:
        configured = [
            (secret.strip(), developer_id)
            for secret, developer_id in self.credentials.items()
            if secret.strip()
        ]
        single = self.expected_api_key.strip()
        if single:
            configured.append((single, DEFAULT_HOOK_DEVELOPER_ID))
        return configured

    def resolve(self, supplied_api_key: str | None) -> str:
        """Return the developer id behind ``supplied_api_key``.

        The two failure modes are unchanged from the single-key verifier: 503
        when nothing is configured, 401 otherwise. Neither response names a
        developer or a secret.
        """

        expected = self._configured()
        if not expected:
            raise ApiError(
                status_code=503,
                code="HOOK_AUTHENTICATION_NOT_CONFIGURED",
                message="Claude Code hook authentication is not configured.",
                retryable=False,
            )
        supplied = supplied_api_key.strip() if supplied_api_key else ""
        resolved: str | None = None
        for secret, developer_id in expected:
            # Every configured secret is compared, in turn, with compare_digest.
            # A dict lookup keyed on the secret would be a timing oracle.
            if supplied and hmac.compare_digest(supplied, secret):
                resolved = developer_id
        if resolved is None:
            raise ApiError(
                status_code=401,
                code="HOOK_AUTHENTICATION_FAILED",
                message="Claude Code hook authentication failed.",
                retryable=False,
            )
        return resolved


def build_supervisor_session_router(
    enforcement: ClaudeCodeSessionEnforcement,
    *,
    api_key_verifier: HookCredentialVerifier | None = None,
) -> APIRouter:
    verifier = api_key_verifier or HookCredentialVerifier.from_environment()
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
        owner_id = verifier.resolve(hook_api_key)
        return correlated_payload(enforcement.start(request, owner_id=owner_id))

    @router.get("")
    def list_sessions(
        hook_api_key: Annotated[
            str | None,
            Header(alias=HOOK_API_KEY_HEADER),
        ] = None,
    ) -> dict[str, object]:
        """Read model for `writai dev status` / `why` / `ack` and the stage check.

        Each entry carries the binding *and* the assignment state behind it —
        state, the assignment's decision snapshot, the workspace's current one,
        whether they match, and whether the deny has been spent. Without those a
        caller has to make a second round trip per workspace to find the state,
        and can reach a different answer than the hook would.

        Authenticated like the rest of the session routes: it discloses which
        machines are running which task, which is not public. Filtered to the
        developer the credential resolves to, for the same reason a developer
        cannot check or end another's session. A single ``WRITAI_HOOK_API_KEY``
        deployment registers every session under the default developer, so
        it keeps seeing all of them.
        """

        owner_id = verifier.resolve(hook_api_key)
        return correlated_payload(
            {
                "sessions": [
                    session.to_payload()
                    for session in enforcement.registered_sessions(owner_id=owner_id)
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
        owner_id = verifier.resolve(hook_api_key)
        _require_matching_session(session_id, request.session_id)
        return correlated_payload(enforcement.check(request, owner_id=owner_id))

    @router.post("/{session_id}/end")
    def session_end(
        session_id: str,
        request: ClaudeSessionEndRequest,
        hook_api_key: Annotated[
            str | None,
            Header(alias=HOOK_API_KEY_HEADER),
        ] = None,
    ) -> dict[str, object]:
        owner_id = verifier.resolve(hook_api_key)
        _require_matching_session(session_id, request.session_id)
        return correlated_payload(enforcement.end(request, owner_id=owner_id))

    @router.post("/{session_id}/acknowledge")
    def acknowledge(
        session_id: str,
        hook_api_key: Annotated[
            str | None,
            Header(alias=HOOK_API_KEY_HEADER),
        ] = None,
    ) -> dict[str, object]:
        owner_id = verifier.resolve(hook_api_key)
        try:
            result = enforcement.acknowledge(
                session_id=session_id,
                owner_id=owner_id,
            )
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
