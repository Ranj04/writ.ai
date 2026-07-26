# writ.ai master prompt — v3 (the product)

*Paste below the line into a coding agent working in the `writ.ai` repo. Supersedes both earlier prompts. Read `writai-product-architecture.md` alongside it.*

**File set.** Copy `CLAUDE.md` and `AGENTS.md` into the repo root first — they carry the invariants and the engine gotchas, and every agent session picks them up automatically. Claude Code reads `CLAUDE.md` and does **not** read `AGENTS.md`; Codex reads `AGENTS.md` and does **not** read `CLAUDE.md`, so both must exist and stay in sync.

**Two-person build.** `BUILD_LANE_A.md` (enforcement — Claude Code primary) and `BUILD_LANE_B.md` (intake and approval — Codex primary) split this prompt's Phases 1–3 into two concurrent lanes that meet at one frozen interface. If two people are building, use those instead of running this prompt end to end; this document remains the shared source of truth for scope, invariants and verified platform facts.

---

## Mission

writ.ai already proves, deterministically, that a coding-agent run loses its authorization when an approved upstream decision changes — with scope-aware selective invalidation, snapshot-bound grants, and an executor that refuses stale grants in front of a real phone call.

Turn that into the product:

> **writ.ai watches an organisation's communication for decisions that change what work is authorised, routes each candidate change to whoever has authority over it, and — on approval — reaches into every teammate's running Claude Code session and corrects it. One person acknowledged the change. Five agents inherit it.**

---

## §0 — Read the repo, then settle the contract (do this first, then stop)

**Read before writing anything.** The plan assumes behaviour in files that have not been reviewed: `workspaces/orchestrator.py`, `services/agent_api.py`, `services/authority_api.py`, `workspaces/models.py`, `workspaces/authority_contexts.py`, `cli.py`. Report what you find, specifically:

1. **Does `workspaces/orchestrator.py` already recompute supervisor assignments on a decision change?** It produces `INTERRUPTED` and `REDIRECTED` states, so it almost certainly does. If it does, **do not build a parallel run registry or a version-delta ledger** — extend what exists.
2. **How does `apply_decision_change`'s supersession requirement get satisfied today?** It needs a `supersedes_id` naming an existing `Decision` with `affected_scopes ⊆ superseded.scopes`, plus a three-way exact match between `decision.scopes`, `mutation.affected_scopes`, and the keys of `decision.attributes["requirements"]` (`engine.py:122-179`). Report how the Workspace import path satisfies this, because Slack intake must satisfy it the same way. **This is the most likely thing to derail Phase 2.**
3. **What does `cli.py` already expose**, and where does the Workspace import flow record the user's explicit confirmation of extracted fields?

Then challenge these priors in at most one page and settle them.

| # | Prior | Why | What would overturn it |
|---|---|---|---|
| P1 | **The product is a live `SupervisorRuntimeAdapter`, not a new subsystem.** | `workspaces/supervisor.py` already defines the Protocol, the assignment model (`interrupt_reason`, `redirect_instruction`, `provenance_path`, `interrupt_enforced`, `runtime_provider` incl. `claude-code`) and the `QUEUED→RUNNING→INTERRUPTED→REDIRECTED→RESUMED→COMPLETED` machine. Only `FixtureSupervisorRuntime` exists. | Evidence that the Protocol cannot express a live runtime. |
| P2 | **The Claude Code PreToolUse hook is the enforcement point.** | The only synchronous veto in the category: it denies a tool call and returns `permissionDecisionReason` plus ≤10k chars of `additionalContext` that the model reads. | A documented synchronous veto in another runtime. |
| P3 | **Deterministic code issues every verdict; the LLM proposes; a human confirms.** | This is the product, and the Workspace import path already works this way. | Near-immovable. Do not propose overturning it. |
| P4 | **Session↔assignment binding is deterministic, never model-inferred.** | A wrong binding interrupts the wrong person. | Nothing. Order: explicit attach → branch-name convention → repo file → unbound. |
| P5 | **The hook transmits only `session_id`, `tool_name`, and a timestamp.** | Never tool input, file contents, or transcripts. Decides whether a security review passes. | Ask the user before widening. |
| P6 | **Do not extend `domain.AgentRun`.** | It is embedded as `ScenarioDefinition.initial_run`, its `plan: AgentPlan` is required with no default, and `loop/workflow.py` passes `task_id=self.run.ticket_id` in four places. `SupervisorAssignment` already carries what is needed. | Nothing. |
| P7 | **Editing `CLAUDE.md`/`AGENTS.md` is persistence, not interruption.** | Documented: read at session start, explicitly excluded from hot-reload. | Nothing. |
| P8 | **Slack only for v1.** | Composio's Teams toolkit has **zero triggers**; Gmail polls with a ~15-minute floor. | A Teams path that does not blow the window. |

**Stop after §0.** Output the repo findings, the settled contract, and any prior you are overturning with evidence.

---

## Invariants

1. **No model output ever becomes a verdict.** Extraction proposes; deterministic code decides; a human confirms.
2. **Newest is not automatically authoritative.**
3. **Invalidation is scope-sensitive.** Out-of-scope work survives.
4. **Agent, authority, and executor stay separate.** The `SupervisorRuntimeAdapter` docstring already says it: *"it never returns an authority verdict."*
5. **Assignments are snapshot-bound** via `decision_snapshot`.
6. **Every interrupt is explained** — message, author, scopes, `provenance_path`, evidence span, what was invalidated and what was preserved.
7. **Never label a replayed payload as live.** The repo's real-vs-simulated discipline is an asset; keep it.
8. **Surgical changes only.** The canonical CSV proof, the 12 Examples, and the whole test suite must keep passing untouched.

---

## Verified platform facts — do not invent APIs

**Claude Code hooks.** `PreToolUse` fires before every tool call, synchronously. Block via exit code 2, or:
```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "allow|deny|ask",
  "permissionDecisionReason": "string the model reads",
  "additionalContext": "≤10000 chars, also reaches the model"}}
```
`SessionStart` (matchers `startup`, `resume`, `clear`, `compact`, `fork`) and `SessionEnd` bracket the session. stdin carries `session_id`, `transcript_path`, `cwd`, `permission_mode`, `tool_name`, `tool_input`, `tool_use_id`.
⚠ **Hooks fail open** — on timeout, crash, or HTTP failure the tool call proceeds. Catch errors *inside* the script and emit `deny`; keep it tiny; set an explicit timeout (command default is 600s); cache the last verdict on disk. ⚠ `additionalContext` caps at 10,000 characters. ⚠ Hooks cannot invoke slash commands or trigger tool calls. ⚠ Nothing else can push an instruction into a running session from outside.
✅ `allowManagedHooksOnly: true` in organisation-managed settings cannot be overridden by project or user settings.

**Composio (Slack).** `SLACK_CHANNEL_MESSAGE_RECEIVED` — realtime push to one project-wide signed webhook; verify with `triggers.parse(body, headers, verify_secret)`. Also `SLACK_DIRECT_MESSAGE_RECEIVED`, `SLACK_MESSAGE_REACTION_ADDED`. Connections keyed by your own `user_id`. Toolkit `slack` (user auth) vs `slackbot` (bot auth). Python/TS SDKs; REST `https://backend.composio.dev/api/v3.1/`, header `x-api-key`.
⚠ **Teams has zero triggers.** ⚠ Gmail polls, ~15-min floor. ⚠ `entity_id` is legacy — use `user_id`; never `default`. ⚠ Call `triggers.get_type("SLACK_CHANNEL_MESSAGE_RECEIVED")` for the real payload shape; no example payload is published.

**Hexclave.** Teams plus nested boolean permissions. `GET /team-permissions?team_id=&user_id=&permission_id=&recursive=true`.
⚠ No roles/ABAC — nested permissions emulate them. ⚠ Permissions are **not** in the JWT; cache with a short TTL. ⚠ No Python SDK — REST + JWKS. ⚠ Use the cloud.

**Superset (demo).** CLI with `--json` and `SUPERSET_API_KEY`; remote MCP at `https://api.superset.sh/api/v2/agent/mcp`; `agents_create(hostId, workspaceId, agent, prompt)`.
⚠ `terminals_send`/`terminals_read` are in source but **not in the docs** — confirm live before depending on them. ⚠ Relay off by default on desktop, **on by default via `superset start`**. ⚠ No capability scopes. ⚠ No outbound events. ⚠ macOS only.

---

## Phase 1 — Live supervisor runtime + the hook (stop at the end)

1. Add `SupervisorExecutionMode.LIVE`. Widen `execution_mode` on `WorkspaceSupervisor` and `SupervisorAssignment` from `Literal[SIMULATED]`. `FixtureSupervisorRuntime` must keep returning `SIMULATED` so every Example and test is unaffected.
2. Implement `ClaudeCodeSupervisorRuntime` against the existing `SupervisorRuntimeAdapter` Protocol. It must not compute or return a verdict.
3. **Session binding.** `SessionStart` hook POSTs `{session_id, cwd, branch}` and binds the session to a `SupervisorAssignment`; `SessionEnd` releases it. Resolution order: explicit `writai attach` → task id in the branch name → `.writai/task` → unbound. Unbound sessions register, are allowed everything, and show as unbound in `writai status`.
4. **Hook verdict endpoint on the agent service** — `POST /supervisor/sessions/{session_id}/check`. **Not** on `services/executor_api.py`: that is the Callwright executor, it is titled "writ.ai Mock Executor", its `ExecuteRequest` requires a `token` and a full `AgentPlan`, and it holds no graph. The hook cannot supply a plan.
   - `assignment.decision_snapshot == current` → allow.
   - `INTERRUPTED` and not yet redirected → deny **once**, return `redirect_instruction` + `provenance_path`, transition to `REDIRECTED`, advance `decision_snapshot`.
   - invalidated outright rather than redirected → keep denying until a human acknowledges via the CLI.
5. **The hook script.** `PreToolUse` sends only `session_id`, `tool_name`, timestamp. On deny, emit a one-line `permissionDecisionReason` and put `redirect_instruction` + a compact provenance summary + one evidence link in `additionalContext`, **budgeted under 10,000 characters**. Catch every error and emit `deny`. Explicit short timeout. On-disk verdict cache.
6. Ship a **managed-settings example** with `allowManagedHooksOnly: true` — "the developer cannot switch it off" is a headline claim and must be demonstrable.

**Success criteria (deterministic, all testable):**

- Every pre-existing test, the canonical CSV proof, and all 12 Examples pass unchanged, and `FixtureSupervisorRuntime` still reports `SIMULATED`.
- A session bound to a current assignment is allowed.
- An assignment interrupted on a scope the session's task does not carry does **not** deny that session.
- An interrupted assignment denies the next `PreToolUse` **once**, with a payload under 10k characters; the following call is allowed.
- An outright-invalidated assignment keeps denying until acknowledged.
- The hook emits `deny` when the service is unreachable; the cached-verdict path is exercised by a test.
- The hook payload contains no tool input, file contents, or transcript data — assert it.

**Explicitly NOT a success criterion:** "the agent replans correctly." That is model behaviour, not determinism. Track it as a rehearsal risk.

**Stop. Report the diff and test results.**

---

## Phase 2 — Slack intake and approval (stop at the end)

1. **Composio trigger → signed webhook → verify → untrusted draft.** Call `triggers.get_type(...)` first; do not guess field paths.
2. **Reuse the existing Workspace import path.** Document import already produces an untrusted draft with evidence metadata and requires explicit user confirmation of extracted fields before server validation, recorded separately. Slack is a new *source*, not a new trust model. Do not build a second one.
3. **Satisfy `apply_decision_change`'s preconditions** as settled in §0 — supersession target, `affected_scopes ⊆ superseded.scopes`, and the three-way scope/requirement match. **Set `effective_at` on every Slack-derived decision**: `current_requirements()` orders by it and defaults `None` to `datetime.min` (`engine.py:344-350`), so an "effective immediately" message without a timestamp sorts to the bottom of precedence.
4. **Deterministic gate before any human sees it:** is the author authoritative for these scopes (Hexclave, cached)? Is confidence above threshold? Do the scopes exist? Failing any → park, never escalate noise.
5. **Blast radius in the confirmation surface** — message, author, scopes, requirement delta, and how many sessions and which people will be interrupted. Build it from the assignment list.
6. **Notification and approval channels.** CLI-first does not mean CLI-only: the approver is usually not in a terminal.
   - **Primary — Slack reaction approval.** Post a threaded message with the extracted fields and the blast radius; the approver reacts ✅ or ❌. Use the `SLACK_MESSAGE_REACTION_ADDED` trigger. Do **not** build Block Kit interactivity in v1 — button clicks POST to your own endpoint, which means your own Slack app with an interactivity request URL.
     Resolve the **reacting** user, check their Hexclave permission **at reaction time**, treat the first qualifying reaction as binding, and record it as evidence. A later un-react must not un-approve. Reactions from users without the permission are ignored silently, not escalated.
   - **Away from desk — ntfy or Pushover**, one HTTP POST with an action button calling back a signed approval endpoint.
   - **Fallback — email with a signed, single-use magic link.**
   - **Terminal — `writai pending` / `writai approve`.**
   - **Backstop — Callwright**, already integrated: escalate to a call when a pending change or an interrupt goes unacknowledged past a threshold.
   **The notification is not the approval.** Every channel must land on the same code path: resolve a Hexclave identity, check the permission for the affected scopes, then apply. No channel may approve by asserting it already did.
7. **Developer-side notification.** No push needed — the agent stops and prints the reason in their terminal. Add a free local notification from the hook script on deny (`osascript -e 'display notification …'` on macOS, `notify-send` on Linux), and stream the same events through `writai watch`.
   Note `services/events.py` `EventBroker` is process-local with a 100-event history. Adequate for the demo; record it as a known limit for multi-machine use rather than silently relying on it.
8. **Hexclave authority.** Map scope → sets of Hexclave **permission id strings** compared against `decision.authority_role`, a single string (`engine.py:136-140`); seed `authority_role="approve_compliance"`, not a role name. Add an injection point — `runtime.py:23-34` constructs `IntentAuthority` with no `authority_policy`, and `ScenarioDefinition.authority_policy` is unusable here because its model requires a `mutation: DecisionMutation`.

**Success criteria:** a stored **real** Composio payload converts to a draft in a test. The gate rejects a non-authoritative author. A reaction from a user *without* the permission does not approve. Approval interrupts exactly the assignments whose scopes intersect, and no others. The blast-radius count equals the set that actually gets denied. An "effective immediately" decision wins precedence over the decision it supersedes. Every approval channel resolves through one shared permission check — assert this with a test per channel.

**Stop. Report.**

---

## Phase 3 — CLI, escalation, demo (stop at the end)

1. Extend `cli.py`: `attach`, `status`, `why`, `pending`, `approve`, `watch`. `why` renders `provenance_path` and the evidence span, not a badge.
2. **Callwright escalation** — reuse `integrations/callwright.py` as-is: an interrupt unacknowledged for N minutes triggers a call to the lead. The idempotent attempt store already prevents duplicate dials. Keep it behind `callwright_live_calls_enabled`.
3. **Demo.** Five Claude Code sessions on one repo in five Superset worktrees. Two on `export.generation`, three on `export.authorization`. A real Slack message from someone holding `approve_compliance` — *"Approved: exports must be admin-only, effective immediately"* — never mentioning the ticket. The approver confirms the extracted fields, sees "3 of 5 sessions will be interrupted", approves. Two sessions carry on untouched. Three are denied once and receive the redirect. Nobody types anything. `writai why` shows the path.
4. Keep the real-vs-simulated panel honest, including that a crashed hook fails open.

**Definition of done:** five sessions register with correct bindings, visible in `writai status` · one real Slack message produces one pending change with a correct blast radius · approval interrupts exactly three and leaves exactly two · each interrupted session receives the redirect with no human typing · `writai why` renders the multi-hop path and evidence · the canonical CSV proof and all Examples still pass.

**Stop. Report, and flag anything you had to fixture that you expected to be real.**

---

## Phase 4 — Desktop app, then positioning (on request)

The desktop app's whole job is *"we don't change what someone had going on without telling them why."* One screen: what changed, who decided it, the evidence, what your agent was doing, what it is doing now, and what work was **preserved**.

Then: a KPI pack in buyer units — **ask the user for a defensible cost constant rather than inventing one**; candidate buyers with the facts that would decide between them, generated and then stopped rather than guessed; and a 30-day pilot on one team.

---

## Non-goals for v1

- Microsoft Teams, Discord, email intake
- Agent runtimes other than Claude Code
- Mobile app
- Editing `CLAUDE.md`/`AGENTS.md` as an interrupt
- A parallel run registry if the orchestrator already recomputes assignments
- Extending `domain.AgentRun`
- Depending on Superset's undocumented `terminals_send` at runtime
- Auto-applying changes without human confirmation
- Any claim that a crashed hook still enforces — it does not
