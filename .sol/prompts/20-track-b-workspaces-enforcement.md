# TASK: Track B — Workspaces & enforcement. A durable store, an owned credential,
# a bounded registry.

You are **Fable**. Work in the worktree at `/work/writai-track-b`, branch
`track/b-workspaces-enforcement`. Activate its venv (`source .venv/bin/activate`) —
each worktree has its own. You own git for this track: one branch, one commit per
phase, per standing rule 13. Do not push, merge or rewrite history.

Read `.sol/prompts/_context.md` first — verified ground truth, the ownership table, the
standing rules, and the **Do not do** list.

**You own outright:** `backend/writai/workspaces/**`,
`backend/writai/services/authority_api.py`, `backend/writai/services/agent_api.py`,
`backend/writai/services/supervisor_api.py`, `backend/tests/test_live_workspaces.py`,
`backend/tests/test_service_concurrency.py`,
`backend/tests/test_claude_code_enforcement.py`,
`backend/tests/test_claude_code_runtime.py`, `backend/tests/test_supervisor_runtime.py`,
`backend/tests/test_workspace_authority_contexts.py` (new).
**You may append one block at the end of** `outputs/OPEN-ITEMS-REGISTER.md`.
You do **not** touch `backend/writai/config.py`, `.env.example`, `pyproject.toml`,
`.github/workflows/**`, `backend/writai/authority/**`, `backend/writai/graph/**`, or
`backend/writai/services/support.py`. T0 already added every `Settings` field you need;
read them off `settings`, never declare one.

**Frozen inside your territory:**
- `backend/writai/workspaces/live_interrupt.py` — test-only, but `AGENTS.md:34`
  invariant 8 forbids deleting pre-existing dead code. **Mention it, never delete it.**
- `backend/writai/workspaces/interrupt_port.py` — **not dead.**
  `scripts/demo/seed.py:47,479` uses `WorkspaceSupervisorInterruptPort`, and so does
  `backend/tests/test_claude_code_runtime.py`. Deleting it breaks the demo seeder.
- `backend/writai/hooks/**` and `hooks/**` — the hook scripts are stdlib-only and run in
  the developer's environment, not ours. Read them; do not change them in this track.

**Standing rule 8 applies to every step.** If a line does not contain what this prompt
quotes: stop that step, `git checkout -- <file>`, skip the rest of the phase, append the
skip to `outputs/OPEN-ITEMS-REGISTER.md`, and report the premise versus the reality with
a real `path:line`.

**Your gate is `bash scripts/check.sh` and it must exit 0 before every commit.**

---

## Phase B1 — SQLite behind the existing `LiveWorkspaceRepository` Protocol

**Why:** One change closes three defects that share one root cause — a JSON document
re-read and rewritten whole on every operation.

- **(a) The enforcement hot path.** `workspaces/repository.py:89-94` `get()` calls
  `_read()` (`:46-55`), which `json.loads` + `model_validate`s the **entire** store and
  then linear-scans it. That call is reached from
  `session_enforcement.py:298-300` (`RepositorySupervisorAssignmentGateway.get`) on
  **every** `PreToolUse` hook. `config.py:165` defaults `WRITAI_HOOK_TIMEOUT_SECONDS` to
  3 with the comment *"Hooks fail OPEN on timeout, so a long timeout silently disables
  enforcement."* Store growth therefore degrades into **silent enforcement loss**, not
  an error. The source plan measured 0.84 ms at 1 workspace and 658.6 ms at 1000 —
  **step 1 re-measures rather than trusting it.**
- **(b) A world-readable window over signed grants.** `repository.py:57-67` `_write()`
  creates the temp file at the process umask, `temporary.replace(self.path)` carries
  that mode onto the target, and `self.path.chmod(0o600)` runs **afterward**. The
  document serialises `AuthorizationResult.grant`, a `SignedGrant` with a plain-string
  token. Every `create()`/`save()` reopens the window.
- **(c) Lost updates.** Read-modify-write guarded only by the in-process `RLock` at
  `:44`. `outputs/OPEN-ITEMS-REGISTER.md` entry A1-4 already admits this.

**The repo already contains the right pattern.** `notify/escalation_source.py:104-207`
`SqliteInterruptEscalationGrantStore` uses `BEGIN IMMEDIATE` (`:123`), a
`record_json TEXT` column, `_secure_permissions()` (`:201`), `_initialize()` (`:172`)
and `sqlite3.connect(self._path, timeout=30.0)` (`:195`). **Copy its shape.** Do not
invent a second persistence idiom in one codebase.

**Files:** `backend/writai/workspaces/repository.py`,
`backend/writai/services/agent_api.py`,
`backend/writai/workspaces/session_enforcement.py`,
`backend/writai/workspaces/orchestrator.py`,
`backend/tests/test_live_workspaces.py`, `backend/tests/test_service_concurrency.py`,
`outputs/OPEN-ITEMS-REGISTER.md`

**Do:**

1. **Measure first, in a throwaway script, and keep the numbers.** Build a
   `_WorkspaceStoreDocument` containing N deep copies of one imported record (ids
   `ws-00000`…) for N ∈ {1, 50, 200}; write each to a `tmp_path` store; time 20
   `JsonFileLiveWorkspaceRepository.get()` calls on the **last** id in each. Record the
   three medians and the ratio t200/t1. **These are the numbers that go in your report
   and in the register, and they are the ones Track C's README cites.** Also record
   `oct(stat.S_IMODE(path.stat().st_mode))` observed immediately after a `save()`.
2. Add `SqliteLiveWorkspaceRepository` to `repository.py`. Schema:
   ```sql
   CREATE TABLE IF NOT EXISTS live_workspaces (
     workspace_id TEXT PRIMARY KEY,
     updated_at   TEXT NOT NULL,
     revision     INTEGER NOT NULL,
     record_json  TEXT NOT NULL
   );
   CREATE INDEX IF NOT EXISTS live_workspaces_updated_at
     ON live_workspaces (updated_at DESC);
   ```
3. Create the file with `os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)`
   **before** the first `sqlite3.connect`, so the durable file is never world-readable
   even momentarily. Keep a `_secure_permissions()` chmod as belt-and-braces exactly as
   `escalation_source.py:201-204` does.
4. In `_initialize`, set `PRAGMA journal_mode=WAL` and `PRAGMA synchronous=FULL`. WAL
   lets the enforcement hot path read while an approval writes; `FULL` is the right
   durability for a store holding authorization state. Add a one-line comment saying so.
5. Implement the Protocol at `repository.py:24-31`:
   - `get()` — one `SELECT record_json FROM live_workspaces WHERE workspace_id = ?`.
     **O(1) in store size.** Raise the existing `LiveWorkspaceNotFound` on no row.
   - `list()` — one `SELECT record_json … ORDER BY updated_at DESC`.
   - `create()` — `INSERT`, translating `sqlite3.IntegrityError` into the existing
     `LiveWorkspaceConflict` with the same message shape as `repository.py:71-74`.
   - `save()` — `BEGIN IMMEDIATE`, `SELECT revision`, then
     `UPDATE … SET record_json = ?, updated_at = ?, revision = revision + 1
      WHERE workspace_id = ? AND revision = ?`, raising `LiveWorkspaceNotFound` when
     zero rows change. **`BEGIN IMMEDIATE` serialises across processes, which the
     `RLock` never did.**
6. Add one method to the `LiveWorkspaceRepository` Protocol:
   `def mutate(self, workspace_id: str, apply: Callable[[LiveWorkspaceRecord], LiveWorkspaceRecord]) -> LiveWorkspaceRecord: ...`
   Implement on **both** repositories — read, apply, write, all inside one
   `BEGIN IMMEDIATE` (SQLite) or one `RLock` acquisition (JSON). This is the seam that
   makes read-modify-write **atomic** rather than merely serialised.
7. Migrate **exactly three** call sites to `mutate()` — the three where a lost update
   loses an *authorization decision*, and no others:
   - `session_enforcement.py` `mark_redirect_delivered` (the gateway method at `:324`,
     called from `check` at `:521`);
   - `orchestrator.py` `approve_baseline` (`:816`, whose `save` is at `:870`);
   - `orchestrator.py` `approve_decision` (`:1116`, whose first `save` is at `:1169`).
   **Leave every other `self._repository.get(...)` → mutate → `save(...)` site in
   `orchestrator.py` alone** — `AGENTS.md:34` invariant 8. There are roughly fifteen;
   enumerate them by line number in the register as follow-up work.
8. Select the backend from the store path in `agent_api.py:162`: a `.sqlite3` or `.db`
   suffix selects `SqliteLiveWorkspaceRepository`, a `.json` suffix keeps
   `JsonFileLiveWorkspaceRepository`. T0.3 already changed the `config.py` default to
   `.writai/live-workspaces.sqlite3` — **if it did not** (T0 may have deferred that
   step), make the routing anyway and note that the default is still `.json`.
9. Add a startup migration inside the SQLite repository's constructor: if the configured
   file does not exist and a sibling `live-workspaces.json` does, read it through
   `JsonFileLiveWorkspaceRepository` and `create()` each record, then emit one
   `logger.info` naming both paths. **Do not delete the JSON file.**
10. Tests.
    - `backend/tests/test_live_workspaces.py::test_workspace_get_cost_does_not_grow_with_unrelated_workspaces`,
      parameterised over `[JsonFileLiveWorkspaceRepository, SqliteLiveWorkspaceRepository]`
      using the harness from step 1. Assert the **ratio**, not wall-clock:
      `assert t200 / t1 < 20`, with a comment stating the value you measured in step 1.
      Mark the **JSON** parameter `pytest.mark.xfail(strict=True, reason="the JSON store
      reparses on every get(); this is a documented property, not a bug to hide")`.
    - `test_workspace_store_is_never_group_or_world_readable_during_a_write`, same
      parameterisation: wrap `Path.replace` (JSON) / observe after each write (SQLite)
      with a spy that records `oct(stat.S_IMODE(target.stat().st_mode))` **immediately**
      after the rename, call `save(record)`, and assert the recorded mode is `0o600`.
      JSON parameter `xfail(strict=True)`.
    - `test_sqlite_store_file_is_created_owner_only` — assert
      `oct(stat.S_IMODE(path.stat().st_mode)) == "0o600"` immediately after construction,
      before any write.
    - `test_json_store_is_migrated_into_sqlite_on_first_start` — write a two-record JSON
      store, construct the SQLite repository at a fresh sibling path, assert both
      `get()` calls return the right records **and** that the JSON file still exists.
    - `backend/tests/test_service_concurrency.py::test_concurrent_saves_from_two_processes_do_not_lose_an_update`
      — two `multiprocessing.Process` workers each calling `repository.mutate(...)` 50
      times to append to a list field on the record; assert the final record has exactly
      100 entries. Parameterise over both repositories; JSON parameter
      `xfail(strict=True)`.
11. Append your block to `outputs/OPEN-ITEMS-REGISTER.md`: the three measured timings,
    the observed pre-fix file mode, and the enumerated `orchestrator.py` line numbers
    left un-migrated.

**Acceptance:** `get()` is O(1) in store size; the durable file is `0o600` from creation
and never observed otherwise; two processes cannot lose a write; every existing test
that used the JSON store still passes against SQLite.

**Verify:**
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_live_workspaces.py \
  backend/tests/test_service_concurrency.py backend/tests/test_claude_code_enforcement.py -q -rx
```
→ all pass, with the three JSON-parameterised cases reported as `xfailed` and `-rx`
printing their reasons.
```bash
bash scripts/check.sh; echo "EXIT=$?"
```
→ `EXIT=0`.
```bash
PYTHONPATH=backend python3 -m writai.demo >/dev/null && echo "demo still zero-config"
```
→ `demo still zero-config`.

**Positive control — prove the permission test can fail:**
temporarily comment out the `os.open(..., 0o600)` in step 3, run
`test_sqlite_store_file_is_created_owner_only`, confirm it goes **red**, restore.
Paste both outputs.

**If it fails:** If `sqlite3.OperationalError: database is locked` appears, the
connection timeout is too low — match `escalation_source.py:195`'s `timeout=30.0`.
**Do not drop `BEGIN IMMEDIATE`.** If a `LiveWorkspaceRecord` will not round-trip
through `model_dump_json` / `model_validate_json`, **stop and report which field** — do
not add a custom serializer, that is a data-model change nobody reviewed. If the
concurrency test is flaky rather than failing, raise the iteration count; **do not add a
retry loop**, which would hide the very defect the test exists to catch.

---

## Phase B2 — Per-developer hook credentials

**Why:** `services/supervisor_api.py:36` reads **one** global `WRITAI_HOOK_API_KEY`, and
`:133-150` `acknowledge` takes only a `session_id` — so any holder of that shared key
can acknowledge **another developer's** interrupt. Acknowledgement is precisely the
redirect-delivery confirmation that lets a denied session resume
(`session_enforcement.py:521`). `ClaudeCodeSessionBinding` (`session_binding.py:87-100`)
carries no owner. `.env.example` already describes the variable as *"Per-developer token
minted by writ.ai; identifies the session owner to the service"* — **the documentation
describes a design the code does not implement.**

**Files:** `backend/writai/services/supervisor_api.py`,
`backend/writai/workspaces/session_binding.py`,
`backend/writai/workspaces/session_enforcement.py`,
`backend/tests/test_supervisor_runtime.py`,
`backend/tests/test_claude_code_enforcement.py`

**Do:**

1. In `supervisor_api.py`, rename `HookApiKeyVerifier` to `HookCredentialVerifier`.
   Keep `from_environment()`, but parse a new `WRITAI_HOOK_API_KEYS` of the form
   `developer_id:secret,developer_id:secret` into `dict[str, str]` mapping
   **secret → developer_id**. Keep `WRITAI_HOOK_API_KEY` as the single-developer
   fallback, mapping to developer id `"default"`, so no existing setup breaks.
   Ignore blank segments and strip whitespace around both halves; a segment with no
   `:` is a configuration error — raise at construction with a message naming the
   variable.
2. Change `require(supplied) -> None` to `resolve(supplied) -> str`, returning the
   developer id. **Keep the two existing failure modes byte-identical**: `503`
   `HOOK_AUTHENTICATION_NOT_CONFIGURED` when nothing is configured (the
   `if not expected:` branch at `:39-45`), `401` `HOOK_AUTHENTICATION_FAILED` otherwise.
   Compare with `hmac.compare_digest` against **each** configured secret in turn, never
   with a dict lookup — a dict lookup keyed on a secret is a timing oracle.
3. Add `owner_id: str = Field(min_length=1, max_length=255)` to
   `ClaudeCodeSessionBinding` (`session_binding.py:87-100`), thread it through
   `ClaudeCodeSessionRegistry.register` (`:138-163`), and set it from the resolved
   developer id in `ClaudeCodeSessionEnforcement.start`
   (`session_enforcement.py:389-399`).
4. Add `owner_id: str` as a keyword-only parameter to `enforcement.check` (`:445`),
   `.end` (`:401`) and `.acknowledge` (`:574`). Each looks up the binding and raises
   `ApiError(status_code=403, code="HOOK_SESSION_NOT_OWNED", message="This session belongs to another developer.")`
   when `binding.owner_id != owner_id`. Every route in the supervisor session router
   (`supervisor_api.py:68-150`) passes the id `resolve()` returned.
5. **The privacy boundary is unchanged, and say so in a comment on `owner_id`:**
   identity comes from the credential the hook already sends in the
   `X-writ.ai-Hook-API-Key` header, not from any new field on the request.
   `ClaudePreToolUseRequest`'s four-field contract (`session_enforcement.py:66-88`)
   **gains nothing**. Do not add a field to it.
6. **Do not edit `.env.example`** — it is T0's and already documents both variables. If
   it does not document `WRITAI_HOOK_API_KEYS`, note that in the register for T1.
7. Tests in `backend/tests/test_supervisor_runtime.py`:
   - `test_a_developer_cannot_acknowledge_another_developers_session` — register a
     session with alice's key; POST `/supervisor/sessions/{id}/acknowledge` with bob's
     key; assert `403` and `error.code == "HOOK_SESSION_NOT_OWNED"`.
   - `test_a_developer_cannot_check_another_developers_session` — same for `/check`,
     asserting the tool call is neither allowed nor denied but rejected `403`. This is
     the important one: a `deny` would look like enforcement working.
   - `test_the_owning_developer_can_acknowledge` — alice's key on alice's session → `200`.
   - `test_a_single_shared_key_still_works_for_one_developer` — only
     `WRITAI_HOOK_API_KEY` set → the full start / check / acknowledge / end cycle
     succeeds. No existing deployment breaks.
   - `test_an_unknown_key_is_rejected_without_revealing_which_keys_exist` — assert `401`
     **and** that the response body contains neither any configured developer id nor any
     configured secret. Assert on the serialised body, not on the exception.

**Acceptance:** A hook credential identifies exactly one developer; that developer's key
is the only one that can check, acknowledge or end their sessions; a single-key setup
behaves exactly as before.

**Verify:**
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_supervisor_runtime.py \
  backend/tests/test_claude_code_enforcement.py backend/tests/test_hooks.py -q
```
→ all passed.
```bash
bash scripts/check.sh; echo "EXIT=$?"
```
→ `EXIT=0`.

**Positive control — prove the ownership check is reachable:** run
`test_a_developer_cannot_acknowledge_another_developers_session` with step 4's `403`
raise commented out. It must go **red with a `200`**, not with an error. Paste both.

**If it fails:** If adding `owner_id` breaks a serialization assertion,
`RegisteredSession.to_payload` (`session_enforcement.py:210`) feeds the authenticated
`GET /supervisor/sessions` route — update that test's expected key set, since the route
already requires a credential. If `writai dev status` renders wrong, that is
`cli_dev.py` and it is **not yours** — record it for T1 rather than editing it.

---

## Phase B3 — Bound the authority context registry

**Why:** `workspaces/authority_contexts.py:170` —
`self._contexts: dict[str, _DynamicAuthorityContext] = {}` grows monotonically. Its only
`delete` (`:249-257`) has exactly one caller, `orchestrator.py:270`, reached only when
`_ensure_context` (`:265`) finds a context that has **drifted** from its record and must
be rebuilt. A context is never released when a workspace finishes. Each holds a full
`MemoryGraphStore` plus an `IntentAuthority`; one per imported workspace, for the life of
the authority process. The source audit calls this "monotonic growth by design"; the
design is the defect.

**Files:** `backend/writai/workspaces/authority_contexts.py`,
`backend/writai/services/authority_api.py`,
`backend/tests/test_workspace_authority_contexts.py` (new)

**[correction — read this before you start]** The source implementation plan puts these
tests in `backend/tests/test_scenario_authority_contexts.py`. **That file tests
`ScenarioAuthorityContextRegistry` from `backend/writai/scenarios/authority_contexts.py`
— a different class in a different module, and it belongs to Track A.** Your tests go in
a **new** file, `backend/tests/test_workspace_authority_contexts.py`.

**Do:**

1. Change `_contexts` (`:170`) to `OrderedDict[str, _DynamicAuthorityContext]` and add
   a `max_contexts: int` parameter to `DynamicAuthorityContextRegistry.__init__`
   (`:160-171`).
2. Add `self._contexts.move_to_end(context_id)` at each **use** site — inside the
   `_access` helper (around `:234`) and in the `authorize` path (`:404-421`) — making it
   a true LRU keyed on use, not on creation. Do it under the existing `RLock` at `:171`.
3. In `create()` (`:180-227`), after the insert at `:227`, evict from the front while
   `len(self._contexts) > max_contexts`. Emit one `logger.info` per eviction naming the
   evicted `context_id` and the configured limit. Add a module-level
   `logger = logging.getLogger(__name__)` if the file has none.
4. **Eviction must be safe, not silent.** `orchestrator._ensure_context`
   (`orchestrator.py:265-285`) already rebuilds a context whose `context_state` returns
   `None`, replaying baseline approval and every approved mutation from the durable
   record. Add a comment at the eviction site naming `orchestrator.py:265` as the reason
   this is safe, so nobody later "fixes" it by removing the rebuild.
5. Pass `max_contexts=settings.max_authority_contexts` where the registry is constructed
   at `authority_api.py:146`. **T0.3 already added that field**; read it, do not declare
   it. If it is absent, that is a rule-8 stop: T0 deferred it, and this phase cannot
   proceed.
6. **Do not edit `.env.example`** — T0's, already documented.
7. Tests in `backend/tests/test_workspace_authority_contexts.py`:
   - `test_the_registry_evicts_the_least_recently_used_context` — `max_contexts=3`;
     create `c1..c4`; assert `state("c1")` raises `DynamicAuthorityContextNotFound` and
     that `c2`, `c3`, `c4` all still resolve.
   - `test_using_a_context_protects_it_from_eviction` — create `c1, c2, c3`; call
     `state("c1")`; create `c4`; assert `c1` survives and `c2` was evicted. This is the
     test that distinguishes LRU from FIFO; without it step 2 is unverified.
   - `test_an_evicted_workspace_context_is_rebuilt_from_its_record` — end-to-end through
     the orchestrator: import a workspace, force eviction by creating `max_contexts` more,
     then call a route that needs the context and assert the response's `graph_version`
     **and** its approved-mutation set match the pre-eviction values exactly.
   - `test_the_default_limit_is_the_configured_setting` — construct the registry the way
     `authority_api.py:146` does and assert it took `settings.max_authority_contexts`.

**Acceptance:** Memory is bounded by a configured constant; an evicted workspace context
rebuilds to an identical state on next use; nothing observable changes below the limit.

**Verify:**
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_workspace_authority_contexts.py -q
```
→ `4 passed`.
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_live_workspaces.py \
  backend/tests/test_workspace_approval_recovery.py -q && bash scripts/check.sh; echo "EXIT=$?"
```
→ all passed, then `EXIT=0`.

**If it fails:** If the rebuild produces a different `graph_version`, `_ensure_context`
is not replaying every mutation — read `orchestrator.py:265-300`, report the gap, and
**do not disable eviction to hide it.** If eviction deadlocks, `move_to_end` is being
called while the `RLock` at `:171` is held in a different order by another caller — the
lock is reentrant, so look for a **second** lock (each `_DynamicAuthorityContext` has its
own `context.lock`, used at `:255-257`).

---

## When Track B is done

Report, in this order:

1. The gate: `bash scripts/check.sh` exit code and the pytest/vitest tallies.
2. **The three measured `get()` timings and the ratio** from B1 step 1, and the observed
   pre-fix file mode. Track C's README cites these; give exact numbers.
3. The enumerated `orchestrator.py` line numbers left un-migrated in B1 step 7.
4. Both positive-control before/after outputs (B1 permissions, B2 ownership).
5. Every file you touched, with one line on why.
6. Every step skipped under standing rule 8, with premise versus reality at a real
   `path:line`.
7. **What you removed.** If nothing, say "nothing was removed" in those words.
8. Every shim, stub, hardcoded value or synthetic id you introduced.
9. A one-line confirmation that `workspaces/live_interrupt.py` and
   `workspaces/interrupt_port.py` are **both still present and unmodified**, and that
   you did not touch `config.py`, `.env.example` or `services/support.py`.
10. Whether `workspaces/authority_contexts.py:417` still lacks its `report=` argument
    (it should — that is T1's line, not yours).
