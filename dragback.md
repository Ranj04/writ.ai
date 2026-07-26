# Dragback

**Decision provenance and continuous intent control for autonomous coding agents**

**Event:** c0mpiled-11: Startup School Hackathon — Friday, July 24, 2026  
**Primary track:** Company Brain  
**Secondary fit:** AI Operating System for Companies

---

## One-line pitch

**Dragback continuously verifies that an autonomous coding agent is still working toward the company's latest approved intent—and forces it to replan when the decisions behind its task change.**

## Memorable line

> Tests prove the code works. Dragback proves the work is still wanted.

## The problem

Coding agents can execute a ticket perfectly even when the ticket is stale.

A task might say, “Add CSV export for all users,” while a newer approved compliance decision says, “Exports must be admin-only.” If nobody updates the ticket, a normal agent can implement the wrong objective, pass every test, and still produce work the company no longer wants.

Dragback addresses that gap. It does not merely ask whether an agent has permission to act. It asks whether the objective driving the action is still authorized by current company decisions.

---

## Core product loop

```text
PLAN
  ↓
VERIFY DECISION PROVENANCE
  ↓
ALLOW / REPLAN / BLOCK / HUMAN_REVIEW
  ↓
ACT
  ↓
OBSERVE NEW DECISIONS OR RESULTS
  ↓
REVERIFY
```

Verification runs before consequential actions—starting implementation, changing the plan, opening a pull request, applying a patch, or merging—and whenever a relevant approved decision changes.

---

## Demo scenario

1. A ticket says: **“Add CSV export for all users.”**
2. The ticket is valid when the coding-agent run begins.
3. The agent creates a plan, implements the feature, and passes its tests.
4. A new approved compliance decision enters the system: **“Exports must be admin-only.”**
5. The new decision never mentions the ticket directly.
6. Dragback follows the provenance path from the decision through the specification and ticket to the active plan.
7. It invalidates only the authorization-related part of the work; CSV generation remains valid.
8. The previous authorization becomes stale, the mock executor refuses it, and the loop transitions to `REPLAN`.
9. The agent produces a corrected plan with an administrator check and submits it for verification again.

### The key proof

```text
Task A: Generate valid CSV files          → VALID
Task B: Expose export to every user       → INVALIDATED
```

One sibling survives. That demonstrates scope-aware invalidation rather than blanket propagation.

---

## Core capabilities

### 1. Typed decision-provenance graph

Dragback models company artifacts as typed nodes:

- `Decision`
- `Specification`
- `Ticket`
- `AgentPlan`
- `PullRequest`
- `CodeChange`
- `Evidence`
- `AgentRun`

It connects them with typed relationships:

- `SUPERSEDES`
- `AMENDS`
- `CONTRADICTS`
- `DERIVED_FROM`
- `BASIS_FOR`
- `IMPLEMENTS`
- `AUTHORIZED_BY`
- `CURRENTLY_DRIVES`
- `SUPPORTED_BY`

The graph is an enforcement input, not merely a retrieval database.

### 2. Authority-aware supersession

The newest message does not automatically win. Dragback evaluates:

- approval status,
- decision-maker authority,
- affected scope,
- proposal versus final decision,
- complete replacement versus narrow amendment,
- effective date,
- extraction confidence,
- and source evidence.

A proposal can be recorded without invalidating work. An approved decision from the appropriate authority can update the graph and trigger reauthorization.

### 3. Multi-hop transitive invalidation

```text
New compliance decision
        ↓ SUPERSEDES
Old product decision
        ↓ BASIS_FOR
Export specification
        ↓ CREATES
Implementation ticket
        ↓ CURRENTLY_DRIVES
Active coding-agent plan
```

Dragback makes this dependency chain explicit and auditable. The changed decision does not need to mention the downstream ticket directly.

### 4. Selective scope invalidation

A decision change carries an affected scope, such as:

```json
{
  "decision_id": "DEC-018",
  "status": "approved",
  "supersedes": "DEC-004",
  "affected_scopes": ["export.authorization"]
}
```

Each descendant artifact also has scopes. Dragback invalidates only descendants whose scopes intersect the changed decision's scope.

### 5. Continuous run reauthorization

Each active run is pinned to a decision-graph snapshot and exact plan:

```json
{
  "authorization_id": "AUTH-481",
  "run_id": "RUN-27",
  "task_id": "TASK-102",
  "decision_snapshot": "graph-v17",
  "plan_hash": "sha256:9e9d...",
  "verdict": "ALLOW",
  "expires_at": "2026-07-24T21:04:00Z"
}
```

When the graph changes to `graph-v18`, Dragback reevaluates affected active runs. A previously valid authorization can become unusable without anyone editing the original ticket.

### 6. Four deterministic outcomes

- **`ALLOW`** — the objective and proposed plan remain valid.
- **`REPLAN`** — the broader objective remains valid, but part of the implementation must change.
- **`BLOCK`** — the objective is no longer approved.
- **`HUMAN_REVIEW`** — evidence, scope, or authority is too ambiguous for an automatic decision.

The system does not invent missing company intent.

### 7. Executor-side enforcement

Dragback does not stop at a warning banner. The mock PR gateway verifies the authorization before applying an action:

```text
Agent proposes PR
        ↓
Executor verifies authorization
        ↓
Snapshot current?
Plan hash matches?
Grant unexpired?
Task still valid?
        ↓
Apply or reject
```

Example rejection:

```text
PR creation rejected.
Authorization AUTH-481 was issued against graph-v17.
TASK-102 is invalid under graph-v18.
```

### 8. Explainable invalidation paths

Every verdict includes the complete reasoning path:

- the approved decision,
- the decision it superseded,
- the affected scope,
- the dependent specification,
- the downstream ticket and plan,
- and the source evidence for every relevant node and edge.

### 9. Surgical replanning

A `REPLAN` response preserves unaffected work:

```json
{
  "current_objective": "Provide CSV export",
  "invalidated_requirement": "Available to all users",
  "new_constraint": "Administrators only",
  "preserved_work": [
    "CSV generation",
    "download endpoint"
  ],
  "required_changes": [
    "Add role check",
    "Add unauthorized-access test"
  ]
}
```

The corrected plan is verified again before execution.

---

## Core implementation principle

> **The LLM proposes structure; deterministic code decides and enforces.**

The LLM may extract a candidate decision, scope, or provenance relationship from raw text. It never directly issues the final authorization verdict.

Deterministic Python and Neo4j logic handle:

- authority rules,
- graph traversal,
- scope intersection,
- graph versioning,
- invalidation propagation,
- authorization issuance,
- and executor verification.

---

## High-level architecture

```text
┌─────────────────────────────────────┐
│ Company artifacts                   │
│ Slack • Docs • Linear • GitHub      │
│ Seeded fixtures for the hackathon   │
└──────────────────┬──────────────────┘
                   ▼
┌─────────────────────────────────────┐
│ Ingestion and extraction            │
│ Anthropic API proposes decisions,   │
│ scopes, relationships, and evidence │
└──────────────────┬──────────────────┘
                   ▼
┌─────────────────────────────────────┐
│ Provenance graph — Neo4j            │
│ Decisions • Specs • Tickets • Plans │
└──────────────────┬──────────────────┘
                   ▼
┌─────────────────────────────────────┐
│ Intent Authority — FastAPI          │
│ Authority checks • Scope matching   │
│ Invalidation • Versioning • Grants  │
└──────────────────┬──────────────────┘
                   │ authorization
                   ▼
┌─────────────────────────────────────┐
│ Agent Loop — FastAPI + LangGraph    │
│ Plan → Verify → Act → Observe       │
│ Replan / Block / Human review       │
└──────────────────┬──────────────────┘
                   │ proposed action + grant
                   ▼
┌─────────────────────────────────────┐
│ Mock executor / PR gateway          │
│ Independently verifies the grant    │
│ Applies or rejects the action       │
└──────────────────┬──────────────────┘
                   ▼
┌─────────────────────────────────────┐
│ React + TypeScript interface        │
│ Graph path • Loop state • Evidence  │
│ Real-versus-simulated disclosure    │
└─────────────────────────────────────┘
```

The agent and authority are intentionally separate. The planner cannot approve its own work.

---

## Technology stack

| Layer | Tool | Role |
|---|---|---|
| Provenance graph | **Neo4j AuraDB** | Typed nodes and edges, multi-hop traversal, invalidation paths |
| LLM | **Anthropic API** | Structured extraction and replanning; advisory only |
| Loop orchestration | **LangGraph** | Explicit `PLAN → VERIFY → ACT/REPLAN/BLOCK/HUMAN_REVIEW` state machine |
| Backend services | **FastAPI** | Separate `agent-service` and `intent-authority` APIs |
| Frontend | **React + TypeScript** | Loop state, graph path, evidence, and authorization views |
| Live updates | **Server-Sent Events** | Stream loop and graph changes to the interface |
| Supporting libraries | `neo4j`, `pydantic`, `httpx`, `PyJWT` or HMAC utilities, `hashlib` | Data models, service calls, signed grants, and plan hashes |
| Fixtures | JSON and Markdown | Slack-style decisions, specifications, tickets, and agent plans |

### Framework choice

LangGraph is preferred over CrewAI because Dragback is primarily an explicit, deterministic state machine around one coding agent—not a crew of role-based agents delegating work to one another.

### External live dependencies

Only two external services are required for the hackathon build:

1. Neo4j AuraDB
2. Anthropic API

---

## What is real versus simulated

### Built for real

- graph writes and version changes,
- typed provenance relationships,
- authority and approval rules,
- multi-hop traversal,
- selective scope invalidation,
- the surviving sibling task,
- snapshot-bound authorization,
- stale-authorization rejection,
- the `ACT → REPLAN` transition,
- corrected-plan generation,
- and reauthorization.

### Seeded or simulated

- Slack, Linear, and GitHub OAuth,
- production webhooks,
- real repository modification,
- real pull-request creation or merge,
- authentication and multitenancy,
- production key management,
- and a multi-hour autonomous coding run.

---

## Competitive positioning

Based on currently reviewed public materials:

- decision-context products can retrieve or check work against recorded decisions;
- permission-oriented agent controls govern which tools or systems an agent may access;
- Dragback focuses on **mid-run reauthorization when the upstream decision that justified the work changes**.

The defensible mechanism is the combination of:

1. authority-aware supersession,
2. selective multi-hop invalidation,
3. graph-snapshot-bound authorization,
4. and automatic loop transition to `REPLAN`, `BLOCK`, or `HUMAN_REVIEW`.

Named competitor comparisons should be reverified before any public launch or published market claim.

---

## Demo outline: 3–5 minutes

1. **Problem — 20–30 seconds**  
   “Coding agents can write correct code from obsolete decisions. Dragback verifies that the work is still wanted.”

2. **Initially valid run — 30–45 seconds**  
   Show the ticket, agent plan, `graph-v17` authorization, completed implementation, and passing tests.

3. **Decision change and reasoning — 60–90 seconds**  
   Ingest the new approved compliance decision. Show the two- or three-hop provenance path lighting up. Emphasize that the new decision never mentions the ticket.

4. **Selective invalidation — 30–45 seconds**  
   Show one sibling task remaining valid while the authorization-related task is invalidated.

5. **Enforcement and replan — 30–45 seconds**  
   The executor rejects the `graph-v17` grant. The loop moves from `ACT` to `REPLAN`, produces a corrected plan, and receives a new authorization.

6. **Code proof — 30–45 seconds**  
   Show the Cypher traversal, scope-intersection rule, and executor verification.

7. **Close — 10 seconds**  
   > Tests prove the code works. Dragback proves the work is still wanted.

The demo should center the invalidation path and surviving sibling—not the red or green verdict badge.

---

## Hackathon success criteria

The build is demo-ready when all five are true:

- [ ] A run starts valid against `graph-v17`.
- [ ] A new approved decision creates `graph-v18` without editing the ticket.
- [ ] A multi-hop path connects the new decision to the active plan.
- [ ] One in-scope task is invalidated while one sibling remains valid.
- [ ] The executor rejects the stale grant and the loop successfully replans.

---

## Non-goals for the hackathon

Do not spend the build window on:

- live Slack, Linear, or GitHub OAuth,
- broad company-wide ingestion,
- production authentication,
- a general-purpose company brain,
- multiple demo scenarios,
- elaborate agent personas,
- or polished enterprise administration screens.

The product is proven by selective invalidation changing a running agent's behavior.
