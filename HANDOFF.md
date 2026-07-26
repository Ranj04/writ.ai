# Lane A handoff — overnight run, 2026-07-25

You have not seen this session. Everything you need is here.

**Merged to `main`.** Suite green: **579 passed, 3 skipped**; frontend 85 passed. `ruff` and `mypy` clean.

Read `ASSUMPTIONS.md` next — it lists every question I could not ask, with the
conservative answer I chose.

---

## The headline

**The survivor beat is proven with two real Claude Code sessions.** This was the
one thing still unproven when the night started.

```
TASK-201  continuing   snap=graph-v17  enforced=False   <- never touched
TASK-203  redirected   snap=graph-v18  enforced=True    <- denied once
```

Sara (export.generation) finished her CSV quoting work with zero Dragback
involvement. Priya (export.authorization) was blocked on her first tool call,
quoted the chain `DEC-018 → DEC-004 → SPEC-009 → TICKET-100 → TASK-203 →
PLAN-027 → this session`, **declined to route around the hook**, and proposed the
admin-gated correction rather than starting over. That last part is the product
claim: the agent adapted, it did not restart.

---

## What shipped

| Item | State | Where |
|---|---|---|
| Survivor proof, two live sessions | **Done, verified causally** | commit `e9db2fe` |
| Demo seeder, one command, 5× clean | **Done** | `scripts/demo/seed.py` |
| A5 dev CLI (`attach`/`status`/`why`/`ack`/`watch`) | **Working end to end** against a live server | `backend/dragback/cli_dev.py` |
| A6 desktop notification on deny | Done, 6 tests | `hooks/dragback_hook_lib.py` |
| Hook hardening: cache, timeout, deny-on-error, privacy test | Done, 53 tests | `hooks/` |
| `managed-settings.example.json` with `allowManagedHooksOnly: true` | Done | `hooks/` |
| Stage runbook | Done | `docs/STAGE_RUNBOOK.md` |
| Five-session proof as a test | **Restored** after review | `backend/tests/test_five_session_demo.py` |
| `NullSupervisorInterruptPort` still the active binding | Unchanged, as instructed | `services/agent_api.py` |

---

## What did not ship, and why

1. **`dragback dev status`, `dev why` AND `dev ack` do not work against the
   merged service.** (`dev ack` was missed in the overnight pass; the
   cross-check caught it.) Both call `GET /supervisor/sessions`. Lane B's router exposes only
   `POST /start`, `/{id}/check`, `/{id}/end`, `/{id}/acknowledge` — there is no
   session-list route. I did not add one: that is Lane B's file. Their logic and
   rendering are fully covered against a mock transport, so they work the moment
   the route exists. **Smallest fix: ~5 lines returning `registry.list()`.**
2. **Two hook implementations coexist.** Mine (`hooks/dragback_*.py`, proven
   tonight, 53 tests) and Lane B's `hooks/claude_code_hook.py` (untested by me).
   Pick one before the demo. I did not delete theirs.
3. **The seeder bypasses channel authentication.** See ASSUMPTIONS A-5. It does
   **not** bypass any authority check.

---

## Run it

```bash
# green build
PYTHONPATH=backend .venv/bin/python -m pytest

# seed + serve  (wait for "agent service listening")
DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1 \
  PYTHONPATH=backend .venv/bin/python scripts/demo/seed.py --serve

# re-seed between rehearsals — REQUIRED, see below
DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1 \
  PYTHONPATH=backend .venv/bin/python scripts/demo/seed.py

# two-terminal demo
cd /tmp/dragback-stage/sara  && claude "$(cat PROMPT.txt)"   # survives
cd /tmp/dragback-stage/priya && claude "$(cat PROMPT.txt)"   # denied once
```

Full detail, including the pre-flight check and troubleshooting, is in
`docs/STAGE_RUNBOOK.md`.

`.venv` is python3.12. System `python3` is 3.9 and will not run this project.

---

## Integration surface

### The frozen seam — unchanged

```python
# backend/dragback/supervisor_contract.py
port.preview(request: InterruptRequest) -> InterruptResult    # never mutates
port.interrupt(request: InterruptRequest) -> InterruptResult  # idempotent per decision_id
```

- **IDs are assignment ids, both in and out**: `("ASSIGNMENT-TASK-203", ...)`.
  Never task ids. Strip the `ASSIGNMENT-` prefix to get a task id.
- `affected_scopes` is **`frozenset[str]`**.
- `InterruptRequest` is a **frozen dataclass, not pydantic**. It does **not**
  survive a JSON round trip — `frozenset` is not JSON-serialisable. Construct it
  in-process. If it ever crosses HTTP, someone must convert to/from a list.
- `interrupt()` idempotency is **persisted** as
  `WorkspaceSupervisor.applied_interrupts`, so a webhook redelivered after a
  restart replays the original partition instead of re-interrupting a session
  that already resumed.
- `NullSupervisorInterruptPort` is still the **active** binding. The real one
  sits beside it as `agent_api.live_interrupt_port`. Swapping is one line.

### The hook ↔ service contract (adapted tonight)

My hook now speaks Lane B's routes:

| Event | Call | Body |
|---|---|---|
| SessionStart | `POST {base}/start` | `{session_id, cwd, branch}` |
| PreToolUse | `POST {base}/{session_id}/check` | `{session_id, tool_name, timestamp}` |
| SessionEnd | `POST {base}/{session_id}/end` | `{session_id}` |

`base` = `DRAGBACK_HOOK_ENDPOINT`, default
`http://localhost:8002/supervisor/sessions`. Auth header is
**`X-Dragback-Hook-API-Key`** — the service fails closed (503
`HOOK_AUTHENTICATION_NOT_CONFIGURED`) if `DRAGBACK_HOOK_API_KEY` is unset on
either side.

`timestamp` must be **timezone-aware**; the service rejects naive ones.

**Privacy:** the PreToolUse body is a closed set of exactly three keys.
`tool_input`, file contents, `transcript_path`, `permission_mode` and `cwd` never
leave the machine **on that call**. SessionStart is the one exception: it sends
`cwd` and `branch` once so the service can read `.dragback/task` itself,
and marker-file *contents* are no longer transmitted at all — the service reads
`.dragback/task` itself. Asserted against bytes on a real socket by
`test_no_secret_appears_in_the_bytes_actually_transmitted`.

---

## Known issues

1. **`dev status` / `dev why` / `dev ack` have no session-list endpoint.**
   `dev ack`'s own route was also wrong (`/ack` vs the service's `/acknowledge`)
   and is now fixed, but it still needs the session list to resolve which
   decision it is releasing.
2. **Two hook implementations.** Mine (`hooks/dragback_*.py`) and Lane B's
   `hooks/claude_code_hook.py`. Pick one.
3. **A service restart denies every open session, survivors included.** The
   session registry is in-memory (`agent_api.py`), so after a restart every
   session reads as unregistered until it re-registers — and SessionStart does
   not re-fire inside a running session. Relaunch all sessions after a restart.
   Lane B's file; not fixed here.
4. **A missing assignment denies until acknowledged, and acknowledging it
   raises.** `session_enforcement._deny_for_missing_assignment` has no release
   path when the assignment is gone. Lane B's file; not fixed here.
5. **The seeder reads `.dragback/task` server-side**, so the service must share a
   filesystem with the sessions. True on one demo machine; not if remote.
6. **`dragback` is not on PATH** in a stage session. Packaging decision.
7. **Unbound sessions are allowed everything and look identical to success.**
   The sharpest demo risk. Confirm `.dragback/task` exists and the port matches
   before judging a run.
8. **The JSON store is single-writer.** Two agent services over one store can
   lose an interrupt. Pre-existing.
9. **`services/events.py` EventBroker is process-local**, 100-event history.
   Pre-existing.

---

## Adversarial review

`codex exec -m gpt-5.6-sol -s read-only` against the full overnight diff returned
11 defects. Fixed 9, including four genuine fail-open holes:

- a cached **allow** was replayed on transport failure, so a supervisor outage
  authorized work — only a cached **deny** is ever reused now;
- malformed responses (`{"decision":"allow"}` with a junk reason) were accepted;
- the desktop notification ran **before** the verdict was written, so a slow HTTP
  call plus a hung `osascript` could exceed the hook's command timeout and get
  the process killed before it emitted anything — which Claude Code treats as
  allow;
- `emit_json` swallowed write failures and still exited 0, emitting no verdict at
  all. It now returns failure and the entry script exits 2 — the only remaining
  way to block when stdout is unusable.

The review also caught that I had **deleted** the five-session proof rather than
porting it. It is restored and passing.

Not fixed, recorded instead: the seeder's self-constructed `ApprovalEvidence`
still bypasses the Hexclave permission check (ASSUMPTIONS A-5).

---

## The three things I would do first

1. Add the session-list route so `dev status` / `dev why` work — five lines, and
   it unlocks a whole demo beat.
2. Decide which hook implementation ships and delete the other.
3. Rehearse twice **with a re-seed in between**, and watch that the second run
   still denies. That is the failure mode most likely to embarrass you on stage.
