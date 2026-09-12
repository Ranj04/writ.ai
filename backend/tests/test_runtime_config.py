from __future__ import annotations

import os
import pathlib
import re
import secrets
import subprocess
import sys
from dataclasses import replace

import pytest
from writai import config
from writai import runtime as runtime_module
from writai.config import Settings
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


#: Variables .env.example documents that are read somewhere other than ``Settings``.
#: Enumerated literally so a new one has to be justified here, not pattern-matched away.
_READ_OUTSIDE_SETTINGS = frozenset(
    {
        # hooks/ scripts and the supervisor service read these from the environment directly
        "WRITAI_HOOK_API_KEY",
        # supervisor_api.parse_hook_credentials reads the per-developer map, not Settings
        "WRITAI_HOOK_API_KEYS",
        # writai doctor probes read these; they are not runtime configuration
        "COMPOSIO_SLACK_AUTH_CONFIG_ID",
        "WRITAI_SLACK_CHANNEL_ID",
        # writai approve reads the human approver's private key itself
        "HEXCLAVE_APPROVER_USER_API_KEY",
        # scripts/demo/lib.sh, demo infrastructure only
        "SUPERSET_API_KEY",
        # the frontend build reads VITE_* through vite, never through Settings
        "VITE_AUTHORITY_URL",
        "VITE_AGENT_URL",
        "VITE_EXECUTOR_URL",
        "VITE_HEXCLAVE_PROJECT_ID",
        "VITE_WRITAI_HEXCLAVE_SIGN_IN",
        # the Neo4j driver set, also consumed by docker-compose and the neo4j CI job
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NEO4J_DATABASE",
    }
)


def test_every_documented_env_var_has_a_settings_field() -> None:
    """config.py is frozen after T0.3: a variable documented without a field is a drift."""

    example = pathlib.Path(".env.example").read_text()
    names = re.findall(r"^([A-Z][A-Z0-9_]*)=", example, flags=re.MULTILINE)
    assert len(names) >= 30, "the .env.example regex stopped matching; the test is vacuous"

    source = pathlib.Path("backend/writai/config.py").read_text()
    undocumented_in_settings = [
        name for name in names if name not in _READ_OUTSIDE_SETTINGS and name not in source
    ]

    assert undocumented_in_settings == []


def test_production_refuses_to_start_on_the_demo_signing_secret() -> None:
    production = replace(
        Settings(), env="production", grant_secret=config.DEFAULT_DEMO_GRANT_SECRET
    )

    with pytest.raises(RuntimeError, match="WRITAI_GRANT_SECRET"):
        config.require_production_secrets(production)


def test_production_refuses_a_short_signing_secret() -> None:
    production = replace(Settings(), env="production", grant_secret="short")

    with pytest.raises(RuntimeError, match="WRITAI_GRANT_SECRET"):
        config.require_production_secrets(production)


def test_development_keeps_the_zero_config_demo_secret() -> None:
    accepted: list[str] = []
    for environment in ("development", "demo", "local", "test"):
        demo = replace(
            Settings(), env=environment, grant_secret=config.DEFAULT_DEMO_GRANT_SECRET
        )
        config.require_production_secrets(demo)
        accepted.append(environment)

    assert accepted == ["development", "demo", "local", "test"]


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _fresh_python(code: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter so module-scope guards actually execute.

    ``WRITAI_GRANT_SECRET`` is pinned to the empty string so a developer's own
    ``.env`` cannot supply a real secret and make the refusal case pass vacuously;
    ``load_dotenv`` never overrides a variable that is already present.
    """

    child = {
        **os.environ,
        "PYTHONPATH": str(_REPO_ROOT / "backend"),
        "WRITAI_GRANT_SECRET": "",
        **env,
    }
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        env=child,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("service", ["authority_api", "agent_api", "executor_api"])
def test_each_service_refuses_to_import_in_production_on_the_default_secret(
    service: str,
) -> None:
    """The guard runs at module scope: the process must never bind a port on the default."""

    completed = _fresh_python(f"import writai.services.{service}", WRITAI_ENV="production")

    assert completed.returncode != 0, completed.stdout
    assert "RuntimeError" in completed.stderr
    assert "WRITAI_GRANT_SECRET" in completed.stderr


def test_an_empty_grant_secret_falls_back_to_the_demo_default() -> None:
    """A copied .env.example leaves the variable set but empty; that must not crash a demo."""

    completed = _fresh_python(
        "from writai.config import settings; print(settings.grant_secret)",
        WRITAI_ENV="development",
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == config.DEFAULT_DEMO_GRANT_SECRET


def test_production_accepts_a_real_signing_secret() -> None:
    production = replace(
        Settings(), env="production", grant_secret=secrets.token_urlsafe(48)
    )

    config.require_production_secrets(production)

    assert production.grant_secret != config.DEFAULT_DEMO_GRANT_SECRET


def _placeholders_the_repo_publishes() -> list[str]:
    """Every non-empty ``*_SECRET`` / ``*_KEY`` value in ``.env.example``, read here
    independently of config.py so the test cannot inherit a bug in the helper."""

    example = pathlib.Path(__file__).resolve().parents[2] / ".env.example"
    published = re.findall(
        r"^([A-Z][A-Z0-9_]*(?:_SECRET|_KEY))=(.+)$", example.read_text(), flags=re.MULTILINE
    )
    return sorted({value.strip() for _name, value in published if value.strip()})


@pytest.mark.parametrize(
    "placeholder",
    [*_placeholders_the_repo_publishes(), "replace-this-for-any-shared-demo"],
)
def test_production_refuses_every_placeholder_the_repo_publishes(placeholder: str) -> None:
    """A placeholder the repository itself has shipped is a publicly known signing key.

    Parametrised over `.env.example`, so a placeholder added there later is refused
    without anyone editing this test; the literal is the value `main` shipped before
    T0 emptied it.
    """

    production = replace(Settings(), env="production", grant_secret=placeholder)

    with pytest.raises(RuntimeError, match="WRITAI_GRANT_SECRET"):
        config.require_production_secrets(production)


def test_production_refuses_a_whitespace_only_secret() -> None:
    """Padding must not manufacture length: 40 spaces is longer than the minimum."""

    production = replace(Settings(), env="production", grant_secret=" " * 40)

    with pytest.raises(RuntimeError, match="WRITAI_GRANT_SECRET"):
        config.require_production_secrets(production)


def test_production_refuses_a_comment_shaped_secret() -> None:
    """python-dotenv hands an inline comment back as the value; a comment is never a secret."""

    comment = "# " + secrets.token_urlsafe(48)
    production = replace(Settings(), env="production", grant_secret=comment)

    with pytest.raises(RuntimeError, match="WRITAI_GRANT_SECRET"):
        config.require_production_secrets(production)


def test_production_still_accepts_a_real_random_secret() -> None:
    """The guard against over-correcting: a generated secret must still boot."""

    production = replace(
        Settings(), env="production", grant_secret=secrets.token_urlsafe(48)
    )

    config.require_production_secrets(production)

    assert production.grant_secret.strip() not in config.PUBLISHED_PLACEHOLDER_SECRETS


def test_placeholder_set_falls_back_to_the_demo_default_without_env_example(
    tmp_path: pathlib.Path,
) -> None:
    """An installed wheel has no `.env.example`; the demo default is refused all the same."""

    published = config._published_placeholder_secrets(tmp_path / "absent.env.example")

    assert published == frozenset(
        {config.DEFAULT_DEMO_GRANT_SECRET, config.RETIRED_PUBLISHED_GRANT_SECRET}
    )
    assert config.PUBLISHED_PLACEHOLDER_SECRETS >= published


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
