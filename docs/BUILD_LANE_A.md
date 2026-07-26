# Lane A — Enforcement (Ranjiv, Claude Code primary)

**You own the local, agent-facing half:** the live supervisor runtime, the Claude Code hook, session binding, the hook verdict endpoint, and the developer-side CLI.

**Your teammate owns Lane B** — Slack intake, extraction, Hexclave authority, approval channels. You meet at one frozen interface. Neither of you touches the other's files.

**Models are verified.** `claude --model fable` is valid. `codex exec -m gpt-5.6-sol` is Codex's real default. **`gpt-5.6` and `gpt-5.6-codex` do not exist** — `-m` accepts any string without validating it and silently degrades, so spell it exactly.

---

## T+0 → T+20 · Freeze the seam (you do this alone, before either lane starts)

Nothing else can safely run in parallel until this is committed to `main` and your teammate has pulled. Do it by hand — it is small and it must be exactly right.

**One commit containing all four:**

1. **`backend/dragback/supervisor_contract.py`** — the only interface between the lanes.

```python
"""FROZEN INTERFACE. Lane A implements it. Lane B calls it.
Do not change without both owners agreeing in the same conversation."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class InterruptRequest:
    workspace_id: str
    decision_id: str
    affected_scopes: frozenset[str]
    provenance_path: tuple[str, ...]
    interrupt_reason: str
    redirect_instruction: str


@dataclass(frozen=True)
class InterruptResult:
    interrupted_assignment_ids: tuple[str, ...]
    preserved_assignment_ids: tuple[str, ...]


class SupervisorInterruptPort(Protocol):
    def preview(self, request: InterruptRequest) -> InterruptResult:
        """Blast radius. MUST NOT mutate anything."""
        ...

    def interrupt(self, request: InterruptRequest) -> InterruptResult:
        """Transition intersecting assignments to INTERRUPTED. Idempotent per decision_id."""
        ...


class NullSupervisorInterruptPort:
    """Stub so Lane B is unblocked from minute one. Lane A replaces the binding, not this file."""
    def preview(self, request: InterruptRequest) -> InterruptResult:
        return InterruptResult((), ())

    def interrupt(self, request: InterruptRequest) -> InterruptResult:
        return InterruptResult((), ())
```

2. **`domain.py` / `workspaces/supervisor.py`** — add `SupervisorExecutionMode.LIVE` and widen `execution_mode` on `WorkspaceSupervisor` and `SupervisorAssignment` from `Literal[SIMULATED]`. `FixtureSupervisorRuntime` must keep returning `SIMULATED`. Run `make test` before committing — everything must still pass.

3. **`cli.py`** — register **both** new command groups now, pointing at two new stub modules `cli_dev.py` (yours) and `cli_approve.py` (theirs). Two lines each. After this commit **neither lane touches `cli.py` again**, which removes the biggest merge-conflict surface in the repo (it is 51K).

4. **`config.py`** — add *every* new settings key both lanes will need, in one go: `composio_api_key`, `composio_webhook_secret`, `hexclave_project_id`, `hexclave_secret_key`, `hexclave_team_id`, `dragback_hook_endpoint`, `dragback_hook_timeout_seconds`, `dragback_hook_cache_path`, `ntfy_topic`. Defaults empty. Neither lane touches `config.py` again.

Push. Tell your teammate to pull. **Now you can both run flat out.**

---

## Your work items

| # | Item | Files you own | Cut line |
|---|---|---|---|
| A1 | `ClaudeCodeSupervisorRuntime` implementing the existing `SupervisorRuntimeAdapter` Protocol; bind it as the real `SupervisorInterruptPort` | `workspaces/runtimes/claude_code.py`, `workspaces/supervisor.py` | **Must** |
| A2 | Session↔assignment binding + registry | `workspaces/session_binding.py` | **Must** |
| A3 | Hook verdict endpoint `POST /supervisor/sessions/{session_id}/check` | `services/agent_api.py` | **Must** |
| A4 | Hook scripts: `SessionStart`, `PreToolUse`, `SessionEnd` + managed-settings example | `hooks/` | **Must** |
| A5 | Dev CLI: `attach`, `status`, `why`, `watch` | `cli_dev.py` | Should |
| A6 | Local desktop notification on deny (`osascript` / `notify-send`) | `hooks/` | Nice |

### A1 — the runtime adapter

The Protocol already exists and its docstring already states the invariant: *"Agent-service runtime boundary; it never returns an authority verdict."* Keep that literally true — `ClaudeCodeSupervisorRuntime` records state, it does not decide.

`interrupt()` must be **idempotent per `decision_id`**. Lane B may retry, and a webhook may be delivered twice.

### A2 — session binding

Deterministic, never model-inferred. Resolution order: explicit `dragback attach ASSIGNMENT-TASK-102` → task id parsed from the branch name (`feat/TASK-102-csv-export`) → `.dragback/task` file → **unbound**. Unbound sessions register fine and are allowed everything, but show as unbound in `dragback status` so the gap is visible rather than silent.

### A3 — the verdict endpoint

On the **agent service**, not the executor. `services/executor_api.py` is the Callwright executor: it is titled "Dragback Mock Executor", its `ExecuteRequest` requires a `token` and a full `AgentPlan`, and it holds no graph. The hook cannot supply a plan and must not.

Logic:
- `assignment.decision_snapshot == current` → allow.
- `INTERRUPTED` and not yet redirected → **deny once**, return `redirect_instruction` + `provenance_path`, transition to `REDIRECTED`, advance `decision_snapshot`.
- invalidated outright rather than redirected → keep denying until a human acknowledges.

Deny-once-then-advance is what makes it terminate. Be honest in the code comment that this is a weaker guarantee than the grant path, because the session's plan is not re-checked — the PR check is what closes it.

### A4 — the hook

`PreToolUse` sends **only** `session_id`, `tool_name`, timestamp. Never tool input, file contents, or transcripts. Write a test that asserts this.

Hooks **fail open** — on timeout, crash, or HTTP failure the tool call proceeds. So: catch every error inside the script and emit `deny`; set an explicit short timeout (the command default is 600s); cache the last verdict on disk so a network blip degrades to the last known state rather than to no enforcement.

Deny payload: one-line `permissionDecisionReason`, and `redirect_instruction` + compact provenance + one evidence link in `additionalContext`, **budgeted under 10,000 characters**.

Ship a managed-settings example with `allowManagedHooksOnly: true` — "the developer cannot switch it off" is a headline claim and has to be demonstrable.

---

## How to run it

### Parallel within your own lane

A1+A2 touch supervisor internals; A3 touches the service; A4 is standalone shell. Run A3 and A4 concurrently in worktrees — they share no files.

```bash
git worktree add ../db-a3 -b lane-a-endpoint
git worktree add ../db-a4 -b lane-a-hook

( cd ../db-a3 && claude --model fable -p --permission-mode acceptEdits \
    "$(cat ~/dragback-prompts/A3.md)" ) &
( cd ../db-a4 && claude --model fable -p --permission-mode acceptEdits \
    "$(cat ~/dragback-prompts/A4.md)" ) &
wait
```

Superset does exactly this with a GUI if you'd rather watch them — and it's the sponsor you're demoing on anyway.

### Cross-model review — after every work item, no exceptions

Author with Claude, review with Codex. Different model, different failure modes; this is where the value is.

```bash
git diff main...HEAD > /tmp/a3.diff

codex exec -m gpt-5.6-sol -s read-only -C "$PWD" --json -o /tmp/a3-review.md < /dev/null \
"Read /tmp/a3.diff and AGENTS.md in this repo.

Find DEFECTS ONLY. No praise, no summary. For each: file, line, what breaks, minimal fix.
Check specifically:
 1. Does any code path let a model output become a verdict?
 2. Does the hook fail CLOSED on its own errors, and OPEN only where documented?
 3. Does the deny payload stay under 10000 characters?
 4. Does the hook transmit tool_input, file contents, or transcript data anywhere?
 5. Does anything depend on a task returning to ValidityStatus.VALID? Nothing ever clears
    invalidated_scopes, so that would be a permanent loop.
 6. Does FixtureSupervisorRuntime still report SIMULATED?
 7. Would the canonical CSV proof or any of the 12 Examples break?
Return a numbered defect list or the single word NONE."
```

Note `-s read-only` — the reviewer must not edit. And `< /dev/null` is mandatory or `codex exec` blocks reading stdin.

### Reviewing your teammate's work

Same command, pointed at their branch diff, and with the Lane B checks from their prompt. Do this at each integration point, not at the end.

---

## Order of operations

1. **T+0–20** — seam commit, pushed. Teammate pulls. *Blocking; nothing else starts.*
2. **T+20–90** — A1, A2 sequentially; A3, A4 in parallel worktrees. Codex review after each.
3. **T+90–120** — merge your worktrees to `lane-a`, `make test`, review the combined diff with Codex.
4. **T+120** — **first integration with Lane B.** Replace `NullSupervisorInterruptPort` with the real binding. This is the moment the two halves meet; do it early enough that a surprise is survivable.
5. **T+120–180** — A5 CLI, then A6 if there's room. Rehearse.
6. **Backstop** — record a screen capture of the working demo the moment it works once. `TASKS.md` P5 still has this open and it is the cheapest insurance you will buy today.

**Cut from the bottom.** A5 and A6 are droppable. A1–A4 are not — without them there is no product, only the existing engine.

## Things that will waste your time if you don't know them

- Editing `CLAUDE.md` mid-session does nothing; it's read at session start and excluded from hot-reload. It is persistence for the *next* session, never an interrupt.
- Do not extend `domain.AgentRun` — it's embedded as `ScenarioDefinition.initial_run`, its `plan` is required with no default, and `loop/workflow.py` reads `run.ticket_id` in four places. Use `SupervisorAssignment`.
- `services/events.py` `EventBroker` is process-local with a 100-event history. Fine today; know it before a buyer asks.

---

## Phase 4 — Approval screen in the existing web app (whichever lane clears first)

**Do not start this until your lane's Phase 3 is done and reported.** Whoever clears first takes it — say so in chat so you don't both start. It is one page on the frontend that already exists (`frontend/src/App.tsx`, `frontend/src/api.ts`, `frontend/src/components/`), not a new app.

**What it shows.** A pending-changes queue, and for the selected change: the Slack message, its author, the extracted scopes, the requirement delta, the evidence span — and the **blast radius**, *"3 of 5 active sessions will be interrupted — Priya, Marcus, Dan"*, with the sessions that will survive listed separately. Approve and reject buttons. After approval, which sessions were actually interrupted versus preserved.

**The one hard rule.** The browser decides nothing. The README already states the discipline — *the browser does not traverse the graph, decide verdicts, sign grants, calculate pass results, or invent loop state* — and it holds here. The blast radius comes from the server's `SupervisorInterruptPort.preview()`, never computed client-side; approve posts to the same shared `approve()` path every other channel uses, and the Hexclave permission is checked server-side. A browser that can approve by asserting it already checked is not a permission system.

**Reuse, don't rebuild.** The SSE `EventBroker` already streams state changes; wire the queue to it rather than polling. Match the existing Workspace visual language — the preserved-work list matters as much as the invalidated one, and that distinction is already a pattern in the Examples UI.

**Why it is worth doing at all**, given the CLI already works: it is the single most photogenic artifact in the product, it is what makes the system read as safe rather than invasive, and every other team on that stage will be showing a terminal.
