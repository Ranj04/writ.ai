# Demo script — 3 to 5 minutes

## Presenter entry

Use one of these browser routes after `make stack`:

- `/` for the primary user-owned Workspace flow;
- `/scenario-lab?demo=1` for the deterministic CSV backup presentation;
- `/scenario-lab` to show the 12 seeded Examples and Run All report;
- `/scenario-lab?scenario=api-read-only` to deep-link another example.

For the primary live demo, import the starter document or a prepared user document in Workspace,
then advance through baseline approval, initial authorization, decision proposal and approval,
stale-grant verification, plan correction, reauthorization, and replacement verification.
The built-in starter is preloaded with a fresh workspace ID whenever Workspace opens. Selected
files also receive a fresh visible run ID, so each rehearsal starts clean while prior verification
records remain available.

For the seeded CSV backup, use the primary action once per narrative moment:
**Start with authorized work**, **Approve the change**, **Show affected work**,
**Check old authorization**, and the final correction action. Keep **Guided story** open for the
main narrative. Open **Impact map** only to show the decision lineage, and open
**Technical evidence** only for grant metadata, the timeline, evaluation, or source references.
The query string selects the opening view only; it does not bypass backend authority checks.

## 0:00–0:25 — problem

“Coding agents can write correct code from obsolete decisions. Tests tell an agent whether the code works. writ.ai tells it whether the work is still wanted.”

## 0:25–1:00 — initially valid run

Show:

- ticket: “Add CSV export for all users”;
- two tasks: CSV generation and all-user exposure;
- `graph-v17`;
- `ALLOW` grant;
- implementation complete and tests passing.

Say: “At this point the agent is doing exactly what it was asked to do.”

## 1:00–2:15 — upstream decision changes

Ingest the approved compliance decision:

> Exports must be admin-only.

Emphasize:

- it does not mention the ticket;
- it is approved and authoritative for `export.authorization`;
- the graph traces a multi-hop path to the active plan.

Show the path lighting up.

Point to the two separate payload summaries:

- upstream provenance chain: `DEC-018 → DEC-004 → SPEC-009 → TICKET-100`;
- invalidated task: `TASK-102`;
- plan needs review: `PLAN-027`.

Say: “The approved decision text never names `TICKET-100`; the graph finds it through lineage.”

## 2:15–2:50 — selective invalidation

Show side by side:

```text
TASK-101 Generate CSV files        VALID
TASK-102 Expose to all users       INVALIDATED
```

Say: “If both tasks turned red, this would be blanket recursion. One survives because the new decision only changed authorization.”

## 2:50–3:25 — executor rejects stale work

Attempt execution with the original grant. Show:

```text
REJECTED
Grant snapshot: graph-v17
Current snapshot: graph-v18
```

The executor, not the agent or UI, performs this check.

## 3:25–4:00 — replan

Show the loop move to `REPLAN`. The fixture-generated corrective plan preserves CSV generation and
proposes changing the audience to `admin_only`. Its plan actions are not persisted graph Tasks. The
authority evaluates it, issues a new `graph-v18` grant, and the executor accepts it.

If asked how general the replanner is, say: “Enforcement, traversal, invalidation, and
reauthorization are general. The corrective `PLAN-028` text is a deterministic demo template; a
production planner would propose that candidate, then pass through the same authority checks.”

## Code peek — optional 30 seconds

Show only:

1. the downstream traversal;
2. the scope-intersection rule;
3. the snapshot and plan-hash checks.

## Close

“Most agent controls ask whether the agent may act. writ.ai verifies whether the objective driving that action still follows from current approved company intent.”

> Tests prove the code works. writ.ai proves the work is still wanted.

## Examples Q&A

If asked whether the other examples are frontend mocks, say: “No. All 12 use typed backend graph
fixtures and the same deterministic authority engine. Every run creates an isolated in-memory
authority context, and the agent calls the authority and executor over HTTP.”

If asked what is simulated, say: “The scenario inputs, provenance fixtures, corrected-plan wording,
proposed corrective actions, and mock pull request are fixture-driven. Corrective actions are plan
actions, not persisted graph Task nodes. Graph writes, policy checks, traversal, invalidation,
grant signing, structured stale-grant rejection, reauthorization, and evaluation are real.”

If the canonical runtime is using Neo4j, clarify: “Examples intentionally use a separate
`MemoryGraphStore` per run, so Run All cannot erase or mutate the configured Neo4j database.”

If asked whether Run All is a durable benchmark, say: “No. It runs the same isolated service flow
serially and keeps session-only, process-local results. Restarting the agent service clears that
history.”
