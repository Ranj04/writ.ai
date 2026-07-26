# Sponsor integrations

One section per integration: what it does for the product, exactly which
variables to set and where to get them, the one command that proves it is live,
and what degrades without it.

**The one command:**

```bash
writai doctor            # or: PYTHONPATH=backend .venv/bin/python -m writai.cli doctor
writai doctor --json     # machine-readable
writai doctor composio   # just one
```

It does not check that an environment variable is non-empty. **It makes a real,
read-only call** for every integration that has a cheap and safe one, because a
variable set to a dead key is the failure this exists to catch. It never posts a
message, places a call, or creates a watcher.

Four statuses, and only one of them is a pass:

| | Meaning |
|---|---|
| `[ LIVE ]` | Configured, and a real call proved the credential works. |
| `[ DEAD ]` | Configured, and the service rejected it — or the configuration is internally wrong (right key, missing team; right key, unpasted auth config). **Worse than absent**, because the code takes the live path. |
| `[  ??  ]` | Could not be checked cheaply. **Not a pass.** Treat as unknown. |
| `[ ---- ]` | Not configured. A choice, not a fault — exit code stays 0. |

`writai doctor` exits **1 only for a dead credential**. An integration you have
chosen not to configure never fails a preflight.

---

## Status as measured on this machine

Run `writai doctor` for the current answer. At the time of writing:

| Integration | Status | What the probe found |
|---|---|---|
| **Gemini** | `LIVE` | Key accepted; 50 models visible, including `gemini-3.6-flash`. |
| **Composio** | `DEAD` | Key accepted **and Slack is connected**, but `COMPOSIO_SLACK_AUTH_CONFIG_ID` is unset. Doctor prints the id to paste. |
| **Hexclave** | `DEAD` | Secret key **valid**, project resolves, but the project contains **zero teams**, so no valid `HEXCLAVE_TEAM_ID` can exist yet. A provisioning gap, not a bad key. |
| **Callwright** | `----` | `CALLWRIGHT_API_KEY` unset. |
| **CrustData** | `----` | `CRUSTDATA_API_KEY` unset; the path runs on a replayed fixture. |
| **Superset** | `----` | `superset` is not on `PATH`. |

---

## Gemini — turns a Slack sentence into a decision proposal

Reads an approved decision written in English and proposes the structured change
it implies: affected scope, superseded decision, new requirement. It **only ever
proposes** — deterministic code validates the evidence spans and the requirement
shape, and a human approves.

| Variable | Where to get it | Required |
|---|---|---|
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | yes |
| `GEMINI_MODEL` | defaults to a working model; doctor fails if the one you set is not visible to your key | no |
| `LLM_PROVIDER` | set to `gemini` to enable; `fixture` (default) keeps the deterministic path | yes, to enable |

**Verify:** `writai doctor gemini`

**Without it:** Slack messages cannot become decision proposals. The explicit
delta still works and needs no model at all:

```bash
writai approve --text "<message>" --scope export.authorization --was all_users --now admin_only
```

**Measured behaviour, 14 live runs on one sentence:** 14/14 produced a valid
proposal and 6/6 driven through approval reached a real graph write with the
correct blast radius. **The requirement wording is not reproducible** — 14 runs
produced 14 different requirement shapes, none using the workspace's own key.
The scope-level verdict is stable; the wording handed to a redirected agent is
not. Do not promise a judge that the redirect text is deterministic.

---

## Composio — the whole Slack loop, in three surfaces

1. **Read** the decision from a channel (signed trigger delivery).
2. **Post** the approval card back into that same thread, carrying the extracted
   fields and the blast radius.
3. **Read** the approval back as a ✅ reaction on that card.

| Variable | Where to get it | Required |
|---|---|---|
| `COMPOSIO_API_KEY` | [app.composio.dev](https://app.composio.dev) → Settings → API Keys | yes |
| `COMPOSIO_WEBHOOK_SECRET` | Composio dashboard → Webhooks → signing secret | yes |
| `COMPOSIO_SLACK_AUTH_CONFIG_ID` | Connect Slack in Composio; **`writai doctor composio` prints the id for you** | yes |
| `WRITAI_SLACK_CHANNEL_ID` | the Slack channel to watch, e.g. `C0123456789` | yes |

**Verify:** `writai doctor composio`

**Without it:** the loop is shut in both directions — nothing is ingested from a
channel, no card is posted back, no reaction can approve. Message text must be
supplied by hand.

**Not yet exercised:** no real signed delivery has been received on this machine.
The verification code and the reaction path are tested against fixtures copied
from Composio's own trigger payloads, not against a live webhook.

---

## Hexclave — the identity behind every approval

Resolves an approval token, a Slack reactor, or a CLI caller to **one** Hexclave
user, then checks that user's permission before anything is applied. Every
approval channel funnels through the same permission check; there is no
per-channel shortcut.

| Variable | Where to get it | Required |
|---|---|---|
| `HEXCLAVE_PROJECT_ID` | Hexclave dashboard → project settings | yes |
| `HEXCLAVE_SECRET_SERVER_KEY` | Hexclave dashboard → API keys (server key) | yes |
| `HEXCLAVE_TEAM_ID` | **create a team first** — see below | yes |

**Verify:** `writai doctor hexclave`

### Browser sign-in (`@hexclave/react`)

Installed for the `/approvals` surface only, so `/` and `/scenario-lab` — the
fallback demo routes — keep rendering with no provider, no sign-in and no
network. `src/hexclave/client.ts` builds the client **lazily and never at import
time**: `new HexclaveClientApp()` throws when no project is configured, and
constructing it at module scope white-screened `/approvals` on any machine
without Hexclave set up. Unconfigured now degrades to a labelled rehearsal.

The browser sends an **access token**; the CLI sends a **user API key**. These
are different artifacts and resolve through different endpoints, so the server
accepts either via `ChainedHexclaveIdentityResolver` rather than asking the
caller to declare which it holds — a claim a caller could get wrong or lie
about. Both land on the same user id, the same permission check and the same
audit record.

| Artifact | Resolver | Endpoint |
|---|---|---|
| Browser access token | `HexclaveAccessTokenIdentityResolver` | `GET /users/me` with `x-stack-access-token` |
| User API key | `HexclaveUserApiKeyIdentityResolver` | `POST /user-api-keys/check` |

`hexclave dev --config-file ./hexclave.config.ts -- <command>` starts a local
dashboard and injects `HEXCLAVE_PROJECT_ID` and `HEXCLAVE_SECRET_SERVER_KEY`
into the child process, with no account required. That is the likely unblock for
the zero-teams problem below — **not yet tried here.**

**Currently blocked on provisioning, not on credentials.** A real call to
`GET /teams` with your server key returns `200 {"items":[]}` — the key is valid
and the project resolves, but it contains **no teams**, so no valid
`HEXCLAVE_TEAM_ID` exists. Create a team in the dashboard and doctor will print
its id to paste.

**Without it:** every authenticated approval path fails closed with
`APPROVAL_AUTHENTICATION_FAILED`. The demo falls back to the gated in-process
seam, which requires `WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1` and bypasses the
*channel* authentication only — role, scope, confidence, the three-way
requirement match and the proposal binding all still run.

---

## Callwright — escalates an unacknowledged interrupt to a phone call

When an interrupt goes unacknowledged, the grant-gated executor can place a real
call. The grant is verified before the call is submitted, and attempts are
idempotent against a persisted store.

| Variable | Where to get it | Required |
|---|---|---|
| `CALLWRIGHT_API_KEY` | Callwright dashboard | yes |
| `CALLWRIGHT_DEMO_PHONE_NUMBER` | a number on your allowlist | to place a call |
| `CALLWRIGHT_LIVE_CALLS_ENABLED` | **`false` by default, deliberately** — set `true` only when you intend real calls | to place a call |

**Verify:** `writai doctor callwright` (reads a call list; never places a call)

**Without it:** escalation still runs, but the fixture client records the attempt
instead of dialling. Note the key alone does **not** enable calls — the flag is a
separate, deliberate switch so a stray key cannot dial anyone.

---

## CrustData — notices when an approver changes role or leaves

An approver who changes role or leaves makes every decision they approved rest on
a fact that is no longer true. This watches for that and flags those decisions
for human review. **It flags; it never invalidates** — nothing here writes
`ValidityStatus` or touches `invalidated_scopes`.

| Variable | Where to get it | Required |
|---|---|---|
| `CRUSTDATA_API_KEY` | CrustData dashboard | to talk to the API |
| `CRUSTDATA_WEBHOOK_BEARER` | you choose it; CrustData documents no webhook signing, so this bearer is the compensating control | yes, to accept a delivery |

**Verify:** `writai doctor crustdata`

**This path is a REPLAY, and stays one even with a valid key.** The person
watcher has a documented one-hour minimum interval, so it cannot fire inside a
demo. The delivery is replayed on demand and labelled
*"documentation-reconstructed payload, replayed (not captured from CrustData)"*
on the API response, the SSE event, the CLI output and inside the fixture file.

**The fixture is reconstructed from** CrustData's documented Person Entity
Watcher webhook shape ([docs.crustdata.com/watcher-docs/person/entity](https://docs.crustdata.com/watcher-docs/person/entity)),
**not captured from a real delivery**, because no API key was available when it
was written.

**Still needs a real capture:** one genuine watcher delivery, saved in place of
the reconstructed fixture. Also needs an identity mapping — the join currently
compares a CrustData person id against a Hexclave user id, which works on the
fixture because both sides are seeded consistently and will not on real data.

**Without it:** nothing observes an approver leaving, so decisions keep resting
on approvals that may no longer hold.

---

## Superset — isolated worktrees for the five demo sessions

Gives each of the five agent sessions its own Superset workspace instead of a
plain directory, so they cannot collide on a shared filesystem.

| Variable | Where to get it | Required |
|---|---|---|
| `superset` on `PATH` | [docs.superset.sh](https://docs.superset.sh) | yes |
| `WRITAI_DEMO_SUPERSET_PROJECT` | your Superset project id — **`workspaces create` has no default `--project`** | yes |
| `WRITAI_DEMO_SUPERSET_BASE_BRANCH` | defaults to `main` | no |

**Verify:** `writai doctor superset`

**Without it:** the launcher falls back to plain directories under the demo root
and says so. The demo still runs.

### Why provisioning only, and not `agents_create` — settled, do not revisit

Superset can start the agent for you. **We deliberately do not let it**, and the
deciding fact is not preference:

> An agent Superset launches does not get the hook configuration or the canned
> prompt this launcher writes into each session directory. A session started
> without the `PreToolUse` hook is an **unenforced session that looks identical
> to a working one on screen** — it registers nothing, it is never interrupted,
> and it produces confident output the whole time.

That is the exact silent failure this project has already been bitten by twice:
an unbound session is allowed everything and is indistinguishable from success,
which is why `check.sh` treats it as one of the three demo-killers. Handing
session startup to a tool that cannot install the hook re-introduces it by
design.

So the division is fixed:

| | Owner |
|---|---|
| Creating the isolated workspace | **Superset** (`workspaces create`) |
| Writing `.writai/task`, the hook config, and the prompt | **the launcher** |
| Starting the agent | **the launcher** |

Nothing is lost by this. Isolated per-agent workspaces *are* Superset's product;
`agents_create` is a convenience on top, and it is the one part we cannot use
without giving up enforcement. `--agent` and `--prompt` are not passed for the
same reason.

**Undocumented surfaces:** `terminals_send` and `terminals_read` exist in
Superset's source but are **not documented**, and nothing here depends on them
at runtime.

**Not yet exercised against the real CLI.** The provisioning logic is verified
against a stub that mimics the documented contract; Superset is not installed
here. The first real run should watch for `superset could not provision
session-N`, which is the clean fallback rather than a crash.

---

## The rule that governs all of this

**Nothing may claim to be live that isn't.** If an integration is running on a
fixture or a replay, `writai doctor` says so, and the real-vs-simulated panel in
the product says so. `UNVERIFIED` is never rendered as success, and a replayed
payload is never labelled live.
