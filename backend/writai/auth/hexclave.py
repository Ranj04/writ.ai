from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

import httpx

HEXCLAVE_DEFAULT_API_URL = "https://api.hexclave.com/api/v1"
HEXCLAVE_TEAM_PERMISSIONS_PATH = "/team-permissions"
HEXCLAVE_USER_API_KEY_CHECK_PATH = "/user-api-keys/check"
#: Resolves a short-lived ACCESS TOKEN, which is what the browser SDK issues.
#: Distinct from a user API key: the key is long-lived and pasted by a human,
#: the token comes from a real sign-in. Both must land on the same user id so
#: the permission check and the audit record cannot tell the surfaces apart.
HEXCLAVE_ACCESS_TOKEN_PATH = "/users/me"
HEXCLAVE_DEFAULT_CACHE_TTL_SECONDS = 60.0
HEXCLAVE_DEFAULT_TIMEOUT_SECONDS = 5.0


class HexclavePermissionError(RuntimeError):
    """A sanitized permission lookup failure safe to expose to the intake gate."""


class HexclaveConfigurationError(HexclavePermissionError):
    """The Hexclave checker is missing required or safe configuration."""


@dataclass(frozen=True)
class TeamPermissionRequest:
    project_id: str
    team_id: str
    user_id: str
    permission_id: str
    recursive: bool = True


@dataclass(frozen=True)
class TeamPermissionResponse:
    allowed: bool


class HexclaveTransport(Protocol):
    """Transport boundary for one deterministic Hexclave permission lookup."""

    def get_team_permission(
        self,
        request: TeamPermissionRequest,
        *,
        secret_key: str,
        timeout_seconds: float,
    ) -> TeamPermissionResponse: ...


class HttpxHexclaveTransport:
    """Small REST adapter isolated from the cached permission decision."""

    def __init__(
        self,
        *,
        api_url: str = HEXCLAVE_DEFAULT_API_URL,
        http_client: httpx.Client | None = None,
    ) -> None:
        normalized_url = api_url.rstrip("/")
        parsed = urlparse(normalized_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise HexclaveConfigurationError(
                "The Hexclave API URL must be an absolute HTTPS URL."
            )
        self._api_url = normalized_url
        self._http_client = http_client or httpx.Client()

    def get_team_permission(
        self,
        request: TeamPermissionRequest,
        *,
        secret_key: str,
        timeout_seconds: float,
    ) -> TeamPermissionResponse:
        try:
            response = self._http_client.get(
                f"{self._api_url}{HEXCLAVE_TEAM_PERMISSIONS_PATH}",
                params={
                    "team_id": request.team_id,
                    "user_id": request.user_id,
                    "permission_id": request.permission_id,
                    "recursive": "true" if request.recursive else "false",
                },
                headers={
                    "Accept": "application/json",
                    "X-Hexclave-Access-Type": "server",
                    "X-Hexclave-Project-Id": request.project_id,
                    "X-Hexclave-Secret-Server-Key": secret_key,
                },
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HexclavePermissionError(
                "Hexclave permission lookup failed."
            ) from exc

        allowed = _permission_value(payload, request=request)
        if allowed is None:
            raise HexclavePermissionError(
                "Hexclave returned an invalid permission response."
            )
        return TeamPermissionResponse(allowed=allowed)


class HexclaveUserApiKeyIdentityResolver:
    """Resolve an opaque Hexclave user API key to its current human identity."""

    def __init__(
        self,
        *,
        project_id: str | None,
        secret_key: str | None,
        api_url: str = HEXCLAVE_DEFAULT_API_URL,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = HEXCLAVE_DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._project_id = _required_value("project ID", project_id)
        self._secret_key = _required_value("secret server key", secret_key)
        normalized_url = api_url.rstrip("/")
        parsed = urlparse(normalized_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise HexclaveConfigurationError(
                "The Hexclave API URL must be an absolute HTTPS URL."
            )
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise HexclaveConfigurationError(
                "The Hexclave request timeout must be greater than zero."
            )
        self._api_url = normalized_url
        self._http_client = http_client or httpx.Client()
        self._timeout_seconds = timeout_seconds

    def resolve_user_id(self, *, approval_token: str) -> str:
        token = _required_value("user API key", approval_token)
        try:
            response = self._http_client.post(
                f"{self._api_url}{HEXCLAVE_USER_API_KEY_CHECK_PATH}",
                json={"api_key": token},
                headers={
                    "Accept": "application/json",
                    "X-Hexclave-Access-Type": "server",
                    "X-Hexclave-Project-Id": self._project_id,
                    "X-Hexclave-Secret-Server-Key": self._secret_key,
                },
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HexclavePermissionError(
                "Hexclave approval authentication failed."
            ) from exc
        if not isinstance(payload, Mapping):
            raise HexclavePermissionError(
                "Hexclave returned an invalid approval identity."
            )
        user_id = payload.get("user_id")
        key_type = payload.get("type")
        is_public = payload.get("is_public")
        if (
            not isinstance(user_id, str)
            or not user_id.strip()
            or key_type != "user"
            or not isinstance(is_public, bool)
            or is_public
        ):
            raise HexclavePermissionError(
                "Hexclave returned an invalid approval identity."
            )
        return user_id.strip()


@dataclass(frozen=True)
class _CacheEntry:
    allowed: bool
    expires_at: float


if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a cycle
    from writai.intake.approval import ApprovalIdentityResolver


class HexclaveAccessTokenIdentityResolver:
    """Resolve a browser SDK access token to its Hexclave user id.

    `@hexclave/react` signs a person in and hands the page an `Authorization`
    header. That is an ACCESS TOKEN, not a user API key, so it does not resolve
    through `/user-api-keys/check` — a token posted there is simply rejected,
    which would read as "unauthorised" when the person is in fact signed in.

    This resolver closes that half, so the web surface reaches the SAME
    permission check as the CLI and the Slack reaction, with the same user id.

    It resolves identity ONLY. It never decides whether that user may approve;
    `ApprovalCoordinator` asks `HexclavePermissionChecker` for that, exactly as
    it does for every other surface.
    """

    def __init__(
        self,
        *,
        project_id: str | None,
        secret_key: str | None,
        api_url: str = HEXCLAVE_DEFAULT_API_URL,
        http_client: httpx.Client | None = None,
        timeout_seconds: float = HEXCLAVE_DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._project_id = _required_value("project ID", project_id)
        self._secret_key = _required_value("secret server key", secret_key)
        normalized_url = api_url.rstrip("/")
        parsed = urlparse(normalized_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise HexclaveConfigurationError(
                "The Hexclave API URL must be an absolute HTTPS URL."
            )
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise HexclaveConfigurationError(
                "The Hexclave request timeout must be greater than zero."
            )
        self._api_url = normalized_url
        self._http_client = http_client or httpx.Client()
        self._timeout_seconds = timeout_seconds

    def resolve_user_id(self, *, approval_token: str) -> str:
        token = _required_value("access token", approval_token)
        try:
            response = self._http_client.get(
                f"{self._api_url}{HEXCLAVE_ACCESS_TOKEN_PATH}",
                headers={
                    "Accept": "application/json",
                    # The documented header trio for an access-token lookup.
                    "x-stack-access-token": token,
                    "x-stack-project-id": self._project_id,
                    "x-stack-secret-server-key": self._secret_key,
                    "x-stack-access-type": "server",
                },
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HexclavePermissionError(
                "Hexclave approval authentication failed."
            ) from exc
        if not isinstance(payload, Mapping):
            raise HexclavePermissionError(
                "Hexclave returned an invalid approval identity."
            )
        # Hexclave returns the signed-in user directly. Accept only a concrete
        # id: an anonymous or partially-populated response is not an approver.
        user_id = payload.get("id") or payload.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            raise HexclavePermissionError(
                "Hexclave returned an invalid approval identity."
            )
        if payload.get("is_anonymous") is True:
            raise HexclavePermissionError(
                "An anonymous Hexclave session cannot approve."
            )
        return user_id.strip()


class ChainedHexclaveIdentityResolver:
    """Try each resolver in turn; the first that resolves an id wins.

    The web surface sends an access token and the CLI sends a user API key, and
    the server is handed one opaque `approval_token` either way. Rather than
    make each caller declare which kind it holds — a claim the caller could get
    wrong, or lie about — this asks Hexclave and lets the answer decide.

    Failing every resolver raises, so an unresolvable token is refused rather
    than falling through to some default identity.
    """

    def __init__(self, *resolvers: ApprovalIdentityResolver) -> None:
        if not resolvers:
            raise HexclaveConfigurationError(
                "At least one Hexclave identity resolver is required."
            )
        self._resolvers = resolvers

    def resolve_user_id(self, *, approval_token: str) -> str:
        last: Exception | None = None
        for resolver in self._resolvers:
            try:
                return resolver.resolve_user_id(approval_token=approval_token)
            except HexclavePermissionError as exc:
                last = exc
        raise HexclavePermissionError(
            "Hexclave approval authentication failed."
        ) from last


class HexclavePermissionChecker:
    """Checks current team permissions and briefly caches positive and negative results."""

    def __init__(
        self,
        *,
        project_id: str | None,
        secret_key: str | None,
        team_id: str | None,
        api_url: str = HEXCLAVE_DEFAULT_API_URL,
        transport: HexclaveTransport | None = None,
        cache_ttl_seconds: float = HEXCLAVE_DEFAULT_CACHE_TTL_SECONDS,
        timeout_seconds: float = HEXCLAVE_DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._project_id = _required_value("project ID", project_id)
        self._secret_key = _required_value("secret server key", secret_key)
        self._team_id = _required_value("team ID", team_id)
        if not isfinite(cache_ttl_seconds) or cache_ttl_seconds <= 0:
            raise HexclaveConfigurationError(
                "The Hexclave permission cache TTL must be greater than zero."
            )
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise HexclaveConfigurationError(
                "The Hexclave request timeout must be greater than zero."
            )
        self._transport = transport or HttpxHexclaveTransport(api_url=api_url)
        self._cache_ttl_seconds = cache_ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._lock = RLock()

    def has_permission(self, *, user_id: str, permission_id: str) -> bool:
        normalized_user_id = _required_value("user ID", user_id)
        normalized_permission_id = _required_value("permission ID", permission_id)
        cache_key = (normalized_user_id, normalized_permission_id)

        with self._lock:
            now = self._clock()
            cached = self._cache.get(cache_key)
            if cached is not None and cached.expires_at > now:
                return cached.allowed

            response = self._transport.get_team_permission(
                TeamPermissionRequest(
                    project_id=self._project_id,
                    team_id=self._team_id,
                    user_id=normalized_user_id,
                    permission_id=normalized_permission_id,
                ),
                secret_key=self._secret_key,
                timeout_seconds=self._timeout_seconds,
            )
            if (
                not isinstance(response, TeamPermissionResponse)
                or not isinstance(response.allowed, bool)
            ):
                raise HexclavePermissionError(
                    "Hexclave transport returned an invalid permission response."
                )
            self._cache[cache_key] = _CacheEntry(
                allowed=response.allowed,
                expires_at=now + self._cache_ttl_seconds,
            )
            return response.allowed

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()


def _required_value(label: str, value: str | None) -> str:
    if value is None:
        raise HexclaveConfigurationError(f"The Hexclave {label} is missing.")
    normalized = value.strip()
    if not normalized:
        raise HexclaveConfigurationError(f"The Hexclave {label} is missing.")
    return normalized


def _permission_value(
    payload: object,
    *,
    request: TeamPermissionRequest,
) -> bool | None:
    """Parse Hexclave's documented ``TeamPermissionCrudList`` response.

    The endpoint is queried with the complete team/user/permission tuple.  An
    empty result is a deterministic deny; an allow requires exactly one item
    matching that tuple.  Compatibility guesses such as bare booleans or an
    uncorrelated ``allowed`` field are intentionally rejected.
    """

    if not isinstance(payload, Mapping):
        return None
    if set(payload) - {"items", "pagination", "is_paginated"}:
        return None
    items = payload.get("items")
    if not isinstance(items, list):
        return None
    is_paginated = payload.get("is_paginated", False)
    if not isinstance(is_paginated, bool) or is_paginated:
        return None
    pagination = payload.get("pagination")
    if pagination is not None:
        if not isinstance(pagination, Mapping):
            return None
        if set(pagination) - {"next_cursor"}:
            return None
        if pagination.get("next_cursor") is not None:
            return None
    if not items:
        return False
    if len(items) != 1:
        return None
    item = items[0]
    if not isinstance(item, Mapping):
        return None
    expected = {
        "id": request.permission_id,
        "user_id": request.user_id,
        "team_id": request.team_id,
    }
    for key, expected_value in expected.items():
        value = item.get(key)
        if not isinstance(value, str) or value != expected_value:
            return None
    return True
