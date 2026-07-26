from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest
from dragback.auth import hexclave as hexclave_module
from dragback.auth.hexclave import (
    HEXCLAVE_TEAM_PERMISSIONS_PATH,
    HEXCLAVE_USER_API_KEY_CHECK_PATH,
    HexclaveConfigurationError,
    HexclavePermissionChecker,
    HexclavePermissionError,
    HexclaveUserApiKeyIdentityResolver,
    HttpxHexclaveTransport,
    TeamPermissionRequest,
    TeamPermissionResponse,
)


@dataclass
class MutableClock:
    value: float = 0

    def __call__(self) -> float:
        return self.value


class RecordingTransport:
    def __init__(self, permissions: dict[tuple[str, str], bool]) -> None:
        self.permissions = permissions
        self.requests: list[TeamPermissionRequest] = []
        self.secret_keys: list[str] = []
        self.timeouts: list[float] = []

    def get_team_permission(
        self,
        request: TeamPermissionRequest,
        *,
        secret_key: str,
        timeout_seconds: float,
    ) -> TeamPermissionResponse:
        self.requests.append(request)
        self.secret_keys.append(secret_key)
        self.timeouts.append(timeout_seconds)
        return TeamPermissionResponse(
            allowed=self.permissions[(request.user_id, request.permission_id)]
        )


def make_checker(
    transport: RecordingTransport,
    *,
    clock: MutableClock | None = None,
    cache_ttl_seconds: float = 60,
) -> HexclavePermissionChecker:
    return HexclavePermissionChecker(
        project_id="project-001",
        secret_key="secret-server-key",
        team_id="team-001",
        transport=transport,
        cache_ttl_seconds=cache_ttl_seconds,
        timeout_seconds=2,
        clock=clock or MutableClock(),
    )


@pytest.mark.parametrize("allowed", [True, False])
def test_permission_checker_caches_positive_and_negative_results(allowed: bool) -> None:
    clock = MutableClock()
    transport = RecordingTransport({("user-001", "approve_compliance"): allowed})
    checker = make_checker(transport, clock=clock)

    first = checker.has_permission(
        user_id="user-001",
        permission_id="approve_compliance",
    )
    clock.value = 59
    second = checker.has_permission(
        user_id="user-001",
        permission_id="approve_compliance",
    )

    assert first is allowed
    assert second is allowed
    assert len(transport.requests) == 1
    assert transport.requests[0] == TeamPermissionRequest(
        project_id="project-001",
        team_id="team-001",
        user_id="user-001",
        permission_id="approve_compliance",
    )


def test_permission_cache_expires_and_is_keyed_by_user_and_permission() -> None:
    clock = MutableClock()
    transport = RecordingTransport(
        {
            ("user-001", "approve_compliance"): True,
            ("user-002", "approve_compliance"): False,
            ("user-001", "approve_security"): False,
        }
    )
    checker = make_checker(transport, clock=clock, cache_ttl_seconds=5)

    assert checker.has_permission(
        user_id="user-001", permission_id="approve_compliance"
    )
    assert not checker.has_permission(
        user_id="user-002", permission_id="approve_compliance"
    )
    assert not checker.has_permission(
        user_id="user-001", permission_id="approve_security"
    )
    clock.value = 5
    assert checker.has_permission(
        user_id="user-001", permission_id="approve_compliance"
    )

    assert len(transport.requests) == 4


def test_clear_cache_forces_a_fresh_permission_lookup() -> None:
    transport = RecordingTransport({("user-001", "approve_compliance"): True})
    checker = make_checker(transport)
    assert checker.has_permission(
        user_id="user-001", permission_id="approve_compliance"
    )

    checker.clear_cache()

    assert checker.has_permission(
        user_id="user-001", permission_id="approve_compliance"
    )
    assert len(transport.requests) == 2


def test_http_transport_uses_team_permissions_get_without_secret_in_query() -> None:
    captured_request: httpx.Request | None = None

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "approve_compliance",
                        "user_id": "user-001",
                        "team_id": "team-001",
                    }
                ],
                "is_paginated": False,
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handle))
    transport = HttpxHexclaveTransport(
        api_url="https://hexclave.example/api/v1",
        http_client=http_client,
    )

    response = transport.get_team_permission(
        TeamPermissionRequest(
            project_id="project-001",
            team_id="team-001",
            user_id="user-001",
            permission_id="approve_compliance",
        ),
        secret_key="secret-server-key",
        timeout_seconds=2,
    )

    assert response == TeamPermissionResponse(allowed=True)
    assert captured_request is not None
    assert captured_request.method == "GET"
    assert captured_request.url.path == f"/api/v1{HEXCLAVE_TEAM_PERMISSIONS_PATH}"
    assert dict(captured_request.url.params) == {
        "team_id": "team-001",
        "user_id": "user-001",
        "permission_id": "approve_compliance",
        "recursive": "true",
    }
    assert captured_request.headers["x-hexclave-access-type"] == "server"
    assert captured_request.headers["x-hexclave-project-id"] == "project-001"
    assert (
        captured_request.headers["x-hexclave-secret-server-key"]
        == "secret-server-key"
    )
    assert "authorization" not in captured_request.headers
    assert "secret-server-key" not in str(captured_request.url)


@pytest.mark.parametrize(
    "payload",
    [
        True,
        {"allowed": True},
        {"allowed": "true"},
        {
            "permission_id": "approve_security",
            "allowed": True,
        },
        {
            "allowed": True,
            "data": {
                "permission_id": "approve_compliance",
                "allowed": False,
            },
        },
        {
            "data": {
                "permission_id": "approve_compliance",
                "allowed": True,
            },
            "permission": {
                "permission_id": "approve_compliance",
                "allowed": False,
            },
        },
        {
            "items": [
                {
                    "id": "approve_compliance",
                    "user_id": "other-user",
                    "team_id": "team-001",
                }
            ]
        },
        {
            "items": [
                {
                    "id": "approve_compliance",
                    "user_id": "user-001",
                    "team_id": "other-team",
                }
            ]
        },
        {
            "items": [
                {
                    "id": "approve_security",
                    "user_id": "user-001",
                    "team_id": "team-001",
                }
            ]
        },
        {"items": [{"id": "approve_compliance"}]},
        {"items": "not-a-list"},
        {"items": [], "is_paginated": True},
        {"items": [], "pagination": {"next_cursor": "page-2"}},
        {
            "items": [
                {
                    "id": "approve_compliance",
                    "user_id": "user-001",
                    "team_id": "team-001",
                },
                {
                    "id": "approve_compliance",
                    "user_id": "user-001",
                    "team_id": "team-001",
                },
            ]
        },
    ],
)
def test_http_transport_rejects_uncorrelated_or_ambiguous_permission_payload(
    payload: object,
) -> None:
    transport = HttpxHexclaveTransport(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=payload)
            )
        )
    )

    with pytest.raises(
        HexclavePermissionError,
        match="invalid permission response",
    ):
        transport.get_team_permission(
            TeamPermissionRequest(
                project_id="project-001",
                team_id="team-001",
                user_id="user-001",
                permission_id="approve_compliance",
            ),
            secret_key="secret-server-key",
            timeout_seconds=2,
        )


def test_http_transport_treats_an_empty_correlated_result_as_denied() -> None:
    transport = HttpxHexclaveTransport(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"items": [], "is_paginated": False},
                )
            )
        )
    )

    response = transport.get_team_permission(
        TeamPermissionRequest(
            project_id="project-001",
            team_id="team-001",
            user_id="user-001",
            permission_id="approve_compliance",
        ),
        secret_key="secret-server-key",
        timeout_seconds=2,
    )

    assert response == TeamPermissionResponse(allowed=False)


def test_permission_checker_rejects_non_boolean_transport_result() -> None:
    class InvalidTransport:
        def get_team_permission(
            self,
            request: TeamPermissionRequest,
            *,
            secret_key: str,
            timeout_seconds: float,
        ) -> TeamPermissionResponse:
            return TeamPermissionResponse(allowed="true")  # type: ignore[arg-type]

    checker = HexclavePermissionChecker(
        project_id="project-001",
        secret_key="secret-server-key",
        team_id="team-001",
        transport=InvalidTransport(),
    )

    with pytest.raises(HexclavePermissionError, match="invalid permission response"):
        checker.has_permission(
            user_id="user-001",
            permission_id="approve_compliance",
        )


def test_permission_checker_builds_default_transport_with_configured_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecordingTransport(
        {("user-001", "approve_compliance"): True}
    )
    captured_urls: list[str] = []

    def transport_factory(*, api_url: str) -> RecordingTransport:
        captured_urls.append(api_url)
        return transport

    monkeypatch.setattr(
        hexclave_module,
        "HttpxHexclaveTransport",
        transport_factory,
    )
    checker = HexclavePermissionChecker(
        project_id="project-001",
        secret_key="secret-server-key",
        team_id="team-001",
        api_url="https://verified.hexclave.example/api/v1",
    )

    assert checker.has_permission(
        user_id="user-001",
        permission_id="approve_compliance",
    )
    assert captured_urls == ["https://verified.hexclave.example/api/v1"]


def test_user_api_key_identity_is_resolved_with_server_auth_headers() -> None:
    captured: httpx.Request | None = None

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured = request
        return httpx.Response(
            200,
            json={
                "type": "user",
                "user_id": "user-001",
                "is_public": False,
            },
        )

    resolver = HexclaveUserApiKeyIdentityResolver(
        project_id="project-001",
        secret_key="secret-server-key",
        api_url="https://hexclave.example/api/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        timeout_seconds=2,
    )

    assert resolver.resolve_user_id(approval_token="user-api-key") == "user-001"
    assert captured is not None
    assert captured.method == "POST"
    assert captured.url.path == f"/api/v1{HEXCLAVE_USER_API_KEY_CHECK_PATH}"
    assert captured.headers["x-hexclave-access-type"] == "server"
    assert captured.headers["x-hexclave-project-id"] == "project-001"
    assert (
        captured.headers["x-hexclave-secret-server-key"]
        == "secret-server-key"
    )
    assert not captured.url.query
    assert captured.content == b'{"api_key":"user-api-key"}'


@pytest.mark.parametrize(
    "payload",
    [
        True,
        {},
        {"type": "team", "team_id": "team-001", "is_public": False},
        {"type": "user", "user_id": "", "is_public": False},
        {"type": "user", "user_id": "user-001"},
        {"type": "user", "user_id": "user-001", "is_public": True},
    ],
)
def test_user_api_key_identity_rejects_nonhuman_or_ambiguous_results(
    payload: object,
) -> None:
    resolver = HexclaveUserApiKeyIdentityResolver(
        project_id="project-001",
        secret_key="secret-server-key",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=payload)
            )
        ),
    )

    with pytest.raises(
        HexclavePermissionError,
        match="invalid approval identity",
    ):
        resolver.resolve_user_id(approval_token="user-api-key")


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_user_api_key_identity_fails_closed_on_http_errors(
    status_code: int,
) -> None:
    resolver = HexclaveUserApiKeyIdentityResolver(
        project_id="project-001",
        secret_key="secret-server-key",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(status_code)
            )
        ),
    )

    with pytest.raises(
        HexclavePermissionError,
        match="authentication failed",
    ):
        resolver.resolve_user_id(approval_token="user-api-key")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", ""),
        ("project_id", None),
        ("secret_key", " "),
        ("team_id", ""),
        ("cache_ttl_seconds", 0),
        ("timeout_seconds", -1),
    ],
)
def test_checker_rejects_missing_or_unsafe_configuration(
    field: str,
    value: str | float | None,
) -> None:
    arguments: dict[str, object] = {
        "project_id": "project-001",
        "secret_key": "secret-server-key",
        "team_id": "team-001",
        "transport": RecordingTransport({}),
        "cache_ttl_seconds": 60,
        "timeout_seconds": 2,
    }
    arguments[field] = value

    with pytest.raises(HexclaveConfigurationError):
        HexclavePermissionChecker(**arguments)  # type: ignore[arg-type]
