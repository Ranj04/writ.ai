# AGENTS.md — writ.ai implementation contract

**Highest-priority repository instruction for Codex and other coding agents.** `CLAUDE.md` is the Claude-Code-facing twin and must stay substantively identical — if you change one, change the other in the same commit. (Codex reads `AGENTS.md` from project root down to cwd, first match per directory, 32 KiB cap. Claude Code reads `CLAUDE.md` and does *not* read this file.)

---

## What writ.ai is

writ.ai is a permission layer for autonomous agents. It proves, deterministically, that a run loses its authorization when an approved upstream decision changes — with scope-aware selective invalidation, snapshot-bound grants, and an executor that refuses stale grants.

**We are now building the product on top of that engine:**

> writ.ai watches an organisation's communication for decisions that change what work is authorised, routes each candidate change to whoever has authority over it, and — on approval — reaches into every teammate's running coding-agent session and corrects it.
>
> **One person acknowledged the change. Five agents inherited it.**

---

## Product invariant — never violate

> **The LLM may propose structure. Deterministic code decides and enforces. A human confirms.**

No model output ever becomes an `ALLOW`, `REPLAN`, `BLOCK`, or `HUMAN_REVIEW` verdict — not directly, not via a confidence shortcut, not by an agent asserting it already checked.

## Engineering invariants

1. **Newest is not automatically authoritative.** A change applies only when it passes deterministic approval, authority, scope and confidence rules, *and* a human with the right permission confirms it.
2. **Invalidation is scope-sensitive.** Intersect affected scopes with each descendant's scopes. Out-of-scope siblings survive. Never blanket-propagate.
3. **The graph drives behaviour.** Graph traversal produces the invalidation path used by the authority decision. It is not decorative.
4. **Agent, authority, and executor stay separate.** The planner cannot approve itself. `SupervisorRuntimeAdapter`'s docstring says it: *"Agent-service runtime boundary; it never returns an authority verdict."* Keep that true.
5. **Grants and assignments are snapshot-bound.** A grant binds `run_id`, `task_id`, `decision_snapshot`, `plan_hash`, `verdict`, `expires_at`. An assignment binds `decision_snapshot`. A mismatch makes it unusable.
6. **Explain every verdict and every interrupt** — affected scopes, provenance path, invalidated artifacts, preserved artifacts, evidence refs. A red badge is not sufficient.
7. **Never label a replayed payload as live.** The real-vs-simulated discipline in this repo is an asset. Preserve it.
8. **Surgical changes only.** Touch what the task requires. Do not refactor adjacent code. Do not delete pre-existing dead code — mention it. Match existing style.

---

## What already exists — do not rebuild it

- **`workspaces/supervisor.py`** already models the product. `SupervisorAssignment` carries `task_id`, `agent_name`, `runtime_provider` (`generic` | `codex` | `claude-code`), `run_id`, `scopes`, `decision_snapshot`, `interrupt_reason`, `redirect_instruction`, `provenance_path`, `interrupt_enforced`, `redirected_from_run_id`. State machine: `QUEUED → RUNNING → INTERRUPTED → REDIRECTED → RESUMED → COMPLETED`. There is a `SupervisorRuntimeAdapter` Protocol with exactly one implementation, `FixtureSupervisorRuntime`, and `SupervisorExecutionMode` has exactly one member, `SIMULATED`. **The product is the second adapter, not a new subsystem.**
- **`integrations/callwright.py`** is a complete grant-gated real-phone-call executor: `LiveCallwrightClient`, `FixtureCallwrightClient`, idempotent attempt store, target allowlist. `services/executor_api.py` verifies the grant before submitting.
- **Workspace document import** already implements the trust model: untrusted draft → extraction confidence in evidence metadata → explicit human confirmation of fields → recorded separately → then server validation. Slack is a new *source* for this path, not a new trust model.
- **`services/events.py`** has a real SSE `EventBroker` — process-local, 100-event history. Fine for the demo; a known limit for multi-machine use.
- **`cli.py`** is ~51K with `workspace` and `agent` command groups. Extend it; do not start a new entry point.

**Do not extend `domain.AgentRun`.** It is embedded as `ScenarioDefinition.initial_run`, its `plan: AgentPlan` is required with no default, and `loop/workflow.py` passes `task_id=self.run.ticket_id` in four places. `SupervisorAssignment` already carries what the product needs.

---

## Enforcement: the Claude Code hook

Even though you are Codex, you are building the adapter that talks to Claude Code sessions. The contract:

`PreToolUse` fires before every tool call, synchronously, and blocks via exit code 2 or:

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "allow|deny|ask",
  "permissionDecisionReason": "one line the model reads",
  "additionalContext": "≤10000 chars, also reaches the model"}}
```

`SessionStart` (matchers `startup`, `resume`, `clear`, `compact`, `fork`) and `SessionEnd` bracket the session. Hook stdin carries `session_id`, `transcript_path`, `cwd`, `permission_mode`, `tool_name`, `tool_input`, `tool_use_id`.

**Hooks fail open.** On timeout, crash, or HTTP failure the tool call *proceeds*. Only an explicit `deny` blocks. The hook script must catch its own errors and emit `deny`, keep an explicit short timeout (command default is 600s), and cache the last verdict on disk. The PR check is the backstop; say so honestly.

**`allowManagedHooksOnly: true`** in organisation-managed settings cannot be overridden by project or user settings. That is how enforcement becomes non-removable.

**`CLAUDE.md` does not reload mid-session.** Editing it is persistence for the *next* session, never an interrupt.

**Privacy, non-negotiable:** the hook sends only `session_id`, `tool_name`, and a timestamp. Never tool input, file contents, or transcripts.

---

## Engine preconditions that will bite

- `apply_decision_change` requires a `supersedes_id` naming an **existing** `Decision`, with `affected_scopes ⊆ superseded.scopes`, **and** a three-way exact match between `decision.scopes`, `mutation.affected_scopes`, and the keys of `decision.attributes["requirements"]` (`engine.py:122-179`). A decision introducing a constraint on an uncovered scope cannot be applied at all.
- Always set `effective_at`. `current_requirements()` orders by it and defaults `None` to `datetime.min` (`engine.py:344-350`), so an "effective immediately" decision without a timestamp sorts to the *bottom* of precedence.
- `authority_policy` compares a **single** `decision.authority_role` string against a set (`engine.py:136-140`). Seed permission ids (`approve_compliance`), not role names.
- `runtime.py:23-34` constructs `IntentAuthority` with no `authority_policy`. It needs an injection point. Do not use `ScenarioDefinition.authority_policy` — its model requires a `mutation: DecisionMutation`.
- `_mark_artifact` only ever downgrades validity. Nothing clears `invalidated_scopes`. Never write a gate that depends on a task returning to `VALID`.

---

## Definition of done

Do not call anything done unless all of these hold:

- The canonical CSV proof, all 12 Examples, and the entire existing test suite pass **unchanged**.
- `FixtureSupervisorRuntime` still reports `SIMULATED`.
- A session bound to a current assignment is allowed; one whose assignment was interrupted on an intersecting scope is denied **once**, with a payload under 10,000 characters; the next call is allowed.
- An assignment interrupted on a scope the session's task does not carry does **not** deny that session.
- The hook emits `deny` when the service is unreachable, and the cached-verdict path is covered by a test.
- The hook payload contains no tool input, file contents, or transcript data — asserted by a test.
- Every approval channel resolves through one shared permission check.

**Not a success criterion:** "the agent replans correctly." That is model behaviour, not determinism. Track it as a rehearsal risk.

## Commands

```bash
make demo      make test      make check
make authority make agent     make executor
```

## Codex-specific operating notes

- `codex exec` defaults in a trusted git repo: `approval: never`, `sandbox: workspace-write`, `model: gpt-5.6-sol`.
- **`workspace-write` has `network_access = false`.** `pip install` will fail inside the sandbox. Install dependencies *before* invoking codex, or pass `-c sandbox_workspace_write.network_access=true`.
- In scripts always redirect `< /dev/null`, or `codex exec` blocks reading stdin.
- `-m gpt-5.6-sol` is the real identifier. **`gpt-5.6` and `gpt-5.6-codex` do not exist** — `-m` accepts any string without validation and silently degrades.

## Code style

Small typed functions with explicit inputs and outputs. Keep authority logic pure where practical. Add or update tests with every behaviour change. No hidden global mutation outside service runtime modules. UTC-aware datetimes. Keep fixture IDs stable — the demo and tests reference them. Do not rename the product away from **writ.ai**.

## Do not spend time on

Microsoft Teams / Discord / email intake · agent runtimes other than Claude Code · a mobile app · editing `CLAUDE.md`/`AGENTS.md` as an interrupt mechanism · a parallel run registry if the orchestrator already recomputes assignments · extending `domain.AgentRun` · depending on Superset's undocumented `terminals_send` at runtime · auto-applying changes without human confirmation · elaborate cryptography (HMAC is sufficient) · live OAuth beyond what the sponsor keys require.
