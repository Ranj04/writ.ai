# API contract

## Shared response contract

Every service accepts an optional `X-Correlation-ID` request header and returns the validated or
generated ID in both the response header and a top-level `correlation_id` field. Errors use a stable
`error` envelope with `code`, `message`, and `retryable`; service-to-service calls forward the same
correlation ID. SSE streams use the same correlated event envelope.

Grant verification returns one stable code:

| Code | Meaning |
|---|---|
| `VALID` | signature, expiry, bindings, snapshot, plan hash, and current plan are valid |
| `INVALID_TOKEN` | the signature or token encoding is invalid |
| `NON_ALLOW_VERDICT` | the signed payload does not contain `ALLOW` |
| `EXPIRED` | the signed authorization has expired |
| `BINDING_MISMATCH` | the grant belongs to a different run or task |
| `PLAN_HASH_MISMATCH` | the supplied plan differs from the signed plan |
| `STALE_SNAPSHOT` | the active graph snapshot differs from the signed snapshot |
| `CURRENT_PLAN_REJECTED` | deterministic re-evaluation no longer returns `ALLOW` |

Scenario Lab public responses expose grant payload metadata, verification codes, and evidence but
never expose signed grant tokens. Tokens move only between the agent runner, executor, and intent
authority.

## Intent Authority — port 8001

### `POST /demo/reset`

Reloads the `graph-v17` fixture. The memory backend enables demo reset by default in local
development/demo environments.

### `POST /graph/reset`

Aura-compatible alias for the same deterministic seed/reset operation. The selected graph backend
is reset in one store operation. Neo4j startup seeding and this endpoint are disabled by default;
both require an explicit `WRITAI_DEMO_RESET_ENABLED=true` and must target a dedicated demo
database because reset deletes all nodes.

### `GET /demo/state`

Returns graph version, artifacts, relationships, and the most recent invalidation report.

### `POST /decisions/ingest`

Accepts a trusted/fixture `DecisionMutation` containing a decision, `supersedes_id`, and
`affected_scopes`. Do not send raw LLM output to this endpoint; optional extraction must first pass
exact-span validation and authenticated `TrustedDecisionContext`.

`TrustedDecisionContext` owns source, approval, authority role, confidence, effective time,
affected scopes, and supersession. Before evaluation, extracted decisions are also normalized to
`VALID` with no pre-existing invalidated scopes.

Expected approved response:

```json
{
  "applied": true,
  "reason": "Approved decision applied and affected work re-evaluated.",
  "graph_version": "graph-v18",
  "verdict": null,
  "report": {
    "affected_scopes": ["export.authorization"],
    "affected_artifact_ids": ["DEC-004", "SPEC-009", "TICKET-100", "TASK-102", "PLAN-027"],
    "upstream_chain_artifact_ids": ["DEC-018", "DEC-004", "SPEC-009", "TICKET-100"],
    "preserved_task_ids": ["TASK-101"],
    "invalidated_task_ids": ["TASK-102"],
    "needs_review_artifact_ids": ["DEC-004", "SPEC-009", "TICKET-100", "PLAN-027"],
    "stopped_work_artifact_ids": ["TASK-102", "PLAN-027"],
    "directly_mentioned_artifact_ids": [],
    "preserved_artifact_ids": ["TASK-101"],
    "evidence_refs": [
      "slack://compliance/decision-018",
      "slack://product/decision-004",
      "notion://specs/export-009",
      "linear://ticket/TICKET-100",
      "linear://ticket/TICKET-100#task-101",
      "linear://ticket/TICKET-100#task-102",
      "agent://run/RUN-27/plan/PLAN-027"
    ]
  },
  "correlation_id": "demo-run-27"
}
```

The typed Task and review fields are additive; legacy artifact lists remain for compatible
consumers. Mutation requirements must cover exactly `affected_scopes`. Any approval, authority,
confidence, shape, or scope failure returns without mutating the graph or advancing its version.

### `POST /authorize`

Accepts `run_id`, `task_id`, and a complete `AgentPlan`.

Returns an `AuthorizationResult` with verdict, reasons, mismatches, current requirements, and optional signed grant.

### `POST /grants/verify`

Accepts token, run ID, task ID, and plan. Rejects bad signatures, non-`ALLOW` payloads, expired
grants, plan-hash mismatches, stale graph versions, and currently invalid plans.

### `GET /events`

Streams an immediate `graph.state.snapshot`, followed by `graph.state.reset`,
`graph.state.changed`, or `graph.decision.reviewed` SSE events. Each event contains a monotonically
increasing ID and correlated envelope.

### Scenario Lab authority contexts

These routes are service-facing. Browser clients use the agent routes documented below.

#### `POST /scenario-lab/authority/contexts`

Creates an isolated, in-memory authority context from a server-known scenario:

```json
{
  "context_id": "ctx-csv-exports-admin-only-abc123",
  "scenario_id": "csv-exports-admin-only"
}
```

Each context owns a separate `MemoryGraphStore`, authority instance, lock, and derived signing key.
Its `graph-v17` seed contains approved role-authoritative baseline decisions for every governed
scope. It remains in memory even when the canonical authority backend is Neo4j.

#### `GET /scenario-lab/authority/contexts/{context_id}`

Returns the context graph, relationships, version, and last invalidation report. It contains no
signed token.

#### `DELETE /scenario-lab/authority/contexts/{context_id}`

Deletes a completed or canceled context. Scenario completion normally performs this cleanup.

#### `POST /scenario-lab/authority/contexts/{context_id}/mutation`

Applies the scenario definition's server-owned approved mutation. The caller cannot substitute an
arbitrary mutation.

#### `POST /scenario-lab/authority/contexts/{context_id}/authorize`

Evaluates an `AuthorizationRequest` against that context.

#### `POST /scenario-lab/authority/contexts/{context_id}/grants/verify`

Verifies a `GrantVerificationRequest` against that context's signing key and graph. Cross-context
tokens return `INVALID_TOKEN`.

## Agent Service — port 8002

### `POST /demo/reset`
Clears local run state.

### `POST /demo/reset-all`

Coordinates the presentation reset: the authority must confirm `graph-v17` before the agent clears
its local run. The archived Guided Proof used this single endpoint instead of concurrent independent
resets; it remains available for CLI, backend, and compatibility testing.

### `POST /demo/start`
Loads the initial run and requests a `graph-v17` authorization. The state response retains the
immutable `initial_plan` beside `initial_grant_token`, so stale-grant verification always uses the
same plan that was originally authorized.

### `POST /demo/tests-pass`
Marks the implementation and tests as complete while preserving the active run.

### `POST /demo/recheck`
Requests reauthorization for the current plan.

### `POST /demo/replan`
Applies current requirements to the affected plan action, creates a new plan ID, and requests a new authorization.

### `GET /demo/state`
Returns current run, plan, grant, verdict, and transition history.

### `GET /events`

Streams an immediate `loop.state.snapshot`, followed by `loop.state.reset` or
`loop.state.changed` events.

### Scenario Lab

#### `GET /scenario-lab/scenarios`

Returns the 12 scenario catalog entries plus the most recent token-free result summary for each
scenario. Every catalog entry also includes a compact pre-change specification, ticket, and task
preview plus the executable initial plan. Task previews label the assertion-only expected outcome
as `preserved` or `invalidated`; those labels are presentation/evaluation metadata and never drive
authority, invalidation, or grant decisions. `corrective_actions` are typed fixture previews with
`source: "fixture"`, `representation: "plan-action"`, no graph artifact ID, and lifecycle
`fixture-preview`.

#### `GET /scenario-lab/scenarios/{scenario_id}`

Returns the complete validated definition: metadata, narrative, graph seed, initial run, approved
mutation, fixture-driven corrected plan, authority policy, presentation copy, and assertion-only
expectations.

#### `POST /scenario-lab/runs`

Starts an isolated run:

```json
{
  "scenario_id": "csv-exports-admin-only"
}
```

The response status is `201` and the stage is `authorized`. The agent has already created the
authority context over HTTP and obtained a real baseline grant. The response includes only the
grant payload, not its signed token.

#### `GET /scenario-lab/runs/{run_id}`

Returns the current token-free run view, including graph artifacts and edges, recorded events,
authorization payloads, executor results, server-owned `agent_loop_state` / `agent_history`, and
evaluation when complete. Its additive `outcome_summary` is the browser's authoritative semantic
projection:

```json
{
  "preserved_task_ids": ["TASK-101", "TASK-102", "TASK-103"],
  "invalidated_task_ids": ["TASK-104", "TASK-105"],
  "needs_review_artifact_ids": ["PLAN-027"],
  "original_plan_id": "PLAN-027",
  "original_plan_status": "NEEDS_REVIEW",
  "corrective_actions": [
    {
      "id": "ACTION-6",
      "description": "Show export controls only after an administrator role check.",
      "scopes": ["export.authorization"],
      "source": "fixture",
      "representation": "plan-action",
      "graph_artifact_id": null,
      "persisted_as_graph_artifact": false,
      "lifecycle": "authorized-plan-action"
    },
    {
      "id": "ACTION-7",
      "description": "Enforce administrator access at the export API boundary.",
      "scopes": ["export.authorization"],
      "source": "fixture",
      "representation": "plan-action",
      "graph_artifact_id": null,
      "persisted_as_graph_artifact": false,
      "lifecycle": "authorized-plan-action"
    }
  ],
  "old_grant_verification_code": "STALE_SNAPSHOT",
  "replacement_authorization_verdict": "ALLOW",
  "replacement_grant_verification_code": "VALID",
  "may_continue": true,
  "primary_provenance_path": [
    "DEC-018",
    "DEC-004",
    "SPEC-009",
    "TICKET-100",
    "TASK-104",
    "PLAN-027"
  ],
  "history_scope": "session"
}
```

`needs_review_artifact_ids` here is work-impact focused, while the mutation report retains every
partially affected graph artifact. Corrective actions remain fixture-generated plan actions rather
than persisted graph Tasks; their lifecycle is `fixture-preview` until the corrected plan is
authorized, then `authorized-plan-action`. `may_continue` is computed by the server.

#### `POST /scenario-lab/runs/{run_id}/advance`

Advances one deterministic stage. The browser binds the request to the stage it rendered:

```json
{
  "expected_stage": "authorized"
}
```

If a response is lost after the server commits, retrying the same request returns the already
advanced state instead of advancing twice. `expected_stage` is optional for backwards-compatible
service callers. Starting from the `authorized` response, three successful calls produce:

```text
decision-changed → work-stopped → reauthorized
```

The final response has `status: "passed"` or `status: "failed"` and an expected-versus-actual
evaluation. A stage is committed only after its safety postconditions hold. Otherwise the run keeps
the last truthful presentation stage, moves its loop state to `BLOCKED`, records an inspectable
failed evaluation, clears signed tokens, and removes its authority context.

#### `POST /scenario-lab/scenarios/{scenario_id}/reset`

Removes active in-memory runs and contexts for one scenario. It does not call the canonical graph
reset and never deletes Neo4j data.

#### `GET /scenario-lab/results`

Returns the latest process-local result summary for each completed scenario. Summaries retain
expected and actual preserved/invalidated task IDs, false-positive and missed invalidation IDs,
old-grant and reauthorization outcomes, runtime, failure reasons, and whether a detailed run view
is available through `inspectable`. Additive semantic fields include `plan_status`,
`needs_review_artifact_ids`, the old and replacement verification codes, the replacement verdict,
and `history_scope: "session"`.

#### `POST /scenario-lab/run-all`

Runs every scenario, or an explicit unique subset:

```json
{
  "scenario_ids": [
    "csv-exports-admin-only",
    "api-read-only"
  ]
}
```

Use `{}` to run all 12. Each scenario gets its own authority context and executes through the same
agent → authority → executor path as a guided run. One failed scenario is recorded and does not
stop later scenarios. Failures after a run starts remain inspectable by their real run ID. A failure
before a run/context can be established has `inspectable: false` and retains its details in the
summary rather than advertising a nonexistent run; its `plan_status` is `null`.

Run All is serialized. Results retain the latest summary per scenario, detailed token-free
runtimes are bounded to five per scenario, and completed authority contexts are deleted. This is
session-only process memory, not durable benchmark history; an agent-service restart clears it.

### Live Workspaces

Live Workspaces are the user-owned practical path. Unlike Scenario Lab, their definitions,
authorization state, verification results, and history survive agent-service restarts in the JSON
file selected by `WRITAI_WORKSPACE_STORE` (default
`.writai/live-workspaces.json`). The public views never expose signed grant tokens.

#### `POST /live-workspaces/import`

Imports structured JSON containing:

- `id`, `name`, and optional `description`;
- `authority_policy`, keyed by governed scope;
- one proposed baseline `Decision` whose `attributes.requirements` exactly match its scopes;
- one `Specification`, one `Ticket`, one or more `Task` artifacts, and one `AgentPlan`;
- optional typed `edges` and an optional numeric `graph_version` (default `17`).

When edges are omitted, writ.ai deterministically creates
`Decision → Specification → Ticket → Task → AgentPlan` provenance. Explicit edge sets are
augmented with missing `Task -[:CURRENTLY_DRIVES]-> AgentPlan` links so the active plan remains
reachable. Every baseline requirement must be an object and its scope must continue through the
Specification, Ticket, at least one Task, a matching Plan action, and every canonical typed edge
between them. The baseline authority role must be allowed by policy for every governed scope.
Plan attributes and optional `task_id` references are preflighted with the same matching semantics
used for initial authorization, so imports that could only receive `REPLAN` before any upstream
change are rejected. Import does not approve the baseline or issue a grant.

#### Workspace state and actions

| Route | Purpose |
|---|---|
| `GET /live-workspaces` | List persistent token-free workspace views |
| `GET /live-workspaces/{id}` | Read one workspace and its ordered history |
| `POST /live-workspaces/{id}/baseline/approve` | Approve the baseline with `{ "actor_role": "…" }` |
| `POST /live-workspaces/{id}/authorize` | Ask the authority for the initial snapshot-bound grant |
| `POST /live-workspaces/{id}/decisions/propose` | Store a proposed `DecisionMutation` without changing the graph |
| `POST /live-workspaces/{id}/decisions/{decision_id}/approve` | Role-check and apply the pending change |
| `DELETE /live-workspaces/{id}/decisions/pending` | Cancel only the pending proposal; leave the graph and initial authorization unchanged |
| `POST /live-workspaces/{id}/grants/initial/verify` | Send the initial grant and original plan through the executor |
| `PUT /live-workspaces/{id}/plan` | Store a user-supplied corrected `AgentPlan` |
| `POST /live-workspaces/{id}/reauthorize` | Evaluate the corrected plan and retain its replacement grant |
| `POST /live-workspaces/{id}/grants/replacement/verify` | Verify the replacement grant through the executor |

The write-only action routes without fields accept `{}`. Status progresses through
`imported`, `baseline-approved`, `authorized`, `change-proposed`, `change-applied`,
`initial-grant-rejected`, `plan-updated`, `reauthorized`, and `complete`. Authority verdicts and
executor results—not the agent service—determine whether a grant exists or can be applied.
Proposal submission first verifies that `supersedes_id` names an existing workspace Decision and
that the affected scopes are contained by it. A proposal that later fails authority approval
remains inspectable and can be canceled before submitting a replacement.

Plan correction is locked until the executor has specifically returned a non-applied
`STALE_SNAPSHOT` result for the retained initial grant. `EXPIRED`, `INVALID_TOKEN`, and every other
non-stale failure remain visible but do not advance the workspace, unlock Plan updates, or permit
completion. Completion also requires a `VALID` replacement verification.

Every view includes the graph version, baseline and latest approved mutation wording, current plan,
token-free authorization metadata, invalidation report, executor verification codes, and ordered
persistent history. After a service restart, the agent rebuilds a missing authority context from
the original proposal, re-approves the baseline with the recorded role, and replays all approved
mutations. Context-specific signing keys are deterministic, so retained grants can still be
verified subject to their original expiry and snapshot binding.

### Live Workspace authority contexts

The authority service owns these service-facing routes:

- `POST /live-workspaces/authority/contexts`
- `GET|DELETE /live-workspaces/authority/contexts/{context_id}`
- `POST /live-workspaces/authority/contexts/{context_id}/baseline/approve`
- `POST /live-workspaces/authority/contexts/{context_id}/mutations/approve`
- `POST /live-workspaces/authority/contexts/{context_id}/authorize`
- `POST /live-workspaces/authority/contexts/{context_id}/grants/verify`

Context creation accepts the explicit graph version, artifacts, edges, baseline Decision ID, and
authority policy. Approval endpoints require the acting role to equal the Decision authority role
and to be authorized for every scope. They also enforce confidence and exact requirement shape
before any graph mutation. A proposal alone never changes the graph.
The seed may contain exactly one Decision: the proposed baseline. Pre-approved or additional
Decision artifacts are rejected. A missing supersession target produces a deterministic conflict,
not an internal graph error.

## Executor Service — port 8003

### `POST /execute`

Calls the authority verifier. Returns:

```json
{
  "applied": false,
  "reason": "Grant snapshot graph-v17 is stale; current graph is graph-v18.",
  "verification_code": "STALE_SNAPSHOT"
}
```

Scenario and Live Workspace requests also include `context_id`; Live Workspace calls additionally
set `context_kind: "workspace"`. The executor forwards verification to the correct isolated
authority context. A successful response uses `verification_code: "VALID"` and may include a
simulated PR URL.

## Browser routes

- `/` — user-owned Workspace;
- `/live-workspace` — compatibility alias for Workspace;
- `/scenario-lab` — seeded Examples catalog and Run All report;
- `/scenario-lab?demo=1` — CSV presenter entry in guided-story view;
- `/scenario-lab?scenario={scenario_id}` — guided-story view for a named example.

The `scenario` query parameter selects an initial browser view only. Demo mode waits briefly for all
three services, resets the CSV scenario through the agent API, and starts its real baseline
authorization so the presenter lands on stage one. Neither route bypasses the backend catalog or
supplies authority input.

Examples default to **Guided story**: a plain-language projection of the backend-owned outcome.
**Impact map** exposes the exact returned graph relationships and selective outcomes.
**Technical evidence** exposes grant metadata, ordered timeline, evaluation checks, and source
references. Every layer displays server results; none calculates an authority verdict.
