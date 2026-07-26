# Architecture decisions

## ADR-001 — deterministic authority

LLMs may extract candidates but may not issue execution verdicts. Reason: authorization must be auditable and reproducible.

## ADR-002 — memory graph default

The demo defaults to an in-memory store. Reason: zero-setup reliability. Neo4j is an adapter, not a runtime prerequisite.

## ADR-003 — scope-aware propagation

Every artifact carries scopes. Invalidation propagates only through intersecting scopes. Reason: blanket descendant invalidation is not credible.

## ADR-004 — independent executor verification

The executor calls the authority service to verify a grant. Reason: a warning banner is not enforcement.

## ADR-005 — snapshot and plan binding

Grants bind to graph version and stable plan hash. Reason: a valid grant must not authorize a changed plan or changed company intent.

## ADR-006 — fixture-first extraction

Seeded decisions and edges are the default. Anthropic extraction is optional. Reason: the live proof should not depend on nondeterministic parsing.

## ADR-007 — competitor names remain in Q&A

The default pitch uses category-level differentiation. Reason: named comparisons are useful only after judges understand Dragback's mechanism.

## ADR-008 — extracted evidence is an untrusted candidate

An extractor must return exact character spans from the supplied source. Deterministic
ingestion code validates the offsets and text, then replaces every governance field with
authenticated `TrustedDecisionContext`: source, approval, role, confidence, scopes, effective
time, and supersession target. It also normalizes the candidate decision to `VALID`
with no invalidated scopes before submitting the mutation to the authority engine. Invalid
evidence or low-confidence trusted context returns `HUMAN_REVIEW` without a graph write. Reason:
an LLM may propose provenance structure, but it cannot create evidence, claim authority, carry
forward invalidation state, control precedence, or issue a verdict.

## ADR-009 — destructive graph seeding requires backend-aware opt-in

The in-memory backend seeds the fixture automatically in local development/demo environments so
the deterministic demo remains zero-config. Neo4j never enables destructive startup seeding or
reset by default; `DRAGBACK_DEMO_RESET_ENABLED=true` must be supplied explicitly and the target
must be a dedicated demo database. Reason: connecting a development process to a remote graph must
not make database deletion an implicit startup side effect.
