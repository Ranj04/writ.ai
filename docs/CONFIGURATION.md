# Configuration

Every environment variable writ.ai reads, where it is read, and what happens when it is
absent. `.env.example` is the machine-checked source: 87 variables are documented there,
and `test_every_documented_env_var_has_a_settings_field` fails the build for any one that
is neither a `Settings` field in `backend/writai/config.py` nor on the explicit
`_READ_OUTSIDE_SETTINGS` allow-list. Documenting a variable nothing reads is a build
failure, not a stale comment.

**Nothing here is required for `make demo`.** It runs in-process with zero configuration
and no network. `make check` is the same. Configuration only matters once you run the
services.

## The one that is not optional outside development

`WRITAI_GRANT_SECRET` is the HMAC key for every signed grant, the derivation key for the
internal-service capability between the three services, and the seed for every
per-workspace authority context secret. One constant, three trust chains.

`require_production_secrets()` in `backend/writai/config.py` refuses to start any service
outside a demo environment when this is unset, left at the published demo default, shorter
than 32 characters, or equal to **any placeholder this repository publishes** — the
blocklist is derived from `.env.example` itself, so documenting a placeholder
automatically bans it. The check runs at module scope, so the process never binds a port.

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(48))"
```

Rotating it invalidates every in-flight grant and every stored escalation grant, and all
three services must restart together. There is no key id in the token, so there is no
overlap window. That is a known limit, not an oversight.

## What `WRITAI_ENV` changes

More than logging. `DEMO_ENVIRONMENTS` (`development`, `demo`, `local`, `test`) decides
two things that matter:

- **The production secret guard** above only enforces outside those environments.
- **`demo_reset_enabled`** defaults on for a demo environment on the memory backend, and
  that flag gates `POST /decisions/ingest`, which accepts a caller-asserted *approved*
  decision. With it open, an anonymous caller who can reach the authority port can ingest
  a decision and then get a signed grant for a plan matching it. See
  [Where the trust boundary is](../README.md#where-the-trust-boundary-is); both
  directions are under test in `backend/tests/review/test_pr2_trust_boundary.py`.

Set `WRITAI_ENV=production` for anything reachable by anyone you do not trust.

## Where variables are read

Almost everything flows through the frozen `Settings` dataclass in
`backend/writai/config.py`, constructed once at import. Values are read at import time, so
changing the environment after a process starts does nothing.

The exceptions are listed literally in `_READ_OUTSIDE_SETTINGS`
(`backend/tests/test_runtime_config.py`), each with the reason it is an exception:

| Variable | Read by |
|---|---|
| `WRITAI_HOOK_API_KEY` | the stdlib-only `hooks/` scripts and the supervisor service |
| `WRITAI_HOOK_API_KEYS` | `supervisor_api.parse_hook_credentials`, the per-developer map |
| `HEXCLAVE_APPROVER_USER_API_KEY` | `writai approve` — the human approver's own key |
| `COMPOSIO_SLACK_AUTH_CONFIG_ID`, `WRITAI_SLACK_CHANNEL_ID` | `writai doctor` probes only |
| `SUPERSET_API_KEY` | `scripts/demo/lib.sh`, demo infrastructure |
| `NEO4J_*` | the driver, `docker-compose.yml`, and the `neo4j` CI job |
| `VITE_*` | the frontend build, through Vite |

## `VITE_*` is shipped to every browser

Vite inlines any `VITE_`-prefixed variable into the bundle at build time. `vite.config.ts`
sets `envDir: ".."`, so it reads the **repository-root** `.env` — the same file holding
`WRITAI_GRANT_SECRET` and every other secret. Only `VITE_`-prefixed names are inlined, so
this is safe as it stands, but the blast radius of one mis-prefixed name is the entire
secret set published to every visitor.

**Never give a secret a `VITE_` name.** The approvals client reads its token from the
runtime instead, and says so at length in `frontend/src/approvals/api.ts`.

The four that exist are service URLs and two Hexclave display flags. A trailing slash on a
service URL is normalised by `serviceBaseUrl()` — all four clients use it, and a test pins
that, because an un-normalised base requests `//path`, 404s, and silently falls back to
fixture data.

## Integrations, and what happens when each is absent

| Integration | Unconfigured behaviour |
|---|---|
| Gemini, Venice | raise at construction — fails loud |
| Hexclave | `503 HEXCLAVE_NOT_CONFIGURED` — fails closed |
| Composio / Slack intake | `503 SLACK_INTAKE_NOT_CONFIGURED` — fails closed |
| CrustData intake | bearer-gated, fails closed |
| Callwright | falls back to the fixture client; live calls also require `CALLWRIGHT_LIVE_CALLS_ENABLED`, default off |
| Neo4j | the in-memory graph is the default backend; Neo4j is opt-in |

`writai doctor` reports seven probes and distinguishes `LIVE`, `UNVERIFIED`, `INVALID`
and `ABSENT` rather than collapsing them to pass/fail. It exits non-zero on any `INVALID`,
which includes a production environment sitting on a published placeholder secret.

## Store paths

Durable state defaults to relative paths under `.writai/`, resolved against the process
working directory. A service started from a different directory therefore gets a **new,
empty store** rather than an error. Set absolute paths for anything long-lived. The
workspace store refuses to open a file that exists but is not a SQLite database, naming
the path, rather than silently initialising an empty one.
