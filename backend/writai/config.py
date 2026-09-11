from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv


def _load_env() -> str:
    """Load the `.env` an operator would expect: the one where they are standing.

    Bare `load_dotenv()` resolves the file by walking up from **this file**, not
    from the working directory. Once the package is installed into a venv that
    lives in a different checkout, that search never reaches the operator's tree
    — it walked past `/…/writai-verify/` and returned nothing while a fully
    populated `.env` sat in the cwd.

    That failure is silent and it reads as reassuring: `writai doctor` reports
    every integration `[ ---- ] not configured` and exits 0, which looks like a
    clean preflight and proves nothing. It is exactly the "looks identical to
    working" failure this project keeps having to design against, so the cwd is
    tried first and the package-relative search is kept only as a fallback.
    """

    found = find_dotenv(usecwd=True) or find_dotenv()
    if found:
        load_dotenv(found)
    return found


DOTENV_PATH = _load_env()
_ENVIRONMENT = os.getenv("WRITAI_ENV", "development")
_GRAPH_BACKEND = os.getenv("WRITAI_GRAPH_BACKEND", "memory")
DEFAULT_AUTHORITY_THRESHOLD = 0.75
DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_VENICE_BASE_URL = "https://api.venice.ai/api/v1"
DEFAULT_VENICE_MODEL = "openai-gpt-4o-mini-2024-07-18"
DEFAULT_HEXCLAVE_API_URL = "https://api.hexclave.com/api/v1"
# The zero-config signing secret. Fine on a demo machine; refused everywhere else by
# `require_production_secrets`, because anyone who has read this file can mint a grant
# with it.
DEFAULT_DEMO_GRANT_SECRET = "writai-local-demo-secret"
# The one definition of "this is a demo machine". Shared by the demo-reset default and
# the production secret guard so the two can never disagree.
DEMO_ENVIRONMENTS = frozenset({"development", "demo", "local", "test"})
MIN_GRANT_SECRET_LENGTH = 32


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_demo_reset_enabled(environment: str, graph_backend: str) -> bool:
    """Keep the memory demo zero-config without defaulting remote stores to destructive writes."""

    return (
        graph_backend.strip().lower() == "memory"
        and environment.strip().lower() in DEMO_ENVIRONMENTS
    )


@dataclass(frozen=True)
class Settings:
    env: str = _ENVIRONMENT
    demo_reset_enabled: bool = _env_flag(
        "WRITAI_DEMO_RESET_ENABLED",
        _default_demo_reset_enabled(_ENVIRONMENT, _GRAPH_BACKEND),
    )
    graph_backend: str = _GRAPH_BACKEND
    grant_secret: str = os.getenv("WRITAI_GRANT_SECRET", DEFAULT_DEMO_GRANT_SECRET)
    grant_ttl_seconds: int = int(os.getenv("WRITAI_GRANT_TTL_SECONDS", "3600"))
    authority_threshold: float = float(
        os.getenv("WRITAI_AUTHORITY_THRESHOLD", str(DEFAULT_AUTHORITY_THRESHOLD))
    )
    # Normalized like the other base URLs below: every caller appends "/path", so a
    # configured trailing slash would produce "//path" and 404 every service call.
    authority_url: str = os.getenv(
        "WRITAI_AUTHORITY_URL", "http://localhost:8001"
    ).rstrip("/")
    agent_url: str = os.getenv("WRITAI_AGENT_URL", "http://localhost:8002").rstrip("/")
    executor_url: str = os.getenv(
        "WRITAI_EXECUTOR_URL", "http://localhost:8003"
    ).rstrip("/")
    service_timeout_seconds: float = float(os.getenv("WRITAI_SERVICE_TIMEOUT_SECONDS", "5"))
    log_level: str = os.getenv("WRITAI_LOG_LEVEL", "INFO")
    # The public intake limiter is per uvicorn worker: N workers allow N x this value.
    rate_limit_enabled: bool = _env_flag("WRITAI_RATE_LIMIT_ENABLED", True)
    public_intake_rate_limit_per_minute: int = int(
        os.getenv("WRITAI_PUBLIC_INTAKE_RATE_LIMIT_PER_MINUTE", "60")
    )
    execution_provider: str = os.getenv("WRITAI_EXECUTION_PROVIDER", "fixture").strip().lower()
    # A .json suffix selects the legacy JsonFileLiveWorkspaceRepository; any other suffix
    # selects the SQLite store, which migrates an existing sibling .json store on first start.
    workspace_store: str = os.getenv(
        "WRITAI_WORKSPACE_STORE",
        ".writai/live-workspaces.sqlite3",
    )
    # Upper bound on live per-workspace authority contexts held in memory at once.
    max_authority_contexts: int = int(os.getenv("WRITAI_MAX_AUTHORITY_CONTEXTS", "256"))
    neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_username: str = os.getenv("NEO4J_USERNAME", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "writai-demo")
    neo4j_database: str = os.getenv("NEO4J_DATABASE", "neo4j")
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY") or None
    gemini_model: str = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    gemini_base_url: str = os.getenv("GEMINI_BASE_URL", DEFAULT_GEMINI_BASE_URL).rstrip("/")
    gemini_timeout_seconds: float = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "30"))
    gemini_max_output_tokens: int = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "2048"))
    # Extraction provider: fixture | gemini | venice. Venice speaks the OpenAI-compatible
    # API, so LLM_MODEL/LLM_BACKUP_MODEL are Venice model slugs. LLM_TIMEOUT_MS is in
    # milliseconds to match the vendor's own naming; it is normalised to seconds here.
    llm_provider: str = os.getenv("LLM_PROVIDER", "fixture").strip().lower()
    venice_api_key: str | None = os.getenv("VENICE_API_KEY") or None
    llm_base_url: str = os.getenv("LLM_BASE_URL", DEFAULT_VENICE_BASE_URL).rstrip("/")
    llm_model: str = os.getenv("LLM_MODEL", DEFAULT_VENICE_MODEL)
    llm_backup_model: str | None = os.getenv("LLM_BACKUP_MODEL") or None
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_MS", "12000")) / 1000.0
    callwright_api_key: str | None = os.getenv("CALLWRIGHT_API_KEY") or None
    callwright_base_url: str = os.getenv(
        "CALLWRIGHT_BASE_URL",
        "https://api.voygr.tech",
    ).rstrip("/")
    callwright_demo_phone_number: str | None = os.getenv("CALLWRIGHT_DEMO_PHONE_NUMBER") or None
    callwright_timeout_seconds: float = float(os.getenv("CALLWRIGHT_TIMEOUT_SECONDS", "15"))
    callwright_attempt_store: str = os.getenv(
        "CALLWRIGHT_ATTEMPT_STORE",
        ".writai/callwright-attempts.json",
    )
    callwright_poll_interval_seconds: float = float(
        os.getenv("CALLWRIGHT_POLL_INTERVAL_SECONDS", "2")
    )
    callwright_max_poll_seconds: float = float(os.getenv("CALLWRIGHT_MAX_POLL_SECONDS", "30"))
    callwright_live_calls_enabled: bool = _env_flag(
        "CALLWRIGHT_LIVE_CALLS_ENABLED",
        False,
    )
    interrupt_escalation_threshold_seconds: float = float(
        os.getenv("WRITAI_INTERRUPT_ESCALATION_THRESHOLD_SECONDS", "300")
    )
    interrupt_escalation_scan_interval_seconds: float = float(
        os.getenv("WRITAI_INTERRUPT_ESCALATION_SCAN_INTERVAL_SECONDS", "30")
    )
    interrupt_escalation_grant_store: str = os.getenv(
        "WRITAI_INTERRUPT_ESCALATION_GRANT_STORE",
        ".writai/interrupt-escalation-grants.json",
    )
    interrupt_escalation_phone_ref: str = os.getenv(
        "WRITAI_INTERRUPT_ESCALATION_PHONE_REF",
        "demo-venue",
    )
    # Seam keys for both build lanes; names match .env.example. Credentials default
    # to unset and fail closed. The hook's own operational knobs carry working local
    # defaults so enforcement needs no configuration to run.
    composio_api_key: str | None = os.getenv("COMPOSIO_API_KEY") or None
    composio_webhook_secret: str | None = os.getenv("COMPOSIO_WEBHOOK_SECRET") or None
    hexclave_api_url: str = os.getenv(
        "HEXCLAVE_API_URL",
        DEFAULT_HEXCLAVE_API_URL,
    ).rstrip("/")
    hexclave_project_id: str | None = os.getenv("HEXCLAVE_PROJECT_ID") or None
    hexclave_secret_key: str | None = os.getenv("HEXCLAVE_SECRET_SERVER_KEY") or None
    hexclave_team_id: str | None = os.getenv("HEXCLAVE_TEAM_ID") or None
    hexclave_permission_cache_ttl_seconds: float = float(
        os.getenv("HEXCLAVE_PERMISSION_CACHE_TTL_SECONDS", "60")
    )
    hexclave_webhook_secret: str | None = (
        os.getenv("HEXCLAVE_WEBHOOK_SECRET") or None
    )
    hexclave_webhook_evidence_store: str = os.getenv(
        "HEXCLAVE_WEBHOOK_EVIDENCE_STORE",
        ".writai/hexclave-webhook-events.json",
    )
    writai_hook_endpoint: str = os.getenv(
        "WRITAI_HOOK_ENDPOINT",
        "http://localhost:8002/supervisor/sessions",
    ).rstrip("/")
    # Hooks fail OPEN on timeout, so a long timeout silently disables enforcement.
    writai_hook_timeout_seconds: float = float(
        os.getenv("WRITAI_HOOK_TIMEOUT_SECONDS", "3")
    )
    writai_hook_cache_path: str = os.getenv(
        "WRITAI_HOOK_CACHE_PATH",
        ".writai/hook-verdict-cache.json",
    )
    public_base_url: str = os.getenv(
        "WRITAI_PUBLIC_BASE_URL",
        "http://localhost:8001",
    ).rstrip("/")
    approval_link_secret: str | None = (
        os.getenv("WRITAI_APPROVAL_LINK_SECRET") or None
    )
    approval_link_ttl_seconds: int = int(
        os.getenv("WRITAI_APPROVAL_LINK_TTL_SECONDS", "900")
    )
    approval_link_store: str = os.getenv(
        "WRITAI_APPROVAL_LINK_STORE",
        ".writai/approval-link-uses.sqlite3",
    )
    approval_assertion_store: str = os.getenv(
        "WRITAI_APPROVAL_ASSERTION_STORE",
        ".writai/approval-assertion-uses.sqlite3",
    )
    approval_recipient_bindings: str | None = (
        os.getenv("WRITAI_APPROVAL_RECIPIENT_BINDINGS") or None
    )
    ntfy_server: str = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    ntfy_topic: str | None = os.getenv("NTFY_TOPIC") or None
    ntfy_access_token: str | None = os.getenv("NTFY_ACCESS_TOKEN") or None
    ntfy_private_topic_confirmed: bool = _env_flag(
        "NTFY_PRIVATE_TOPIC_CONFIRMED",
        False,
    )
    pushover_app_token: str | None = os.getenv("PUSHOVER_APP_TOKEN") or None
    pushover_user_key: str | None = os.getenv("PUSHOVER_USER_KEY") or None
    resend_api_key: str | None = os.getenv("RESEND_API_KEY") or None
    email_from: str | None = os.getenv("WRITAI_EMAIL_FROM") or None
    crustdata_api_key: str | None = os.getenv("CRUSTDATA_API_KEY") or None
    crustdata_api_version: str = os.getenv(
        "CRUSTDATA_API_VERSION",
        "2025-11-01",
    )
    crustdata_webhook_bearer: str | None = (
        os.getenv("CRUSTDATA_WEBHOOK_BEARER") or None
    )
    crustdata_replay_bearer: str | None = (
        os.getenv("CRUSTDATA_REPLAY_BEARER") or None
    )
    crustdata_person_identity_bindings: str | None = (
        os.getenv("CRUSTDATA_PERSON_IDENTITY_BINDINGS") or None
    )
    crustdata_capture_dir: str = os.getenv(
        "CRUSTDATA_CAPTURE_DIR",
        ".writai/crustdata-captures",
    )


settings = Settings()


def require_production_secrets(config: Settings | None = None) -> None:
    """Refuse to serve outside a demo environment on the public default signing secret.

    ``grant_secret`` is the HMAC key for every signed grant, the derivation key for
    the internal-service capability, and the seed for every per-workspace context
    secret. On a demo machine the default is what makes a fresh clone run with zero
    configuration; anywhere else it means anyone who has read this repository can mint
    a valid grant. Called at module scope by every service so the process never binds
    a port on the default. A set-but-trivial secret is refused for the same reason.
    """

    active = settings if config is None else config
    if active.env.strip().lower() in DEMO_ENVIRONMENTS:
        return
    generate = 'python3 -c "import secrets;print(secrets.token_urlsafe(48))"'
    if active.grant_secret == DEFAULT_DEMO_GRANT_SECRET:
        raise RuntimeError(
            f"WRITAI_ENV={active.env!r} is running on the public default signing secret. "
            f"Set WRITAI_GRANT_SECRET to a private value, for example: {generate}"
        )
    if len(active.grant_secret) < MIN_GRANT_SECRET_LENGTH:
        raise RuntimeError(
            f"WRITAI_ENV={active.env!r} has a WRITAI_GRANT_SECRET shorter than "
            f"{MIN_GRANT_SECRET_LENGTH} characters. Generate one with: {generate}"
        )
