# Architecture

## Service boundary

```text
Company artifacts / fixtures
          |
          v
Ingestion or structured fixture adapter
          |
          v
Provenance graph (memory by default, Neo4j optional)
          |
          v
Intent Authority Service
  - approval and authority rules
  - scope-aware transitive invalidation
  - graph versioning
  - grant issuance and verification
          ^
          | authorization request
          |
Agent Service
  - plan
  - request verification
  - act / replan / stop
          |
          v
Executor Service
  - independently verifies grant
  - applies or rejects mock PR action
          |
          v
React operator interface
```

The authority and agent expose server-sent event streams. Mutations publish state through a
thread-safe in-process broker, so the React interface receives graph and loop changes without
polling. The UI still performs explicit reads after a user-triggered phase as a deterministic
postcondition check.

## Trust boundary

The agent never mutates authority state or signs its own grants. The executor does not trust a UI verdict; it sends the grant back to the authority service for verification.

## Default runtime

The default canonical demo uses an in-memory graph so it works immediately. The graph interface is
implemented separately so Neo4j can replace it without changing authority semantics.

Scenario Lab has a deliberately different persistence boundary: every run receives its own
`MemoryGraphStore` and `IntentAuthority`, even when `WRITAI_GRAPH_BACKEND=neo4j` selects Neo4j for
the canonical runtime. Lab runs therefore cannot reset, pollute, or contend over the configured
Neo4j graph.

## Scenario Lab

```text
React Scenario Lab
        |
        | catalog / start / advance / run-all
        v
Agent Service — ScenarioRunner
        |
        | HTTP, context_id + correlation ID
        v
Intent Authority — ScenarioAuthorityContextRegistry
        |
        | one MemoryGraphStore + signer per run context
        |
        +----------------------+
        |                      |
        | authorization        | grant verification
        v                      ^
Agent orchestration       Executor Service
                               ^
                               | HTTP execution request
                               +----- Agent Service
```

The backend Pydantic catalog contains 12 deterministic definitions. Each definition carries
presentation metadata, a typed graph seed, an initial `AgentRun`, an approved `DecisionMutation`, a
fixture-driven corrected `AgentPlan`, an authority policy, and assertion-only expectations.
Validation rejects duplicate IDs, missing edge endpoints, broken plan/ticket bindings,
non-task expectations, discontinuous scoped provenance, unauthorized mutations, downstream ID
mentions, mutation requirements that do not exactly match the affected scopes, and initial or
corrected plans that do not satisfy their approved requirements.

Every `graph-v17` seed has approved baseline Decision artifacts partitioned by role and scope. The
primary baseline decision owns exactly the scopes the incoming decision changes; companion
decisions remain authoritative for unaffected scopes. The initial plan is evaluated against the
union, so baseline authorization does not depend on implicit or unauthoritative requirements.

The agent creates a unique authority context and a real `AgentRun`, then advances through four
public stages:

1. `authorized` — the authority issues a baseline snapshot-bound grant;
2. `decision-changed` — the authority applies the approved mutation and traverses the graph;
3. `work-stopped` — the executor rejects the old grant and the original plan receives `REPLAN`;
4. `reauthorized` — the fixture-driven corrected plan receives `ALLOW` and a replacement grant
   executes.

ScenarioRunner applies the same shared authorization-to-loop transition used by the canonical
`AgentLoopController`. The retained loop state therefore moves through `ACT → REPLAN → COMPLETE`;
the UI exposes that server-owned state and its history rather than inferring a loop transition.

Stage changes are postcondition-bound: mutation must apply, the old execution must fail specifically
with `STALE_SNAPSHOT`, the conflicting plan must receive `REPLAN`, and the replacement execution
must succeed with `VALID`. A failed call leaves the run at the last truthful stage and persists an
inspectable failed evaluation while the retained loop moves to `BLOCKED`. Advance requests may
include the rendered `expected_stage`, making a retry after a lost response idempotent.

Every consequential operation still crosses the service boundary over HTTP. The agent never signs
a grant, and the executor forwards the signed token to the matching authority context rather than
trusting an agent or browser verdict. Each context derives a distinct signing key, so a grant from
one context is invalid in another.

Signed tokens are held only inside the agent runner long enough to call the executor and are cleared
when the run finishes. Public Scenario Lab responses include the verified grant payload metadata
needed for evidence views, but never include the signed token. Completed runs retain token-free
summaries—including expected and actual task IDs, false positives, missed invalidations, safety
results, and runtime—in a replaceable, process-local repository, while their authority contexts are
deleted. Detailed token-free runtimes are bounded to the five most recent completed runs per
scenario.

Run All serially executes the same four-stage runner for each selected definition. A failed scenario
produces a failed summary without preventing later scenarios from running. Failures after start
retain the real run ID and evidence for inspection; failures before a context exists are explicitly
marked non-inspectable. Evaluation compares actual Task outcomes and verification results with
structurally separate expectations; expectations never drive graph traversal, verdicts, or grants.

Run All history is deliberately session-only and process-local. The result repository keeps the
latest summary per scenario and at most five token-free detailed runs per scenario; completed
authority contexts are removed. Restarting the agent service clears this history.

Authority evaluation also resolves every action-level `task_id` back into the current graph. A plan
that reintroduces a missing, non-Task, `NEEDS_REVIEW`, or `INVALIDATED` task receives `REPLAN` even
when its requirement attributes otherwise match the latest decision.

Authority traversal follows only allowed downstream relationship kinds, sorts candidates
deterministically, and uses an explicit equal-depth tie-break for the primary provenance path.
Mutation and outcome payloads separate preserved/invalidated Tasks from artifacts that merely need
review.

### Guided story, Impact map, and Technical evidence

The React Examples interface uses one server-owned run model with three disclosure layers:

- **Guided story** presents the dominant stage message, selective Task outcomes, original Plan
  review status, and proposed corrective actions.
- **Impact map** presents the exact returned graph relationships, shortest provenance path, and
  preserved, invalidated, and needs-review artifacts.
- **Technical evidence** presents grant metadata, expanded timeline, evaluation checks, and source
  references.

The additive `outcome_summary` and run-summary fields drive both layers. The browser may format and
filter those values, but it does not derive verdicts or safety outcomes. Corrective actions are
truthfully typed as fixture-generated plan actions with no graph artifact ID; they are not
persisted Task nodes.

## Data flow

### Initial authorization

1. Agent loads `RUN-27` and `PLAN-027`.
2. Authority checks current requirements in `graph-v17`.
3. Plan matches `audience=all_users`.
4. Authority returns `ALLOW` and a signed `graph-v17` grant.

### Decision mutation

1. `DEC-018` is submitted.
2. Authority verifies `approved`, role `compliance`, confidence threshold, and affected scope.
3. Graph adds `DEC-018 -[:SUPERSEDES {scope: export.authorization}]-> DEC-004`.
4. Graph version increments to `graph-v18`.
5. Authority traverses downstream edges and records exact paths.
6. `TASK-102` is invalidated; `TASK-101` remains valid; `PLAN-027` needs replanning.

### Reauthorization

1. Executor verifies the old grant and rejects the snapshot mismatch.
2. Agent requests a current verdict for the original plan.
3. Authority returns `REPLAN` with the admin-only requirement.
4. Agent produces `PLAN-028` preserving CSV generation and changing authorization.
5. Authority issues a new `graph-v18` grant.
6. Executor accepts it.

The canonical `PLAN-028` and all Scenario Lab corrected plans are fixture-driven planner output.
Their actions are presented as proposed plan actions, not graph Tasks. Their authorization is real:
the authority evaluates their structured actions against current approved requirements, signs
successful grants, and the executor independently verifies them.

### Coordinated demo reset

The archived Guided Proof and compatibility clients can call one agent-owned reset endpoint. The
agent first asks the authority to seed `graph-v17`, validates the returned snapshot, and only then
clears its local run. This removes the former `Promise.all` partial-reset race while keeping the
services separately owned. It is cross-service coordination, not a distributed transaction.
Destructive reset is environment-gated and intended only for a dedicated local/demo database.

### Persistent Live Workspace

1. The agent service validates a friendly user document and writes it atomically to the configured
   JSON repository. The baseline remains a proposal.
2. The agent sends the explicit graph seed and authority policy to an isolated authority context.
3. The authority validates the acting role and approves the baseline without allowing the agent to
   manufacture a verdict.
4. Initial authorization crosses agent → authority and the signed token remains internal.
5. A proposed decision is persisted without graph effects. Its approval crosses into the authority,
   where role, scope, confidence, and requirement shape are checked before mutation.
6. The real graph traversal selectively invalidates intersecting Tasks and the active AgentPlan.
7. Initial and replacement verification cross agent → executor → authority. Public views expose
   stable verification codes but never signed tokens.
8. On restart, the agent reconstructs an absent authority context by replaying recorded approvals
   into the original graph seed. The JSON history remains the user-visible audit ledger.

Before reusing an existing context, the agent compares its graph version, baseline approval state,
authority policy, immutable artifact signatures, exact Decision lineage, and `SUPERSEDES` edges
with persistence. A mismatched or one-step-ahead context is discarded and replayed from the stored
record. This makes context-ID collisions and lost-response recovery fail closed.

The executor's `STALE_SNAPSHOT` result is a required state-machine proof, not presentation
metadata. `CHANGE_APPLIED` cannot skip directly to Plan correction, and a generic grant failure
cannot satisfy the stale-authorization claim.

The default store is `.writai/live-workspaces.json`; `WRITAI_WORKSPACE_STORE` may point to a
different file. It contains internal signed grants needed for restart verification and is written
with owner-only file permissions; public API views strip every signed token. Atomic replacement
protects against partial writes, but this prototype assumes one agent-service writer and is not a
transactional multi-process database.

## Optional integrations

### Neo4j

Set `WRITAI_GRAPH_BACKEND=neo4j`. The `Neo4jGraphStore` persists typed artifacts, dynamic relationship types, graph metadata, scopes, validity, and evidence references.
Its downstream traversal reads only matching outgoing relationships with Cypher. Seed/reset runs
in one write transaction, and the opt-in `neo4j` test suite compares the resulting graph and exact
invalidation report with the in-memory backend. The suite must target a disposable database because
reset deletes all data in the configured Neo4j database.

This setting affects the canonical authority runtime only. Scenario Lab contexts remain isolated
in memory and never call `Neo4jGraphStore.reset()`.

### Anthropic

The optional Anthropic adapter is not wired into the deterministic live demo. An integration may
instantiate it after installing `.[llm]` and setting `ANTHROPIC_API_KEY` plus `ANTHROPIC_MODEL`.
Its output is untrusted structure: a decision mutation plus exact, zero-based source-text spans.
Before a candidate can reach graph mutation, deterministic code verifies every span
against the supplied source. A separate `TrustedDecisionContext` replaces all model-proposed
governance metadata: source reference, approval status, authority role, confidence, affected
scopes, and supersession target. Missing, out-of-bounds, or non-matching evidence returns
`HUMAN_REVIEW` without changing the graph.

Candidates with valid evidence still pass through the authority engine, which
deterministically validates approval, role, extraction confidence, scope, and graph
relationships. Low-confidence candidates also return `HUMAN_REVIEW` without a graph
write. The default fixture extractor implements the same candidate interface without
importing or requiring Anthropic.

### LangGraph

The repository includes an optional LangGraph workflow builder. The deterministic controller remains available as a fallback and as the unit-test target.
