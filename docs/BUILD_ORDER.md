# Five-hour build order

The scaffold already implements the deterministic core. Use this order when extending or polishing it.

## 0:00–0:30 — establish a green baseline

- Read `AGENTS.md` and the product brief.
- Run `make demo` and `make test`.
- Do not touch the UI until the deterministic flow is stable.

## 0:30–1:30 — make graph reasoning undeniable

- Inspect and strengthen selective invalidation.
- Confirm one sibling survives.
- Add evidence references and exact invalidation paths.
- Confirm proposals and low-confidence changes do not mutate the graph.

## 1:30–2:15 — enforce the boundary

- Run authority, agent, and executor separately.
- Verify old grants fail for graph mismatch.
- Verify changed plans fail for plan-hash mismatch.
- Verify a corrected `graph-v18` plan succeeds.

## 2:15–3:15 — operator interface

- Connect the React UI to the three services.
- Show loop state, graph version, active path, sibling statuses, and grant payload.
- Add a real-versus-simulated panel.

## 3:15–4:00 — Neo4j or extraction, not both unless ahead

Preferred: switch the graph store to Neo4j and prove parity.

Optional: add Anthropic extraction only after deterministic fixtures work perfectly.

## 4:00–5:00 — presentation hardening

- Follow `docs/DEMO_SCRIPT.md`.
- Remove extra UI and extra scenarios.
- Keep named competitor comparison for Q&A.
- Record a backup demo.
- Freeze fixtures and IDs.
