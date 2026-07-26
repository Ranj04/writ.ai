# The enforcement backstop

> ## ✅ Required on protected `main`
>
> *Branch authorization is current* was added to `main`'s required status checks
> on 2026-07-25 with strict/up-to-date checks enabled. A red result now blocks a
> merge. If branch protection is recreated or this repository is forked, repeat
> the configuration below; an advisory workflow alone closes nothing.
>
> The click path, on GitHub:
>
> **Repository → Settings → Branches → Branch protection rules → `main` → Edit
> → Require status checks to pass before merging → search `Branch
> authorization is current` → select it → Save changes.**
>
> Rulesets instead of branch protection: **Settings → Rules → Rulesets → your
> ruleset → Edit → Require status checks to pass → Add checks → `Branch
> authorization is current` → Save.**
>
> The check must have run at least once on the repository before GitHub will
> offer it by name. Open one pull request first, then add it.
>
> Also set the `DRAGBACK_AGENT_URL` repository variable
> (**Settings → Secrets and variables → Actions → Variables → New repository
> variable**). It is deliberately not defaulted — see *Configuration* below.


The Claude Code `PreToolUse` hook **fails open**. On timeout, crash, or HTTP
failure the tool call proceeds — `hooks/dragback_hook_lib.py` says so in its own
module docstring, and `workspaces/session_enforcement.py` names a PR check as the
compensating control in two separate comments. This directory is that check.

| | Hook | This check |
|---|---|---|
| Runs | before every tool call | on every pull request |
| Service unreachable | **allows** (falls back to a cached deny, else denies that one call) | **fails the check** |
| Removable | yes, unless org-managed settings pin it | no, when it is a required status check |
| Blocks | one tool call | the merge |

## Files

- `dragback_ci_check.py` — the check. Standard library only, 3.9-compatible, so
  it runs unchanged on a developer machine and on a bare runner.
- `verify.sh` — run the identical logic locally, before pushing.
- `test_dragback_ci_check.py` — its tests. No network, no installed package.
- `../../.github/workflows/dragback-pr-authorization.yml` — the workflow.

## What it does

1. **Resolves the branch to a task** using the same fixed order as session
   binding (`workspaces/session_binding.py`): explicit `.dragback/attach`, then
   an exact task ID in the branch name, then `.dragback/task`, then unbound.
   Ambiguity resolves to unbound, never to a guess. The candidate set is
   filtered exactly as `list_live_claude_assignments` filters it — live
   supervisor, live assignment, enforceable provider, not completed.
2. **Asks the agent service** (`GET /live-workspaces`) for that task's current
   state. It reads state; it never decides authority, and no model output
   reaches a verdict. A well-formed response with no bindable candidates is
   visibly `UNBOUND`; a malformed workspace or assignment candidate fails
   closed instead of being discarded into that passing state.
3. **Fails** when the assignment was interrupted, when its snapshot is
   superseded, or when the grant covering it is stale, expired, or not an
   `ALLOW`.

The failure text carries what the hook's deny block carries: what is still
valid, what no longer is, what is now required, the affected scopes, the
provenance path, and an evidence ref.

## Verdict order — the ordering is load-bearing

`CONTINUING` is checked **before** the snapshot comparison, exactly as
`ClaudeCodeSessionEnforcement.check` does. `_apply_supervisor_invalidation`
moves a scope-preserved sibling `RUNNING → CONTINUING` *without* advancing its
decision snapshot, so a snapshot-first order would fail precisely the tasks the
scope intersection deliberately spared. Out-of-scope siblings survive.

`invalidated_task_ids` feeds the explanation and never the gate. Nothing clears
a downgraded validity, so a task that was invalidated, redirected and
re-authorized stays on that list forever; gating on it would fail that branch
permanently. The gate is snapshot equality.

## Running it locally

```bash
scripts/ci/verify.sh                        # check the current branch
scripts/ci/verify.sh --json                 # machine-readable verdict
scripts/ci/verify.sh --require-binding      # treat an unbound branch as a failure
scripts/ci/verify.sh --allow-missing-grant  # tolerate a bound branch with no grant
scripts/ci/verify.sh --self-test            # run the check's own tests
```

Exit codes: `0` authorized · `1` not authorized · `2` unreachable · `3` usage.

## Configuration

| Variable | Meaning |
|---|---|
| `DRAGBACK_AGENT_URL` | Base URL of the agent service. Required on a runner. |
| `DRAGBACK_CI_TIMEOUT_SECONDS` | HTTP timeout, default 10. |
| `DRAGBACK_CI_API_KEY` | Optional; sent as a request header. |

`DRAGBACK_AGENT_URL` is deliberately **not** defaulted on a runner. A check
silently pointed at a service that cannot exist there would pass by accident,
and "passed because it was misconfigured" is the failure mode this exists to
prevent.

## Make it required

See the banner at the top of this file. It is the one step that turns this from
a report into enforcement, and it is not something the repository can do for
itself.

## The two defaults, and why they differ

An **unbound** branch passes. A **bound** branch whose workspace has issued no
grant at all **fails**.

That asymmetry is deliberate, and it is scoped by *where* the rule is applied
rather than by a flag. `evaluate` returns at step 1 for an unbound branch and
never reaches the grant check, so a docs PR — which resolves to no assignment —
cannot be failed by the grant requirement. Only a branch that actually resolves
to a live Claude Code assignment has to show a current grant, and a task under
active supervision with nothing authorizing it is precisely the state this check
exists to catch.

| | Default | Turn it around with |
|---|---|---|
| Unbound branch | passes | `--require-binding` |
| Bound branch, no grant | **fails** | `--allow-missing-grant` |

`--require-grant` is still accepted so existing invocations keep parsing; it is
now the default and the flag is a no-op.

## The redirected branch must be able to pass again

A branch that was authorized, invalidated by an approved change, redirected, and
then re-authorized against the new snapshot **passes**. Getting this wrong is
not a corner case — it fails the developer who did exactly what the redirect
instruction asked, and it fails them permanently.

The trap is `invalidated_task_ids`. Nothing in the engine ever clears a
downgraded validity (`_mark_artifact` only downgrades, and nothing clears
`invalidated_scopes`), so once a task is on that list it stays there for the
life of the workspace. A gate that consults it can never be satisfied again. The
gate is **snapshot equality**; the report feeds the explanation and never the
verdict. `RedirectedBranchTests` covers both halves — that the re-authorized
branch passes, and that the same branch fails before it is re-authorized, so the
pass is earned rather than the check going blind.
