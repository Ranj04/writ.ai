# Provenance graph schema

## Node types

| Kind | Purpose | Important fields |
|---|---|---|
| `Decision` | approved, proposed, or rejected company intent | approval status, authority role, effective time, scopes, requirements |
| `Specification` | structured product or engineering requirement | scopes, source reference |
| `Ticket` | work request derived from a specification | scopes, validity |
| `Task` | selectively invalidatable subtask | scopes, validity, invalidated scopes |
| `AgentPlan` | proposed execution plan | actions, plan hash, scopes |
| `AgentRun` | loop instance pinned to a graph version | state, graph snapshot, grant ID |
| `Evidence` | source text or artifact reference | source, span, confidence |
| `PullRequest` | proposed code delivery | plan hash, grant ID |

## Relationship types

| Relationship | Direction | Meaning |
|---|---|---|
| `SUPERSEDES` | new decision -> old decision | new decision replaces or narrows old intent for one or more scopes |
| `AMENDS` | new decision -> old decision | partial modification without complete replacement |
| `CONTRADICTS` | artifact -> artifact | unresolved conflict; normally routes to human review |
| `BASIS_FOR` | decision -> specification | decision justified the specification |
| `CREATES` | specification -> ticket | specification produced a work item |
| `DECOMPOSES_TO` | ticket -> task | ticket contains independently scoped subtasks |
| `CURRENTLY_DRIVES` | task -> agent plan | task is implemented by the active plan |
| `IMPLEMENTS` | plan -> pull request | plan produced a code change |
| `SUPPORTED_BY` | artifact -> evidence | provenance for a claim or relationship |

## Demo path

```text
DEC-018
  -[SUPERSEDES scope=export.authorization]-> DEC-004
  -[BASIS_FOR]-> SPEC-009
  -[CREATES]-> TICKET-100
  -[DECOMPOSES_TO]-> TASK-102
  -[CURRENTLY_DRIVES]-> PLAN-027
```

`TASK-101` also descends from `TICKET-100`, but its only scope is `export.generation`, so it survives the `export.authorization` change.

## Validity semantics

- `VALID`: no invalidated scope.
- `NEEDS_REVIEW`: some scopes invalidated, but other scopes remain valid.
- `INVALIDATED`: all scopes are invalidated.

## Traversal rule

For each downstream artifact:

```text
intersection = artifact.scopes ∩ changed_decision.affected_scopes
```

- Empty intersection: preserve artifact and stop propagation on that branch.
- Partial intersection: mark `NEEDS_REVIEW`, record affected scopes, continue.
- Full intersection: mark `INVALIDATED`, record affected scopes, continue.

The traversal result must retain the exact path used for every invalidated artifact.
