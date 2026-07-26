# Product specification

## Product

**Dragback** is a continuous intent-control layer for autonomous coding agents.

## User problem

A coding agent can implement a ticket exactly as written even after the company has changed the decision that justified the ticket. Traditional tests prove functional correctness against the local task; they do not prove that the task still represents current approved intent.

## Initial user

Engineering teams running autonomous or long-running coding agents across tickets, specifications, and pull requests.

## Job to be done

Before a coding agent performs a consequential action, determine whether the objective and plan are still supported by current approved company decisions. When upstream intent changes, invalidate affected work, preserve unaffected work, and cause the loop to replan or escalate.

## Core scenario

- Initial decision: all users may export data.
- Specification: build CSV export.
- Ticket decomposes into:
  - generate valid CSV files;
  - expose export to all users.
- Agent starts, completes the plan, and passes tests.
- New approved compliance decision: exports must be admin-only.
- The new decision supersedes only the `export.authorization` scope.
- CSV generation remains valid; all-user exposure becomes invalid.
- The old authorization is rejected and the agent replans.

## Functional requirements

1. Store typed company artifacts and provenance relationships.
2. Distinguish proposals from approved decisions.
3. Apply authority rules by scope.
4. Traverse downstream lineage from a superseded decision.
5. Invalidate only intersecting scopes.
6. Version the graph after accepted decision changes.
7. Bind grants to graph version and plan hash.
8. Reject stale or mismatched grants at the executor.
9. Return `ALLOW`, `REPLAN`, `BLOCK`, or `HUMAN_REVIEW` with evidence.
10. Preserve unaffected work during replanning.

## Non-functional requirements

- Deterministic demo without external API keys.
- Explainable paths and stable fixture IDs.
- Independent authority and executor boundaries.
- Fast enough for a 3–5 minute live presentation.
- Obvious real-versus-simulated disclosure.

## Success metric for the demo

A judge can see one sibling remain valid, one sibling become invalid, the old grant fail, and the corrected plan succeed—all without the ticket being edited.
