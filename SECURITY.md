# Security policy

writ.ai is a permission layer for autonomous coding agents. It decides whether an
agent's work is still authorized, so a defect here is a defect in an authorization
decision. Reports are welcome and will be answered honestly, including when the answer
is "known, deliberate, and written down."

## Reporting a vulnerability

Open a [private security advisory](https://github.com/Ranj04/writ.ai/security/advisories/new)
on this repository. Please do not open a public issue for anything exploitable.

Include the version or commit, the configuration (`WRITAI_ENV` and whether demo reset is
enabled both change the answer materially), and the smallest reproduction you have.

**What you can expect.** An acknowledgement within 7 days. This is maintained by one
person, so that is a commitment that can actually be met rather than an aspirational
24-hour figure. If a report is valid you will be credited in the advisory unless you ask
otherwise.

## Supported versions

| Version | Supported |
|---|---|
| `main` | yes |
| `0.1.x` tags | no — pre-1.0, no backports |

There is no release cadence yet. Fixes land on `main`.

## Known and deliberate weaknesses

These are not vulnerabilities to report. They are documented properties, each with the
code that implements the boundary, and each is covered by a test that fails if the
behaviour drifts. If you can defeat one of these *outside* its stated precondition, that
is a vulnerability and we want to hear about it.

**The hook fails open when its process dies.** `PreToolUse` converts any internal error
into `deny` (`hooks/writai_pre_tool_use.py`), and the HTTP wait is clamped to 4 seconds
so it cannot outlast the 5-second command deadline
(`hooks/writai_hook_lib.py:56`) — a process killed before it writes has, in effect,
allowed the call. But a hook that never starts is invisible to Claude Code, and
`allowManagedHooksOnly` prevents removal, not death. The backstop is the PR check, which
deliberately checks the checker out from the base branch so a candidate change cannot
rewrite the gate that judges it
(`.github/workflows/writai-pr-authorization.yml:45-56`). See `hooks/README.md:42-55`.

**Anonymous grant minting while the demo gate is open.** On a default development
configuration, `POST /decisions/ingest` accepts a caller-asserted *approved* decision,
and a plan matching that requirement then gets `ALLOW` and a signed grant from the open
`/authorize` route. `WRITAI_ENV=production` — or any value outside `DEMO_ENVIRONMENTS` —
answers `403 FIXTURE_INGEST_DISABLED` and closes it. Both directions are under test in
`backend/tests/review/test_pr2_trust_boundary.py`. See
[Where the trust boundary is](README.md#where-the-trust-boundary-is).

**23 mutating routes carry no identity check.** They are the routes the browser calls,
and the alternative today would be shipping a credential to every visitor, which
`frontend/src/approvals/api.ts` refuses on purpose. `backend/tests/test_route_authentication.py`
freezes every mutating route to a tier, so a new unguarded route has to be added to the
map consciously. What those routes cannot do is enumerated in the README section above.

**The published default signing secret.** One constant is the HMAC key for every grant,
the internal-service capability, and every per-workspace context secret. A startup guard
refuses to boot outside a demo environment on that value or on any placeholder the
repository itself publishes — the blocklist is derived from `.env.example`, so
documenting a placeholder automatically bans it (`backend/writai/config.py`).

**A world-readable window in the legacy JSON store.** `chmod(0o600)` runs after the
rename, over a file that serialises signed grant tokens
(`backend/writai/workspaces/repository.py:82-83`). The SQLite store that replaced it is
owner-only from creation, including its `-wal` and `-shm` siblings. The JSON path is
kept for compatibility and its properties are pinned by strict `xfail` tests.

**Four low-severity transitive npm advisories**, all reached through `@hexclave/shared`.
Tracked in `outputs/OPEN-ITEMS-REGISTER.md`; not on the demo path.

## Scope

In scope: authorization decisions, grant issuance and verification, the enforcement
hook, the CI backstop, secret handling, and the intake paths that accept outside input.

Out of scope: the simulated integrations listed under
[Scope: what is built for real](README.md#scope-what-is-built-for-real), and anything
requiring an attacker who already has the grant secret — that is the root of every trust
chain here and is documented as such.
