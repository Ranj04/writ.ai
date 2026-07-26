# writ.ai

**Continuous decision-provenance control for autonomous coding agents.**

> Tests prove the code works. writ.ai proves the work is still wanted.

writ.ai detects when an approved upstream company decision changes while a coding-agent run is active. It traces the change through a typed provenance graph, selectively invalidates only affected downstream work, rejects authorizations bound to stale graph snapshots, and moves the agent loop to `REPLAN`, `BLOCK`, or `HUMAN_REVIEW`.

This repository is a Codex-ready hackathon starter. The deterministic demo works without external API keys. Neo4j and Anthropic integrations are included as optional extension points.

## Read first

Codex and human contributors should read these files in order:

1. [`AGENTS.md`](AGENTS.md) — implementation rules and non-negotiable invariants.
2. [`writai.md`](writai.md) — complete product brief.
3. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — service and data flow.
4. [`docs/GRAPH_SCHEMA.md`](docs/GRAPH_SCHEMA.md) — nodes, edges, scopes, and traversal semantics.
5. [`TASKS.md`](TASKS.md) — prioritized work queue.
6. [`docs/CODEX_START_PROMPT.md`](docs/CODEX_START_PROMPT.md) — a ready-to-paste Codex prompt.

## What already works

The starter contains a deterministic in-memory implementation of the core proof:

1. `graph-v17` authorizes a plan to create CSV exports for all users.
2. The agent completes the plan and tests pass.
3. An approved compliance decision creates `graph-v18` and changes only `export.authorization`.
4. A multi-hop traversal reaches the active plan without the new decision mentioning the ticket.
5. `TASK-102` is invalidated while sibling `TASK-101` remains valid.
6. The executor rejects the old snapshot-bound grant.
7. The loop returns `REPLAN`, preserves CSV generation, adds the admin constraint, and receives a new valid grant.

## Examples

Examples extend the canonical CSV proof into 12 deterministic requirement-change cases:

1. CSV exports become admin-only.
2. A payment provider is no longer approved.
3. Customer data must remain in the United States.
4. A public launch is canceled while internal testing continues.
5. API access becomes read-only.
6. Logs may not contain personal data.
7. AI-generated changes require human approval.
8. Third-party model use is prohibited.
9. File uploads are limited to PDFs.
10. Database migrations must be reversible.
11. Production access is removed from the agent.
12. User deletion must remove derived data.

Each `graph-v17` seed contains approved, role-authoritative baseline decisions whose combined
requirements authorize the initial plan. The changed decision supersedes only the baseline
decision responsible for its affected scopes; companion decisions continue to govern unaffected
scopes.

The browser provides a searchable Examples catalog and a presenter-controlled guided story.
**Guided story** explains each decision and enforcement step in plain language. **Impact map**
shows the exact returned relationships and selective outcomes. **Technical evidence** expands the
grant metadata, ordered timeline, evaluation, and source references. The backend catalog and
`outcome_summary` remain the typed source of truth; the browser does not traverse the graph, decide
verdicts, sign grants, calculate pass results, or invent loop state. Example runs retain a real
`AgentRun` that transitions through `ACT`, `REPLAN`, and `COMPLETE`.

Examples always create an isolated in-memory authority context per run. This remains true when
the canonical demo is configured to use Neo4j, so running one or all Lab scenarios does not reset or
mutate the configured Neo4j database. The agent orchestrates each run over HTTP through the intent
authority and independent executor. Signed grant tokens stay server-side; public agent responses
contain only grant payload metadata.

Run All is serialized and keeps only the latest summary per scenario plus a bounded set of detailed
runs. This history is process-local and session-only: restarting the agent service clears it.

## Live Workspace

Live Workspace is the practical, user-owned path. Import structured YAML or JSON, or choose a PDF,
Word document, Markdown file, text file, or PNG/JPEG/WebP screenshot. Screenshot text is read with
a locally bundled English OCR model. Document uploads are converted into an untrusted draft
containing a decision proposal, specification, ticket, tasks, plan, scopes, and authority roles.
The OCR confidence is preserved in evidence metadata, and the user must explicitly confirm the
extracted fields before server validation. That human confirmation is recorded separately;
extraction never approves a decision or issues a verdict. writ.ai then persists the workspace,
constructs and validates its provenance graph, and walks you through the real enforcement
lifecycle:

```text
import → approve baseline → authorize plan → approve change
       → reject stale grant → correct plan → verify replacement grant
```

The authority and executor boundaries are unchanged: the browser and CLI cannot mint a verdict,
approve their own proposal, or verify a signed grant locally. Public responses expose grant
metadata and verification codes but never the signed token.

Start the stack and open:

```text
http://127.0.0.1:5173/
```

Use the built-in refund example, choose a `.yaml`, `.yml`, `.json`, `.pdf`, `.docx`, `.md`, `.txt`,
`.png`, `.jpg`, `.jpeg`, or `.webp` file, or download the document template from the page. The web
Workspace gives both the built-in starter and each selected file a fresh visible run ID, so the
demo can be rehearsed repeatedly without deleting earlier audit records. Direct API and CLI imports
retain their declared IDs and still reject duplicates:

```bash
writai workspace import examples/writai-workspace.yaml
writai workspace approve-baseline refund-operations --role finance-admin
writai workspace authorize refund-operations
```

The complete CLI flow, exit-code contract, and reusable GitHub Action are documented in
[`docs/LIVE_WORKSPACE_CLI.md`](docs/LIVE_WORKSPACE_CLI.md). Persistent state defaults to
`.writai/live-workspaces.json`; change it with `WRITAI_WORKSPACE_STORE`.

## Fastest start

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -e ".[dev]"
make demo
make check
```

No Anthropic key or Neo4j database is required for those commands.

## Run the full stack

Start all three APIs and the frontend, with readiness checks and coordinated cleanup. This command
pins the local stack to `127.0.0.1` on ports `8001`–`8003` and `5173`, overriding conflicting
service URL values from the root `.env` for the processes it launches. It explicitly enables the
demo reset flow, which deletes and reseeds the selected graph backend; configure only a dedicated
local/demo database before running it:

```bash
make stack
```

Open `http://127.0.0.1:5173`.

Browser routes:

- `http://127.0.0.1:5173/` — Workspace: import and enforce user-owned work;
- `http://127.0.0.1:5173/live-workspace` — compatibility alias for Workspace;
- `http://127.0.0.1:5173/scenario-lab` — seeded Examples catalog;
- `http://127.0.0.1:5173/scenario-lab?demo=1` — presenter entry for the CSV example;
- `http://127.0.0.1:5173/scenario-lab?scenario=api-read-only` — open a named example.

In Examples, choose a scenario and select **Start with authorized work**. Advance deliberately with
**Approve the change**, **Show affected work**, **Check old authorization**, and the final
correction action. **Run all scenarios** executes the 12 isolated scenarios and opens the measured
session-only report. The `?demo=1` route waits for all three services, resets the CSV example,
obtains its real baseline authorization, and opens directly on the first guided stage.

## Run services separately

Open three terminals after installing dependencies:

```bash
make authority   # http://localhost:8001
make agent       # http://localhost:8002
make executor    # http://localhost:8003
```

Then run the frontend:

```bash
cd frontend
npm install
npm run dev
```

The Vite UI runs at `http://localhost:5173` by default.

## Optional external services

Copy the environment template:

```bash
cp .env.example .env
```

- Set `WRITAI_GRAPH_BACKEND=neo4j` and provide Neo4j credentials to use a real graph database.
- The optional Anthropic adapter is an explicit extension point, not part of the live demo path.
  Install `.[llm]`, set `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`, then wire the adapter through
  `TrustedDecisionContext` supplied by authenticated ingestion. Exact source spans are checked
  deterministically; model-proposed approval, role, confidence, effective time, scope,
  supersession, and pre-existing invalidation state are ignored and never control mutation.
- Keep the deterministic authority engine as the final source of `ALLOW`, `REPLAN`, `BLOCK`, and `HUMAN_REVIEW` verdicts.

The local memory backend keeps zero-config fixture seeding in development/demo environments.
Neo4j never enables destructive startup seeding or `/graph/reset` by default: set
`WRITAI_DEMO_RESET_ENABLED=true` explicitly and use only a dedicated writ.ai demo database.
Scenario Lab does not use this destructive reset path; its per-run stores are always isolated
`MemoryGraphStore` instances.

### Hexclave provisioning

The checked-in `hexclave.config.ts` records the project apps and RBAC permission definitions.
It contains no API keys or other credentials. Authenticate the CLI, then pull the live project
configuration into a clean worktree before editing so dashboard changes are not overwritten:

```bash
npx --yes @hexclave/cli@1.0.66 login
npx --yes @hexclave/cli@1.0.66 config pull \
  --cloud-project-id "$HEXCLAVE_PROJECT_ID" \
  --config-file ./hexclave.config.ts \
  --overwrite
git diff -- hexclave.config.ts
```

After reviewing the pulled state, edit the file and inspect the diff before explicitly syncing it:

```bash
git diff --check -- hexclave.config.ts
npx --yes @hexclave/cli@1.0.66 config push \
  --cloud-project-id "$HEXCLAVE_PROJECT_ID" \
  --config-file ./hexclave.config.ts
```

`config push` changes the live Hexclave project, so it is deliberately not part of `make check`.
The current config schema records the `approve_compliance` team-permission definition, but not the
concrete Dragback team, its members, or their grants. Manage those live server-side and keep
Hexclave credentials only in the ignored `.env`.

Configure the Hexclave Webhooks app to send `team_permission.created`,
`team_permission.deleted`, `team_membership.created`, and `team_membership.deleted` to the
authority service at `https://<public-authority-host>/webhooks/hexclave`, then set:

```dotenv
HEXCLAVE_WEBHOOK_SECRET=<svix-endpoint-secret>
HEXCLAVE_WEBHOOK_EVIDENCE_STORE=.writai/hexclave-webhook-events.json
```

The endpoint verifies the raw delivery with the Svix signature headers, stores only redacted event
identifiers and a body digest, and clears the authority and agent permission caches. It never
produces an authorization verdict or mutates the graph, policy, grants, or sessions. The next
approval still performs the live server-side Hexclave permission check; an unavailable agent
service returns `503` so Svix retries the invalidation.

### CrustData person watcher

CrustData is a review signal, never an authority source. A role change or departure can flag
decisions that person previously approved, but it cannot mutate the graph or revoke authority
without human confirmation.

Create an account with the sponsor code, obtain an API key, and configure:

```dotenv
CRUSTDATA_API_KEY=...
CRUSTDATA_API_VERSION=2025-11-01
CRUSTDATA_WEBHOOK_BEARER=<strong-random-callback-secret>
CRUSTDATA_REPLAY_BEARER=<different-strong-random-operator-secret>
CRUSTDATA_CAPTURE_DIR=.writai/crustdata-captures
CRUSTDATA_PERSON_IDENTITY_BINDINGS={"schema_version":1,"people":[{"crustdata_person_id":<CRUSTDATA_PERSON_ID>,"hexclave_user_id":"<HEXCLAVE_USER_ID>","evidence_ref":"provisioning://crustdata/person/<CRUSTDATA_PERSON_ID>"}]}
```

The identity binding is human-provisioned server configuration. writ.ai never infers a Hexclave
identity from a name, title, email address, or webhook claim. The two bearer values must be
different: CrustData receives only the callback credential, while deliberate replay is an
operator-only capability. The service fails closed if they match.

Start the agent service with `WRITAI_DEMO_RESET_ENABLED=false`. Put a controlled HTTPS gateway in
front of it that permits only `POST /intake/crustdata/person/capture` and rejects every other
method and path. Do **not** point an unrestricted `ngrok http 8002` tunnel at the agent service:
that port also carries demo and state surfaces. For a temporary callback, the checked-in ngrok
Traffic Policy provides that narrow boundary and disables request-body inspection:

```bash
# Terminal 1
WRITAI_DEMO_RESET_ENABLED=false make agent

# Terminal 2
ngrok http 8002 \
  --traffic-policy-file ./examples/crustdata-ngrok-traffic-policy.yml \
  --inspect=false
```

The existing key has been accepted by the current read-only Person Watcher list endpoint, but
there are currently zero watchers and no genuine callback has been captured. Do not provision one
until a human supplies the exact LinkedIn target and the matching Hexclave user id; writ.ai never
guesses either identity.

List active watchers without changing vendor state:

```bash
curl --fail-with-body \
  'https://api.crustdata.com/watch/person?status=active&limit=50&offset=0' \
  --header "Authorization: Bearer ${CRUSTDATA_API_KEY}" \
  --header 'Accept: application/json' \
  --header 'x-api-version: 2025-11-01'
```

Once the target and identity binding are confirmed, create the Person watcher with
`POST https://api.crustdata.com/watch/person`, `Authorization: Bearer
<CRUSTDATA_API_KEY>`, `x-api-version: 2025-11-01`, `Content-Type:
application/json`, and this complete body:

```json
{
  "entities": {
    "professional_network_profile_urls": [
      "<LINKEDIN_PROFILE_URL>"
    ]
  },
  "track": {
    "op": "or",
    "conditions": [
      {
        "field": "experience.employment_details.current",
        "type": "added"
      },
      {
        "field": "basic_profile.current_title",
        "type": "changed"
      }
    ]
  },
  "fields": [
    "basic_profile",
    "experience"
  ],
  "config": {
    "trigger": {
      "type": "interval",
      "every_hours": 24
    },
    "max_results_per_run": 100,
    "refresh_frequency_days": 30
  },
  "notifications": [
    {
      "type": "webhook",
      "url": "https://<PUBLIC_AGENT_HOST>/intake/crustdata/person/capture",
      "headers": {
        "Authorization": "Bearer <CRUSTDATA_WEBHOOK_BEARER>"
      }
    }
  ]
}
```

The 24-hour trigger is an 86,400-second interval; the documented minimum is one hour. The first
run establishes a silent baseline, so creating a watcher is not a stage-time change event.
The callback only writes an owner-readable replay file; it does not create a review or mutate the
graph. CrustData does not document a vendor webhook signature, so the capture records that it
arrived with the configured shared bearer and explicitly records that no vendor signature was
verified. The retained file is minimized to the watched identity/profile fields and delivered
change evidence, but it still contains personal data; delete it after the review/demo according to
your retention policy. After a later change is captured, replay the returned file deliberately:

```bash
writai workspace replay-crustdata \
  .writai/crustdata-captures/crustdata-capture-<id>.json
```

The CLI reads `CRUSTDATA_REPLAY_BEARER` from the environment, keeping it out of process arguments.
Before processing captured provenance, the service requires an exact match with its immutable
server-side capture; a caller cannot self-assert that a payload was captured. Captured files are
labelled `configured CrustData callback payload, replayed from server capture (not live; no vendor
signature verified)`. The original documentation-reconstructed fixture remains available as an
explicitly simulated fallback.

For a deterministic rehearsal that does not call CrustData or require a callback:

```bash
make demo-crustdata-replay
```

That command is reconstructed fallback evidence only, never a live sponsor demonstration. After
the real watcher is no longer needed, delete it explicitly and verify it no longer appears in the
read-only list:

```bash
curl --fail-with-body --request DELETE \
  'https://api.crustdata.com/watch/person/<WATCHER_ID>' \
  --header "Authorization: Bearer ${CRUSTDATA_API_KEY}" \
  --header 'x-api-version: 2025-11-01'
# Expected: HTTP 204. Then repeat the active-watcher GET above.
```

See the current [API authentication reference](https://docs.crustdata.com/openapi-specs/2025-11-01/introduction)
and [Person Entity Watcher contract](https://docs.crustdata.com/watcher-docs/person/entity).

### Neo4j parity tests

The Neo4j suite is opt-in because it resets the configured database. Use only a disposable
database, provide connection values through the environment, and keep credentials out of
command history and source control.

```bash
pip install -e ".[dev,graph]"
WRITAI_RUN_NEO4J_TESTS=1 python -m pytest -m neo4j
```

The suite seeds `graph-v17` repeatedly and compares the persisted graph, selective invalidation
report, and `ALLOW`/`REPLAN` behavior with the in-memory store. Without the opt-in variable, the
tests skip and the normal deterministic suite needs no Neo4j credentials.

## Repository layout

```text
AGENTS.md                         Codex operating instructions
TASKS.md                          prioritized implementation queue
writai.md                       complete product brief
docs/                             architecture, graph, API, demo, and test docs
fixtures/                         seeded company artifacts and decision changes
backend/writai/                 Python package
  authority/                      authority and selective invalidation engine
  graph/                          in-memory and Neo4j stores
  llm/                            fixture and Anthropic extraction adapters
  loop/                           agent-loop controller and LangGraph adapter
  scenarios/                      typed catalog, isolated contexts, runner, and evaluation
  services/                       FastAPI authority, agent, and executor apps
frontend/                         React Workspace and Examples interface
scripts/                          bootstrap, demo, service, and validation scripts
```

## Add an Example

1. Add a definition in `backend/writai/scenarios/catalog.py`. Use scenario-namespaced artifact,
   action, plan, ticket, and run IDs; only the canonical CSV scenario retains its familiar IDs.
2. Provide the graph seed, initial `AgentRun`, approved `DecisionMutation`, fixture-driven corrected
   `AgentPlan`, presentation copy, and assertion-only expectations. Keep expectations separate from
   the data that drives authority behavior.
3. Add every changed scope and authoritative role to `SCENARIO_AUTHORITY_POLICY`, and assign every
   seeded scope an approved owner in `SCENARIO_BASELINE_AUTHORITY_BY_SCOPE`.
4. Let `ScenarioDefinition` validation check unique IDs, edge endpoints, role and scope authority,
   exact mutation requirement scopes, scope-continuous provenance, downstream-ID non-mention, and
   initial/corrected plan requirements.
5. Run:

   ```bash
   python -m pytest backend/tests/test_scenario_catalog.py \
     backend/tests/test_scenario_authority_contexts.py \
     backend/tests/test_scenario_runner.py \
     backend/tests/test_scenario_service_flow.py
   make check
   ```

The catalog API and UI discover valid definitions automatically; no scenario-specific browser
component is required.

## Real versus fixture-driven

Real behavior includes graph writes and versioning, deterministic authority policy, multi-hop
traversal, selective invalidation, plan hashing, signed grant issuance, structured grant
verification, executor rejection, corrected reauthorization, and expected-versus-actual
evaluation. Scenario definitions, Slack/Linear-style evidence references, approved decision input,
corrected-plan wording/actions, and pull-request creation are fixture-driven or simulated for the
prototype. Corrective actions are explicitly labeled fixture-generated `plan-action` previews;
they are not persisted or presented as graph Task artifacts.

## Hackathon scope

Build the reasoning and enforcement for real. Keep OAuth, webhooks, real PR creation, authentication, multitenancy, and production key management simulated. The exact scope boundaries are in [`AGENTS.md`](AGENTS.md).
