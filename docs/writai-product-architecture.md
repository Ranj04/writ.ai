# writ.ai — product architecture v1

> **One person acknowledged the change. Five agents inherited it.**

writ.ai watches an organisation's communication for decisions that change what work is authorised, routes each candidate change to whoever has authority over it, and — on approval — reaches into every teammate's running coding agent and corrects it. Nobody has to have seen the Slack message. Nobody has to be at their desk.

**Settled:** build the real product (procurement dropped) · a human confirms with evidence, then fan-out is automatic · Claude Code only for v1 · CLI first, desktop app second.

---

## 0. What you already have — read this before planning anything

The `writ.ai` repo (renamed from `lookback`, single commit `40bd8ea`) is substantially further along than the earlier planning assumed. Three things in particular:

**`workspaces/supervisor.py` already models this product.** `SupervisorAssignment` carries `task_id`, `agent_name`, `runtime_provider` (`generic` | `codex` | `claude-code`), `run_id`, `scopes`, `decision_snapshot`, `interrupt_reason`, `redirect_instruction`, `provenance_path`, `interrupt_enforced`, and `redirected_from_run_id`. Its state machine is `QUEUED → RUNNING → INTERRUPTED → REDIRECTED → RESUMED → COMPLETED`. There is a `SupervisorRuntimeAdapter` **Protocol** whose docstring already encodes the invariant — *"Agent-service runtime boundary; it never returns an authority verdict."*

There is exactly one implementation, `FixtureSupervisorRuntime`, and `SupervisorExecutionMode` has exactly one member, `SIMULATED`, with the comment *"The demo runtime is explicit about not controlling a real provider process."*

**That sentence is the entire gap.** The product is not "design a supervisor" — it is "write the second adapter."

**The kill path in front of a real-world action already works.** `integrations/callwright.py` (21K) has a `LiveCallwrightClient` against `https://api.voygr.tech`, a fixture client, an idempotent attempt store (in-memory and file-backed, `reserve`/`transition`), `select_callwright_action`, and `build_call_request(action, verified_grant, allowed_targets)` — grant-bound, with a target allowlist. `services/executor_api.py` verifies the grant against the authority and only then submits the call, gated by `execution_provider`, `callwright_live_calls_enabled`, an API key and a configured demo number.

**The human-confirmation trust model already exists.** Workspace document import (PDF, DOCX, MD, TXT, PNG/JPEG/WebP with locally bundled OCR) produces an *untrusted draft*; OCR confidence is preserved in evidence metadata; the user must explicitly confirm extracted fields before server validation; that confirmation is recorded separately; and per the README, *"extraction never approves a decision or issues a verdict."* Slack intake is the same pattern with a different source — not a new trust model.

**Files I have NOT read** and whose behaviour the plan below assumes: `workspaces/orchestrator.py` (34K), `services/agent_api.py` (21K), `services/authority_api.py` (12K), `workspaces/models.py` (21K), `workspaces/authority_contexts.py`, `scenarios/runner.py`, `cli.py`. **Phase 0 must read them first.** In particular, the orchestrator almost certainly already recomputes assignments on a decision change — it has to, in order to produce `INTERRUPTED` and `REDIRECTED` — so the "which scopes changed between versions" question may already be answered there. Do not build a parallel mechanism before checking.

---

## 1. Enforcement: the Claude Code hook

Verified against the current hook docs. A **PreToolUse** hook is a local command that runs before every tool call, synchronously, and can deny it while returning text the model reads:

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "deny",
  "permissionDecisionReason": "writ.ai: the decision this task rests on changed.",
  "additionalContext": "<redirect_instruction + provenance, ≤10000 chars>"}}
```

Both `permissionDecisionReason` and `additionalContext` reach the model's context. `additionalContext` is capped at **10,000 characters**.

The payload it carries is `SupervisorAssignment.redirect_instruction` plus `provenance_path` — fields that **already exist** in your supervisor model. (Note: the five field names in `writai.md` §9 — `current_objective`, `invalidated_requirement`, `new_constraint`, `preserved_work`, `required_changes` — are product-brief prose, not Python. The only one present in code is `ScenarioPresentation.preserved_work`, which the README labels fixture presentation copy. Build the redirect payload from `redirect_instruction`, `provenance_path`, and the real `InvalidationReport` fields.)

**Three facts that shape the design:**

**Hooks fail open.** On timeout, crash, or HTTP failure the tool call **proceeds**. Only an explicit `deny` (or exit code 2) blocks. So the hook script must catch its own errors and emit `deny` rather than dying, must set an explicit short timeout (the command-hook default is 600s), and should cache the last verdict on disk so a network blip degrades to the last known state rather than to no enforcement. Say this plainly in the pitch — and point at the PR check as the backstop.

**Managed settings make hooks non-removable.** `allowManagedHooksOnly: true` in organisation-managed settings cannot be overridden by project or user settings. A developer cannot disable an org-enforced hook locally. That is the "enforcement is mandatory" claim, made concrete.

**`CLAUDE.md` does not reload mid-session** — documented, explicitly excluded from hot-reload. It is a persistence mechanism for the *next* session, never the interrupt. Use the hook to interrupt; write files only to make a correction durable.

**Privacy, decided up front:** the hook sends only `session_id`, `tool_name`, and a timestamp. Never tool input, file contents, or transcripts. It costs nothing and decides whether a security review passes.

---

## 2. The work: a live `SupervisorRuntimeAdapter`

### 2.1 Extend the enum, implement the Protocol

Add `SupervisorExecutionMode.LIVE`. Both `WorkspaceSupervisor.execution_mode` and `SupervisorAssignment.execution_mode` are currently `Literal[SIMULATED]`; widening them is the one deliberate model change, and `FixtureSupervisorRuntime` must keep returning `SIMULATED` so every existing test and Example is untouched.

Then write `ClaudeCodeSupervisorRuntime` implementing the existing Protocol. It differs from the fixture in exactly one way: `transition(..., state=INTERRUPTED, redirect_instruction=..., interrupt_enforced=True)` records something a *real* hook will read and act on.

**Do not extend `domain.AgentRun`.** It is embedded as `ScenarioDefinition.initial_run`, its `plan: AgentPlan` is required with no default, and `loop/workflow.py` passes `task_id=self.run.ticket_id` in four places. `SupervisorAssignment` already carries everything the registry needs.

### 2.2 Session binding

A **SessionStart** hook POSTs `{session_id, cwd, branch}`; the runtime binds that session to a `SupervisorAssignment`. **SessionEnd** releases it.

Binding must be deterministic, never model-inferred: explicit `writai attach ASSIGNMENT-TASK-102` → task id parsed from the branch name (`feat/TASK-102-csv-export`) → `.writai/task` file → **unbound**. Unbound sessions register successfully and are allowed everything, but show as unbound in the CLI so the gap is visible rather than silent.

### 2.3 The hook verdict endpoint

A new endpoint on the **agent service** (`services/agent_api.py`), not the executor. `services/executor_api.py` is the *Callwright* executor: it is titled "writ.ai Mock Executor", requires a `token` and a full `AgentPlan` in `ExecuteRequest`, forwards to `{authority_url}/grants/verify`, and holds no graph. The hook cannot supply a plan and must not.

```
POST /supervisor/sessions/{session_id}/check   ->  {decision, reason, redirect_instruction, provenance_path}
```

Resolution: look up the assignment; if `assignment.decision_snapshot == current` → allow. If the assignment is `INTERRUPTED` and not yet redirected → deny once, return the redirect payload, transition to `REDIRECTED`, and advance `decision_snapshot`. If the assignment was invalidated outright rather than redirected → keep denying until a human acknowledges in the CLI.

**Deny once, then advance** is what makes this terminate — and it is literally the product name. Two things to be honest about:

- Advancing the snapshot on delivery means the run is marked current whether or not the agent actually complied. `evaluate_plan` re-checks a plan against `current_requirements()` on every call and keeps returning `REPLAN` until it genuinely matches; the session-level check has no equivalent, because writ.ai does not hold the session's plan. **The compliance guarantee here is weaker than the grant path's, and the PR check is what closes it.** Do not claim otherwise on stage.
- "The agent reads the redirect and replans itself" is a *model-behaviour* outcome, not a deterministic one. Every other success criterion in this build is deterministic; this one is not. Rehearse it, and have `writai why` on screen so a human can see the correction even if the agent handles it clumsily.

---

## 3. Intake and approval

```
Slack message
   ↓  Composio  SLACK_CHANNEL_MESSAGE_RECEIVED  (realtime push, signed webhook)
LLM extraction  →  untrusted draft: decision proposal, scopes, requirement delta, evidence spans
   ↓  DETERMINISTIC GATE: author authoritative for these scopes (Hexclave)? confidence ≥ threshold?
Pending change  →  the approver confirms the extracted fields, with the blast radius
   ↓
apply_decision_change()  →  version bump  →  assignments recomputed  →  hooks deny once
```

This is the *existing* Workspace import flow with Slack as a new source. Reuse the untrusted-draft + explicit-confirmation path rather than building a second one.

**Two engine preconditions the Slack path must respect** — `apply_decision_change` will reject otherwise (`engine.py:122-179`):

1. It requires a `supersedes_id` naming an **existing** `Decision`, and `affected_scopes ⊆ superseded.scopes`. A Slack decision introducing a constraint on a scope no prior decision covers **cannot be applied at all**. So each workspace needs a seeded baseline policy decision covering its scopes, or the intake needs an explicit "new scope" path. Decide this in Phase 0; it is the most likely thing to derail Phase 2.
2. `decision.scopes`, `mutation.affected_scopes`, and the keys of `decision.attributes["requirements"]` must match **exactly**, three ways.

Also set `effective_at` on every Slack-derived decision. `current_requirements()` orders candidates by `effective_at` and defaults `None` to `datetime.min` (`engine.py:344-350`), so an "effective immediately" message ingested without a timestamp sorts to the *bottom* of precedence.

**Put the blast radius in the confirmation screen** — the message, its author, the extracted scopes, the requirement delta, and *"3 of 5 active sessions will be interrupted — Priya, Marcus, Dan."* Build it from the assignment list. That element is what makes the product feel safe rather than invasive, and no competitor screenshot has it.

---

## 4. Surfaces

**CLI first.** `cli.py` is already 51K with two command groups, `workspace` and `agent` — extend it rather than starting a new entry point. Add `writai attach`, `status`, `why`, `pending`, `approve`, `watch`. `why` renders the provenance path from the Slack message to this session's task with the evidence span — the repo already returns `provenance_path` on the assignment.

### 4.1 Notification and approval — the approver is not in the terminal

CLI-first does **not** mean CLI-only, because two different people need two different channels.

**The interrupted developer needs no push at all.** Their agent stops and prints the reason in their own terminal — that *is* the notification, and it lands exactly where the work is. Two free additions: the `PreToolUse` hook script runs locally, so on a deny it can fire a native OS notification (`osascript -e 'display notification …'` on macOS, `notify-send` on Linux) with zero infrastructure; and `writai watch` streams the same events over the existing SSE broker.

**The approver is a lead, PM, or compliance person who is probably on a phone.** Four channels, in build order:

1. **Slack itself — the right v1 answer.** The change came from Slack; the approval should go back to Slack. You inherit iOS, Android and desktop apps, push notifications, and identity, for free, with no new client to build.
   **Approve by emoji reaction.** writ.ai posts a threaded message carrying the extracted fields and the blast radius; the approver reacts ✅ or ❌. Composio exposes `SLACK_MESSAGE_REACTION_ADDED` as a real trigger, so this needs no Slack app interactivity endpoint, no Block Kit callback URL, and no extra infrastructure. It is the cheapest correct design.
   Block Kit buttons are nicer, but the button click POSTs to *your* endpoint, not Composio's, so they require your own Slack app with an interactivity request URL. Keep that for v2.
   **The caveat matters:** a reaction is a weaker authentication signal than a signed button click, and reactions can be removed. Mitigate by resolving the *reacting user's* id, checking their Hexclave permission **at reaction time** rather than at post time, treating the first qualifying reaction as binding, and recording it as evidence. A later un-react does not un-approve.
2. **Phone push without building an app — ntfy or Pushover.** One HTTP POST gives real iOS/Android push, and both support action buttons that call back an HTTP endpoint, so approve-from-lock-screen works. ntfy is open source and self-hostable, which matters for the enterprise conversation. Roughly an hour of work and it covers the away-from-desk case properly.
3. **Email with a signed magic link.** Works everywhere, needs no install, and produces an audit artifact for free. The right fallback for people who don't live in Slack.
4. **`writai pending` / `writai approve`** for approvers who do live in a terminal.

**Callwright is the backstop you already own.** An interrupt or a pending change unacknowledged past a threshold escalates to a phone call. That is the literal answer to *people aren't always at their desks*, and the idempotent attempt store already prevents duplicate dials.

**The principle to hold on to: the notification is not the approval.** Whatever channel delivers the ping, the approval itself must resolve to a Hexclave identity and a permission check, recorded with the evidence. Otherwise you have built a system where anyone who can react to a Slack message can redirect five engineers.

**One implementation note:** `services/events.py` `EventBroker` is explicitly process-local with a 100-event history — fine for the demo, but a product spanning several developers' machines needs a durable bus behind it. Flag it rather than discovering it in front of a buyer.

**Desktop app second**, and its whole job is *"we don't change what someone had going on without telling them why."* One screen: what changed, who decided it, the evidence, what your agent was doing, what it is doing now, and what work was **preserved**. The preserved list is the difference between "my agent got hijacked" and "my agent got corrected."

Mobile is v3; notifications reach a phone long before an app does.

---

## 5. Sponsor map

| Sponsor | Role | Cost |
|---|---|---|
| **Hexclave** | Identity, teams, per-user and per-company config, and who may approve a change to which scope. Note `authority_policy` compares a **single** `decision.authority_role` string against a set (`engine.py:136-140`), so map scope → sets of Hexclave **permission id strings** and seed `authority_role="approve_compliance"`, not a role name. `runtime.py:23-34` builds `IntentAuthority` with no `authority_policy`, so add an injection point. | Core, new |
| **Composio** | Slack intake. `SLACK_CHANNEL_MESSAGE_RECEIVED` is realtime push to one signed webhook; connections keyed by your own `user_id`, so multi-tenancy is native. | Core, new |
| **Callwright** | **Already integrated.** Repoint it: when a redirect goes unacknowledged for N minutes, escalate to a phone call — your own words, *people aren't always at their desks*. Reuses `LiveCallwrightClient` and the idempotent attempt store as-is. | Near-zero |
| **Superset** | Runs five isolated agent worktrees on one machine, which is what makes a five-developer demo physically possible on stage. That is its actual product. | Demo |
| **CrustData** | Person Watcher: the approver changed roles or left. An approval resting on a departed approver is exactly what should be flagged. | Stretch |
| **Channel3, VOYGR business-status** | No honest fit. | Drop |

Callwright being already built is the cheapest sponsor prize on the board — it is a working, grant-gated, allowlisted real-phone-call executor today.

**Composio caveats you must be ready for**, because this product's whole pitch is trust: message bodies necessarily transit Composio's infrastructure; there is no published retention policy, no verifiable self-hosting, no EU residency, and an unqualified SOC 2 claim; and a May 2026 breach reached their tool-execution sandbox and exposed some Slack connections. Use it for speed, know the answer, and put "own the Slack OAuth app" on the roadmap. **Teams has zero Composio triggers** — v1 is Slack only.

**Superset caveats:** `terminals_send` / `terminals_read` (push a prompt into a live Claude Code session, read its screen) are present in the repo's `main` but **absent from the docs** — confirm they are live before depending on them. Relay exposure is off by default in the desktop app but **on by default via `superset start`**. Auth has no capability scopes, so a token that reads a task can also send text into a terminal. No outbound events. macOS only.

---

## 6. The demo

The canonical CSV proof, distributed across people — which is why the existing backend tests already validate the engine half.

Five Claude Code sessions on one repo in five Superset worktrees, all pinned to the same snapshot. Two working on `export.generation`, three on `export.authorization`. A real Slack message from someone holding `approve_compliance`: *"Approved — exports must be admin-only, effective immediately."* It never mentions the ticket. writ.ai extracts it into an untrusted draft; the approver confirms the fields and sees *"3 of 5 active sessions will be interrupted"*; they approve. **Two sessions carry on untouched** — the surviving sibling, now a person. The other three are denied once at their next tool call, receive the redirect, and continue. Nobody types anything. `writai why` shows the path from the message to that session's task.

*Tests prove the code works. writ.ai proves the work is still wanted — for everyone at once.*

**Stretch:** an unacknowledged interrupt escalates to a Callwright call. CrustData reports the approver has left, flagging everything they approved.

---

## 7. Risks

1. **Hooks fail open.** Tiny script, internal error handling that emits `deny`, explicit short timeout, on-disk verdict cache. Managed settings so the hook cannot be removed. PR check as backstop.
2. **`apply_decision_change`'s supersession and three-way scope preconditions** will reject a naive Slack-derived decision. Settle the baseline-decision question in Phase 0.
3. **The agent must actually act on the redirect** — model behaviour, not determinism. Rehearse; keep `writai why` visible.
4. **Snapshot-advance-on-delivery is a weaker guarantee** than the grant path. Be honest; lean on the PR check.
5. **Composio's breach and retention gaps.** Have the answer; roadmap direct OAuth.
6. **Superset's undocumented terminal tools**, relay-on-by-default via CLI, macOS only.
7. **10,000-character ceiling** on the redirect payload.
8. **False positives are the product killer.** Human confirmation is the mitigation; do not quietly add auto-apply.
9. **Five concurrent sessions on stage.** `TASKS.md` P5 still has "record a backup screen capture" open — do it.

---

## 8. Open questions

- **The event.** `writai.md:5` names *c0mpiled-11: Startup School Hackathon, Friday 24 July 2026*, which has passed; the sponsor list you were given matches no event I could find, and Superset's `TRANSPOSE-20` code hints at an organiser named Transpose. If this repo line is stale, fix it — a judge may read it.
- **The consent model.** Reading a company's Slack *and* running a hook on every tool call are two separate trust asks. Per-developer opt-in with visible status is the honest default, and the answer to "is this surveillance" should be crisp before you demo to a buyer.
- **Product managers.** You named PMs using Claude for email. There is no PreToolUse equivalent outside coding agents, so that persona gets notify-and-acknowledge, not enforcement. Decide whether v1 claims them.
