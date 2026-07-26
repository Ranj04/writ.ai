"""The browser SDK's access token resolves to the same user the CLI's key does.

`@hexclave/react` signs a person in and hands the page an `Authorization`
header. That is an ACCESS TOKEN, not a user API key, and it does not resolve
through `/user-api-keys/check` — posting one there is rejected, which would read
as "unauthorised" for a person who is in fact signed in.

So the web surface gets its own resolver, and a chained resolver lets the server
accept either artifact without asking the caller to declare which it holds. What
must NOT change is where it ends up: the same permission check, the same user
id, the same audit record.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from writai.auth.hexclave import (
    ChainedHexclaveIdentityResolver,
    HexclaveAccessTokenIdentityResolver,
    HexclaveConfigurationError,
    HexclavePermissionError,
    HexclaveUserApiKeyIdentityResolver,
)

PROJECT = "proj-1"
SECRET = "secret-server-key"


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _access_resolver(handler: Any) -> HexclaveAccessTokenIdentityResolver:
    return HexclaveAccessTokenIdentityResolver(
        project_id=PROJECT,
        secret_key=SECRET,
        http_client=_client(handler),
    )


def test_a_signed_in_access_token_resolves_to_its_user() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"id": "user-42", "is_anonymous": False})

    resolver = _access_resolver(handler)
    assert resolver.resolve_user_id(approval_token="tok-live") == "user-42"

    assert seen["url"].endswith("/users/me")
    # The documented header trio, and the secret never travels in the body.
    assert seen["headers"]["x-stack-access-token"] == "tok-live"
    assert seen["headers"]["x-stack-project-id"] == PROJECT
    assert seen["headers"]["x-stack-secret-server-key"] == SECRET


def test_an_anonymous_session_cannot_approve() -> None:
    """A session is not a person. Anonymous resolves to nobody."""

    resolver = _access_resolver(
        lambda _r: httpx.Response(200, json={"id": "anon-1", "is_anonymous": True})
    )
    with pytest.raises(HexclavePermissionError, match="anonymous"):
        resolver.resolve_user_id(approval_token="tok")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"id": ""},
        {"id": "   "},
        {"id": 42},
        [],
        "not-an-object",
    ],
)
def test_an_unusable_identity_response_is_refused(payload: Any) -> None:
    resolver = _access_resolver(lambda _r: httpx.Response(200, json=payload))
    with pytest.raises(HexclavePermissionError):
        resolver.resolve_user_id(approval_token="tok")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 503])
def test_a_rejected_token_fails_closed(status: int) -> None:
    """Never fall back to a default identity when Hexclave says no."""

    resolver = _access_resolver(lambda _r: httpx.Response(status, json={}))
    with pytest.raises(HexclavePermissionError):
        resolver.resolve_user_id(approval_token="tok")


def test_a_transport_failure_fails_closed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    with pytest.raises(HexclavePermissionError):
        _access_resolver(handler).resolve_user_id(approval_token="tok")


def test_an_empty_token_is_refused_without_a_call() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"id": "user-1"})

    with pytest.raises(HexclavePermissionError):
        _access_resolver(handler).resolve_user_id(approval_token="   ")
    assert called is False


def test_partial_configuration_fails_closed_at_construction() -> None:
    for project, secret in ((None, SECRET), (PROJECT, None), (None, None)):
        with pytest.raises(HexclaveConfigurationError):
            HexclaveAccessTokenIdentityResolver(project_id=project, secret_key=secret)
    with pytest.raises(HexclaveConfigurationError):
        HexclaveAccessTokenIdentityResolver(
            project_id=PROJECT, secret_key=SECRET, api_url="http://insecure.example"
        )


# --------------------------------------------------------------------------------------
# The chain: either artifact, one user id
# --------------------------------------------------------------------------------------


def _api_key_resolver(handler: Any) -> HexclaveUserApiKeyIdentityResolver:
    return HexclaveUserApiKeyIdentityResolver(
        project_id=PROJECT,
        secret_key=SECRET,
        http_client=_client(handler),
    )


def test_the_chain_accepts_either_artifact_without_being_told_which() -> None:
    """The caller holds one opaque token. Hexclave decides what it is."""

    def api_key_only(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user-api-keys/check"):
            return httpx.Response(
                200, json={"user_id": "user-cli", "type": "user", "is_public": False}
            )
        return httpx.Response(401, json={})

    def access_token_only(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me"):
            return httpx.Response(200, json={"id": "user-web", "is_anonymous": False})
        return httpx.Response(401, json={})

    from_key = ChainedHexclaveIdentityResolver(
        _access_resolver(api_key_only), _api_key_resolver(api_key_only)
    )
    from_token = ChainedHexclaveIdentityResolver(
        _access_resolver(access_token_only), _api_key_resolver(access_token_only)
    )

    assert from_key.resolve_user_id(approval_token="key") == "user-cli"
    assert from_token.resolve_user_id(approval_token="tok") == "user-web"


def test_the_chain_refuses_a_token_no_resolver_recognises() -> None:
    """An unresolvable token must not fall through to a default identity."""

    reject = lambda _r: httpx.Response(401, json={})  # noqa: E731
    chain = ChainedHexclaveIdentityResolver(
        _access_resolver(reject), _api_key_resolver(reject)
    )
    with pytest.raises(HexclavePermissionError):
        chain.resolve_user_id(approval_token="garbage")


def test_the_chain_requires_at_least_one_resolver() -> None:
    """An empty chain would resolve nothing and silently approve nobody."""

    with pytest.raises(HexclaveConfigurationError):
        ChainedHexclaveIdentityResolver()


def test_the_chain_stops_at_the_first_success() -> None:
    """No unnecessary second lookup once the identity is known."""

    calls: list[str] = []

    def access(request: httpx.Request) -> httpx.Response:
        calls.append("access")
        return httpx.Response(200, json={"id": "user-web"})

    def api_key(request: httpx.Request) -> httpx.Response:
        calls.append("api-key")
        return httpx.Response(200, json={"user_id": "user-cli", "type": "user", "is_public": False})

    chain = ChainedHexclaveIdentityResolver(
        _access_resolver(access), _api_key_resolver(api_key)
    )
    assert chain.resolve_user_id(approval_token="tok") == "user-web"
    assert calls == ["access"]
