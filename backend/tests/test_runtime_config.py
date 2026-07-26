from __future__ import annotations

from dataclasses import replace

import pytest
from writai import config
from writai import runtime as runtime_module
from writai.domain import Artifact, Edge
from writai.graph.memory import MemoryGraphStore
from writai.services import agent_api


class ResetTrackingStore(MemoryGraphStore):
    def __init__(self) -> None:
        super().__init__()
        self.reset_calls = 0

    def reset(self, *, version: int, artifacts: list[Artifact], edges: list[Edge]) -> None:
        self.reset_calls += 1
        super().reset(version=version, artifacts=artifacts, edges=edges)


def test_memory_development_keeps_zero_config_demo_reset() -> None:
    assert config._default_demo_reset_enabled("development", "memory") is True


def test_notification_settings_expose_safe_local_and_durable_controls() -> None:
    configured = replace(
        config.settings,
        public_base_url="http://localhost:8001",
        approval_link_secret="a-distinct-test-secret",
        approval_link_ttl_seconds=900,
        approval_link_store=".writai/approval-link-uses.json",
        approval_recipient_bindings=None,
        ntfy_server="https://ntfy.sh",
        ntfy_topic=None,
        pushover_app_token=None,
        pushover_user_key=None,
        resend_api_key=None,
        email_from=None,
    )

    assert configured.public_base_url == "http://localhost:8001"
    assert configured.approval_link_ttl_seconds == 900
    assert configured.approval_link_store == (
        ".writai/approval-link-uses.json"
    )
    assert configured.approval_recipient_bindings is None
    assert configured.ntfy_server == "https://ntfy.sh"
    assert configured.ntfy_topic is None
    assert configured.pushover_app_token is None
    assert configured.pushover_user_key is None
    assert configured.resend_api_key is None
    assert configured.email_from is None


@pytest.mark.parametrize("environment", ["development", "demo", "local", "test", "production"])
def test_neo4j_never_enables_destructive_reset_by_default(environment: str) -> None:
    assert config._default_demo_reset_enabled(environment, "neo4j") is False


def test_explicit_reset_flag_can_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WRITAI_DEMO_RESET_ENABLED", "true")

    assert config._env_flag("WRITAI_DEMO_RESET_ENABLED", False) is True


@pytest.mark.parametrize(
    ("reset_enabled", "expected_calls", "expected_version"),
    [(False, 0, 0), (True, 1, 17)],
)
def test_authority_startup_only_seeds_when_reset_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
    reset_enabled: bool,
    expected_calls: int,
    expected_version: int,
) -> None:
    graph = ResetTrackingStore()
    monkeypatch.setattr(
        runtime_module,
        "settings",
        replace(
            runtime_module.settings,
            graph_backend="neo4j",
            demo_reset_enabled=reset_enabled,
        ),
    )
    monkeypatch.setattr(runtime_module, "create_graph_store", lambda _settings: graph)

    created = runtime_module.create_authority_runtime()

    assert graph.reset_calls == expected_calls
    assert created.graph.version == expected_version


def test_agent_hexclave_factories_use_configured_transport_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker_arguments: list[dict[str, object]] = []
    access_resolver_arguments: list[dict[str, object]] = []
    api_key_resolver_arguments: list[dict[str, object]] = []
    chained_resolver_arguments: list[tuple[object, ...]] = []
    checker = object()
    access_resolver = object()
    api_key_resolver = object()
    resolver = object()

    def checker_factory(**kwargs: object) -> object:
        checker_arguments.append(kwargs)
        return checker

    def access_resolver_factory(**kwargs: object) -> object:
        access_resolver_arguments.append(kwargs)
        return access_resolver

    def api_key_resolver_factory(**kwargs: object) -> object:
        api_key_resolver_arguments.append(kwargs)
        return api_key_resolver

    def chained_resolver_factory(*resolvers: object) -> object:
        chained_resolver_arguments.append(resolvers)
        return resolver

    monkeypatch.setattr(
        agent_api,
        "settings",
        replace(
            agent_api.settings,
            hexclave_api_url="https://verified.hexclave.example/api/v1",
            hexclave_permission_cache_ttl_seconds=17,
        ),
    )
    monkeypatch.setattr(agent_api, "hexclave_permission_checkers", {})
    monkeypatch.setattr(agent_api, "hexclave_identity_resolvers", {})
    monkeypatch.setattr(
        agent_api,
        "HexclavePermissionChecker",
        checker_factory,
    )
    monkeypatch.setattr(
        agent_api,
        "HexclaveAccessTokenIdentityResolver",
        access_resolver_factory,
    )
    monkeypatch.setattr(
        agent_api,
        "HexclaveUserApiKeyIdentityResolver",
        api_key_resolver_factory,
    )
    monkeypatch.setattr(
        agent_api,
        "ChainedHexclaveIdentityResolver",
        chained_resolver_factory,
    )

    first_checker = agent_api._workspace_permission_checker(
        {"slack_binding": {"hexclave_team_id": "hex-team-workspace"}}
    )
    second_checker = agent_api._workspace_permission_checker(
        {"slack_binding": {"hexclave_team_id": "hex-team-workspace"}}
    )
    first_resolver = agent_api._approval_identity_resolver()
    second_resolver = agent_api._approval_identity_resolver()

    assert first_checker is second_checker is checker
    assert first_resolver is second_resolver is resolver
    assert len(checker_arguments) == 1
    assert len(access_resolver_arguments) == 1
    assert len(api_key_resolver_arguments) == 1
    assert chained_resolver_arguments == [(access_resolver, api_key_resolver)]
    assert checker_arguments[0]["team_id"] == "hex-team-workspace"
    assert checker_arguments[0]["api_url"] == (
        "https://verified.hexclave.example/api/v1"
    )
    assert checker_arguments[0]["cache_ttl_seconds"] == 17
    assert access_resolver_arguments[0]["api_url"] == (
        "https://verified.hexclave.example/api/v1"
    )
    assert api_key_resolver_arguments[0]["api_url"] == (
        "https://verified.hexclave.example/api/v1"
    )
