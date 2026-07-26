# Lane A internal contract — FROZEN

A2, A3, A4/A6 and A5 are built concurrently. This file is the only thing that
lets them meet. **Do not change a name, path, or field below without saying so
explicitly** — three other work items are compiling against it right now.

Same discipline as `supervisor_contract.py`: agree the seam, then run flat out.

---

## File ownership — do not edit another item's files

| Item | Owns | Must not touch |
|---|---|---|
| A2 | `backend/dragback/workspaces/session_binding.py`, `backend/tests/test_session_binding.py` | everything else |
| A3 | `backend/dragback/services/supervisor_check.py`, the `/supervisor/...` routes in `backend/dragback/services/agent_api.py`, `backend/tests/test_supervisor_check.py` | `session_binding.py` |
| A4+A6 | `hooks/**`, `backend/tests/test_hooks.py` | all backend modules |
| A5 | `backend/dragback/cli_dev.py`, `backend/tests/test_cli_dev.py` | everything else |

**Frozen and off-limits to every item:** `backend/dragback/cli.py`,
`backend/dragback/config.py`, `backend/dragback/supervisor_contract.py`,
`backend/dragback/workspaces/supervisor.py`,
`backend/dragback/workspaces/interrupt_port.py`.

---

## A2 — the session binding module

`backend/dragback/workspaces/session_binding.py`. Exact public API:

```python
class SessionBindingSource(StrEnum):
    EXPLICIT_ATTACH = "explicit-attach"   # dragback dev attach, highest precedence
    BRANCH_NAME     = "branch-name"       # feat/TASK-102-csv-export
    REPO_FILE       = "repo-file"         # .dragback/task
    UNBOUND         = "unbound"           # registers fine, allowed everything, visible


class AssignmentRef(BaseModel):
    """What the caller already knows about one candidate assignment."""
    workspace_id: str
    assignment_id: str          # "ASSIGNMENT-TASK-102"
    task_id: str                # "TASK-102"


class SessionRegisterRequest(BaseModel):
    """Facts the SessionStart hook reports. The CLIENT NEVER RESOLVES A BINDING."""
    cwd: str
    branch: str | None = None
    attached_assignment_id: str | None = None   # contents of .dragback/attach
    task_file_task_id: str | None = None        # contents of .dragback/task


class SessionBinding(BaseModel):
    session_id: str
    source: SessionBindingSource
    workspace_id: str | None = None
    assignment_id: str | None = None
    task_id: str | None = None
    cwd: str
    branch: str | None = None
    registered_at: datetime          # UTC-aware
    acknowledged_decision_ids: list[str] = Field(default_factory=list)

    @property
    def bound(self) -> bool: ...     # True iff assignment_id is not None


class SessionBindingRegistry:
    """Deterministic, never model-inferred. Process-local, thread-safe (RLock)."""

    def register(
        self,
        *,
        session_id: str,
        request: SessionRegisterRequest,
        assignments: Sequence[AssignmentRef],
    ) -> SessionBinding: ...

    def get(self, session_id: str) -> SessionBinding | None: ...

    def release(self, session_id: str) -> SessionBinding | None: ...

    def list(self) -> list[SessionBinding]: ...      # sorted by registered_at

    def acknowledge(self, session_id: str, decision_id: str) -> SessionBinding | None: ...
```

### Resolution order — exactly this, and stop at the first hit

1. `attached_assignment_id` matches an `AssignmentRef.assignment_id` → `EXPLICIT_ATTACH`
2. a `TASK-<digits>` token parsed out of `branch` matches an `AssignmentRef.task_id`
   → `BRANCH_NAME`. Parse with `re.search(r"TASK-\d+", branch)`, case-sensitive.
3. `task_file_task_id` matches an `AssignmentRef.task_id` → `REPO_FILE`
4. otherwise → `UNBOUND` with all three ids `None`

A value that is supplied but matches nothing **falls through to the next rule**;
it never raises and never guesses a near match. Re-registering an existing
`session_id` replaces the binding and preserves `acknowledged_decision_ids`.

---

## A3 — HTTP contract

All routes on the **agent service** (`services/agent_api.py`, port 8002).
Never on `executor_api.py`. Put the resolution logic in
`services/supervisor_check.py` so it is unit-testable without HTTP.

### `POST /supervisor/sessions/{session_id}/register`

Body: `SessionRegisterRequest`. Builds `AssignmentRef`s from every Live
Workspace supervisor assignment, calls `registry.register`, returns:

```json
{"binding": { …SessionBinding… }, "correlation_id": "…"}
```

### `POST /supervisor/sessions/{session_id}/check`

The `PreToolUse` verdict. Body — **and this is the whole privacy claim, so it is
a closed set**:

```json
{"tool_name": "Edit", "timestamp": "2026-07-24T21:00:00Z"}
```

Reject any body carrying `tool_input`, file contents, or transcript data. Response:

```json
{
  "decision": "allow",
  "reason": "Session is bound to a current assignment.",
  "bound": true,
  "assignment_id": "ASSIGNMENT-TASK-102",
  "task_id": "TASK-102",
  "redirect_instruction": null,
  "provenance_path": [],
  "evidence_ref": null,
  "decision_snapshot": "graph-v18",
  "correlation_id": "…"
}
```

`decision` is `"allow"` or `"deny"` only. Never `"ask"` — a blocked agent needs a
reason, not a prompt. On deny, `redirect_instruction`, `provenance_path` and
`evidence_ref` are populated.

### Verdict logic — deterministic, in this order

1. no binding, or `bound` is false → **allow** (`reason` says the session is unbound)
2. assignment not found any more → **allow** (fail open on our own bookkeeping gap)
3. `assignment.decision_snapshot == workspace.graph_version` → **allow**
4. state is `INTERRUPTED` and `interrupt_enforced` and the decision has not been
   acknowledged → **deny once**: return the redirect payload, transition the
   assignment to `REDIRECTED`, advance its `decision_snapshot` to the current
   graph version, persist. The next check hits rule 3 and allows.
5. assignment invalidated outright rather than redirected (its task is in
   `invalidation_report.invalidated_task_ids` **and** no corrected plan exists,
   i.e. `redirect_instruction is None`) → **deny every time** until
   `POST /supervisor/sessions/{session_id}/ack`.

Rule 4 is the product name. Write an honest comment that advancing the snapshot
on delivery marks the run current whether or not the agent actually complied —
weaker than the grant path, and the PR check is what closes it.

### `POST /supervisor/sessions/{session_id}/ack`

Body `{"decision_id": "DEC-018"}`. Records the acknowledgement so rule 5 stops
denying. Returns the updated binding.

### `DELETE /supervisor/sessions/{session_id}`

`SessionEnd`. Releases the binding. Idempotent; returns 200 even if unknown.

### `GET /supervisor/sessions`

Every binding, for `dragback dev status`. Unbound sessions are included and
visibly unbound — that is the point.

---

## A4 — the hook scripts

`hooks/` at the repo root. Python 3, stdlib only, no third-party imports —
the hook runs in the developer's environment, not ours.

| File | Purpose |
|---|---|
| `hooks/dragback_session_start.py` | reads branch + `.dragback/attach` + `.dragback/task`, POSTs `register` |
| `hooks/dragback_pre_tool_use.py` | POSTs `check`, emits the permission decision |
| `hooks/dragback_session_end.py` | DELETEs the session |
| `hooks/dragback_hook_lib.py` | shared: config, HTTP, on-disk verdict cache, notify |
| `hooks/settings.example.json` | project settings wiring all three hooks |
| `hooks/managed-settings.example.json` | org settings with `"allowManagedHooksOnly": true` |
| `hooks/README.md` | install, and an honest statement of what fails open |

Config comes from the environment, matching `.env.example`:
`DRAGBACK_HOOK_ENDPOINT` (default `http://localhost:8002/supervisor/sessions`),
`DRAGBACK_HOOK_TIMEOUT_SECONDS` (default `3`),
`DRAGBACK_HOOK_CACHE_PATH` (default `.dragback/hook-verdict-cache.json`).

### PreToolUse output

```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "allow|deny",
  "permissionDecisionReason": "one line the model reads",
  "additionalContext": "≤10000 chars"}}
```

Non-negotiables, each of which needs a test:

- **The request body contains only `tool_name` and `timestamp`.** Never
  `tool_input`, file contents, `transcript_path`, or `cwd`. Assert it.
- **Catch every exception and emit `deny`.** Hooks fail open, so an uncaught
  crash silently disables enforcement. The script's own failure must close.
- **Explicit short timeout** — the command-hook default is 600s.
- **On-disk verdict cache**: write every server verdict; on a transport failure
  fall back to the cached verdict for that session rather than to no
  enforcement. A cache miss on a transport failure is `deny`.
- **`additionalContext` budgeted under 10,000 characters** — build it, then
  truncate deterministically and say it was truncated.
- Exit code 0 with JSON on stdout. Do not use exit code 2.

### A6 — desktop notification

In `dragback_hook_lib.py`, called only on deny: `osascript -e 'display
notification …'` on darwin, `notify-send` on linux. Never let a notification
failure change the verdict or raise — wrap it and swallow.

---

## A5 — the dev CLI

`backend/dragback/cli_dev.py`. The parser is already registered and its command
surface is fixed: `attach`, `status`, `why`, `ack`, `watch`. Implement `run()`.

`client` is a `dragback.cli.DragbackClient`; use `client.request(Route(...))`.
Import `Route` lazily inside the function to avoid an import cycle.

- `attach ASSIGNMENT_ID --workspace WS` — writes `.dragback/attach` in the cwd.
  Local file only; no HTTP. Print the path written.
- `status [--workspace WS]` — `GET /supervisor/sessions`. Table: session, task,
  source, state. Unbound sessions render as `unbound` and are **not** hidden.
- `why [SESSION_ID]` — renders the provenance path as `A → B → C`, the affected
  scopes, the interrupt reason, and the evidence ref. A path, not a badge.
- `ack SESSION_ID` — `POST …/ack`.
- `watch WORKSPACE_ID` — streams `GET /live-workspaces/{id}/events` via
  `client.workspace_events()` and prints supervisor transitions as they arrive.

Honour `--json` (machine-readable, redacted) and never print a grant token.

---

## Rules every item follows

1. **No model output ever becomes a verdict.** Deterministic code decides.
2. The canonical CSV proof, all 12 Examples, and the existing suite pass
   **unchanged**. `FixtureSupervisorRuntime` still reports `SIMULATED`.
3. Add tests with every behaviour change. UTC-aware datetimes. Typed functions.
4. Run before you finish, from the repo root:
   ```
   PYTHONPATH=backend .venv/bin/python -m pytest
   .venv/bin/python -m ruff check backend
   .venv/bin/python -m mypy backend
   ```
   `.venv` already exists (python3.12). System `python3` is 3.9 and will not work.
5. Surgical changes only. Do not refactor adjacent code. Do not delete
   pre-existing dead code — mention it.
