# Open items register — Lane A

Defects and limits found during the build and deliberately **not** fixed, with the
reason. Nothing here is a silent omission; each one is a known, stated trade-off.

Source of most entries: the cross-model Codex review (`gpt-5.6-sol`) run against
each work item's diff, per `docs/BUILD_LANE_A.md`.

---

## A1 — live runtime + SupervisorInterruptPort

| # | Item | Severity | Why it is open |
|---|---|---|---|
| A1-1 | **Two decisions interrupting the same assignment overwrite each other's `interrupt_reason`, `redirect_instruction`, and `provenance_path`.** The newest interrupt wins and the earlier explanation is lost. | Medium | Correct behaviour is a queue of pending interrupts per assignment, which is a design change to `SupervisorAssignment`, not a patch. The demo path applies one decision. `applied_interrupts` does retain each decision's own partition, so the history is not lost — only the assignment's rendered explanation is. |
| A1-2 | **`preview()` and `interrupt()` are separate reads with no revision check.** State can change between the approver seeing "3 of 5 will be interrupted" and approving, so the applied blast radius can differ from the previewed one. | Medium | Needs an optimistic-concurrency token on the workspace record and a re-confirm path in Lane B's approval UI. Cross-lane change; raise at the T+120 integration point. |
| A1-3 | **The blast radius counts assignments with no bound Claude Code session.** A `RUNNING` assignment nobody is actually working on is counted as "will be interrupted" although no `PreToolUse` hook will ever fire for it. | Medium (was High) | Now **measured** for the case that matters: `test_the_blast_radius_equals_the_set_that_actually_gets_denied` proves previewed == denied when all five sessions are bound. The gap that remains is an assignment that is `RUNNING` with no session registered — it is still counted. The blast radius is therefore an upper bound, never an under-count, which is the safe direction. Closing it fully means intersecting the preview with the session registry. |
| A1-4 | **The JSON repository read-modify-write is not atomic across processes.** Two agent services, or a concurrent orchestrator save, can lose an interrupt. | Low (known) | `docs/ARCHITECTURE.md` already states the store assumes one agent-service writer. The in-process `RLock` covers the single-writer deployment. Flag to a buyer; do not claim multi-writer safety. |
| A1-5 | **`services/events.py` `EventBroker` is process-local with a 100-event history.** | Low (known, pre-existing) | Documented in `CLAUDE.md` and the product architecture. Adequate for the demo, inadequate for several developers' machines. Needs a durable bus. |

### Fixed during A1 review (recorded for the audit trail)

- Target selection used raw scope overlap, so a **partially** affected Task would
  have been interrupted although the graph marks it `NEEDS_REVIEW` and the
  orchestrator preserves it. Now: the `InvalidationReport` decides once it exists,
  and the pre-approval estimate uses full scope containment, matching the
  traversal's own validity rule.
- Every interrupted assignment received the **same** provenance path, so one
  engineer's interrupt could be explained with another's lineage. Now each
  assignment's path is selected from `InvalidationReport.paths` by its own task.
- Idempotency was process-local, so a webhook redelivered after a restart could
  interrupt a session that had already complied and resumed. Now the partition is
  persisted as `WorkspaceSupervisor.applied_interrupts` and replayed verbatim.
- A supervisor could end up labelled `simulated` while holding assignments a real
  hook enforces. Now applying a live interrupt relabels the supervisor too.

---

## Environment

| # | Item | Severity | Note |
|---|---|---|---|
| ENV-1 | **`.env` service URLs carry trailing slashes** (`http://localhost:8001/`), which produced `//graph/reset` and 502'd every service-to-service call — and, because `vite.config` sets `envDir: ".."`, `//live-workspaces` in the browser client too. | Was blocking | Fixed on both sides: `config.py` normalizes `authority_url` / `agent_url` / `executor_url` with `.rstrip("/")`, matching `callwright_base_url` and `gemini_base_url`; the frontend gained one shared `serviceBaseUrl()` helper used by all three clients. The `.env` file itself was left alone — it is user-owned and was being edited during the build. |
| ENV-2 | **The repo had no virtualenv and system `python3` is 3.9**, below the `requires-python = ">=3.11"` floor. | Was blocking | Created `.venv` with python3.12 and installed `.[dev]`. `make` uses `PYTHON ?= python3`, so run `make test` as `make test PYTHON=.venv/bin/python`, or activate the venv first. The hook scripts are deliberately exempt: they are stdlib-only and verified to run under 3.9, because they execute in the developer's environment, not ours. |

---

## Hook installation policy

Hook wiring is **demo-local and per-developer**, and this is enforced rather than
documented:

- It goes in the demo working directory's `.claude/settings.local.json`, which
  `.gitignore` now excludes.
- **Never** the user-level `~/.claude/settings.json` — that would point every
  project on the machine at a service only running for the demo.
- **Not** committed to the tracked `.claude/settings.json` until after the demo.
  Promoting it there enforces for every teammate who pulls, which is a deliberate
  decision to take once the demo has been rehearsed.
- `backend/tests/test_hook_install_policy.py` fails if any of the above is
  violated, including if a shell command in `hooks/README.md` writes to either
  file.

The one legitimate machine-wide install is the organisation-managed
`managed-settings.json` carrying `allowManagedHooksOnly: true`. That is the
"the developer cannot switch it off" claim, and it is an organisation decision,
not a demo step.

---

## Integration pass — review findings and final dispositions

`gpt-5.6-sol`, read-only, against the full `pre-integration...HEAD` diff. Ten
defects were raised and none disproved. **Nine are closed. INT-2 is the only
deferred finding** — see its row for why reimplementing grant verification in
the check would be the wrong fix. See `INTEGRATION_REPORT.md` §5 for the full
disposition of each.

| # | Item | Severity | Disposition |
|---|---|---|---|
| INT-1 | ~~`writai_ci_check.py` silently discards malformed workspace or assignment objects.~~ | ~~Medium~~ | **CLOSED.** The rule is the one `.writai/attach` settled: absence of binding information is permissive, failure to obtain it is not. A response that parses cleanly and yields no candidates is UNBOUND and passes; a workspace, supervisor, assignments value or assignment that is not the shape it claims raises `MalformedServiceResponse` and fails with its own verdict code. The question was never which field broke — it is whether we got a clean answer at all, so schema drift that still parses keeps passing. |
| INT-2 | **The PR check's grant validation ignores `run_id`, `task_id` and `plan_hash`.** An ALLOW grant for another run, task or plan passes that weaker check when its snapshot and expiry match. | Medium | **DEFERRED — deliberately, do not "fix" this under time pressure.** CLAUDE.md invariant 5 still HOLDS where it is enforced: `services/executor_api.py` verifies the grant, including `run_id`, `task_id` and `plan_hash`, before anything executes. The PR check is a weaker second opinion on top of that, not the only gate. Closing it properly needs a server endpoint that performs canonical grant verification and returns a verdict — **not** a reimplementation of grant verification inside a stdlib-only, 3.9-compatible script, which would be a second copy of the rules that can silently drift from the real one. That is a considered change, not a demo-eve change. |
| INT-3 | ~~`mark_redirect_delivered()` persists `REDIRECTED` before the denial reaches the hook.~~ | ~~High~~ | **CLOSED.** Now deny-until-acknowledged: `/check` returns the redirect without advancing, the hook echoes the `redirect_id` on its next call once the verdict is actually on stdout, and the service advances only then. A lost delivery re-delivers the identical redirect instead of allowing. Proven against the real hook under python3.9. See `INTEGRATION_REPORT.md` §5a. |
| INT-4 | ~~`approvals/api.ts` — approval bindings survive fallback and concurrent stale loads.~~ | ~~Low~~ | **CLOSED proactively.** Local authentication still cannot make the path reachable because the Hexclave project currently has zero teams and therefore no valid `HEXCLAVE_TEAM_ID`, but that absence is not a safety boundary. Bindings are now held in a `WeakMap` keyed by the change OBJECT and frozen on creation, so a fixture card carrying the same composite id was simply never bound and cannot borrow live credentials, and two concurrent loads cannot cross-contaminate. |
| INT-5 | ~~`approvals/api.ts` — a lost or malformed response after a successful approval renders as a rehearsal.~~ | ~~Medium~~ | **CLOSED.** `ApprovalOutcome` is now `applied \| rehearsal \| indeterminate`. A 4xx refusal is a rehearsal (the server decided, nothing landed); a 5xx, a network failure, an unreadable body or a missing partition is indeterminate and is reconciled by re-reading the workspace. A failed read NEVER resolves to "not applied" — the screen says *sent, outcome not confirmed* and points at `writai dev status`. One wifi hiccup mid-approval no longer makes the UI contradict the room. |

---

## Left open deliberately on demo eve

| # | Item | Severity | Disposition |
|---|---|---|---|
| ENV-1 | **4 low-severity npm advisories**, all one transitive edge: `elliptic` under `@hexclave/shared`, inherited by `@hexclave/react` and `@hexclave/ui`. `npm audit fix` cannot resolve them without changing the Hexclave dependency itself. | Low | **DEFERRED by the user's explicit call** — "four low-severity transitive advisories are not worth a dependency change tonight". Not exploitable from this surface: `@hexclave/react` is imported by `/approvals` only, and browser sign-in is off by default, so the SDK is not even constructed on the demo path. Revisit after the recording. |
| ENV-2 | **`scripts/demo/ack.sh` renders a garbled blocked-session line** — `blocked by yesnonointerruptedgraph-v17graph-v18`, several fields concatenated with no separators — then reports `[FAIL]` for a session that `writai dev ack` considers not blocked. | Low | **LOGGED, NOT FIXED.** Nothing on the staged path needs it: a denied-until-acknowledged session is released by its own hook echoing the `redirect_id` on the next tool call, with no human step. `dev ack` is for the narrower case of an assignment invalidated outright with no corrected plan, and it answers `DECISION_ID_UNRESOLVED` correctly for anything else. STAGE_RUNBOOK.md now says do not run this script on stage. |
| ENV-3 | **The venv held an editable install of the pre-rename `dragback` package pointed at `/Users/ranjivj/DragBack`.** `dragback …` ran old code from the archive tree and looked entirely normal doing it, while `.venv/bin/writai` did not exist at all — so the runbook's own `writai` commands and `writai doctor` could not run. | Medium | **FIXED** in `writai-verify`: `pip uninstall dragback && pip install -e .`. Step 0 of STAGE_RUNBOOK.md now checks for it. **Any other checkout on this machine may still have it** — the check is `ls .venv/bin/dragback`; if that file exists, the venv is stale. |
| ENV-4 | **`docs/BUILD_LANE_A.md` still specifies "deny once"** (line ~99), which INT-3 superseded with deny-until-acknowledged. | Low | **NOT EDITED.** It is a build-lane design record of what was specified at the time, not operator-facing guidance, and rewriting history there would hide that the design moved. STAGE_RUNBOOK.md — the document an operator actually reads — has been corrected. |

---

## Plan execution — T0 Foundation

No phase was skipped under standing rule 8. Items recorded for later tracks or a
separate change:

| # | Item | Disposition |
|---|---|---|
| T0-1 | **`uv.lock` is still consumed by nothing.** T0.1 capped `pyproject.toml` (`svix<2` and ceilings on every dependency) so a fresh `pip install` resolves the version that passes. Moving `scripts/bootstrap.sh` and CI to `uv sync --locked` is the durable fix. | **OUT OF SCOPE**, recorded as instructed. |
| T0-2 | **Two documented variables are read by nothing in the tree:** `WRITAI_PUBLIC_WEBHOOK_URL` and `HEXCLAVE_PUBLISHABLE_CLIENT_KEY` in `.env.example`. `test_every_documented_env_var_has_a_settings_field` allow-lists them literally with that note rather than deleting operator documentation. | **LOGGED.** Track C owns the docs; decide whether to wire or drop them. |
| T0-3 | **`workspace_store` now defaults to `.writai/live-workspaces.sqlite3` before the SQLite store exists.** Until Track B's B1 lands, `agent_api.py` still constructs `JsonFileLiveWorkspaceRepository` against that path (a JSON document under a `.sqlite3` name), and the sibling stores derived in `authority_api.py:730` and `agent_api.py:680` inherit the suffix. Gate is green; nothing asserts on the literal default. | **HANDED TO TRACK B** (B1 suffix routing and migration). |
| T0-4 | **The `neo4j` CI job passes `--no-cov`.** With `fail_under = 83` in `pyproject.toml`, a marker-selected run of two tests measures 35.8% and would fail for no reason. The full-suite floor is enforced by the `check` job. | **BY DESIGN**, disclosed here because the plan text omitted the flag. |

---

## Plan execution — Track B

### B1 — SQLite behind `LiveWorkspaceRepository`

Measured on this tree (Linux/WSL2, Python 3.11.2, umask `0o022`, median of 20
`get()` calls on the **last** id in a store of N deep copies of one imported record):

| N workspaces | JSON `get()` | SQLite `get()` |
|---|---|---|
| 1 | 0.24 ms | 0.30 ms |
| 50 | 3.79 ms | 0.35 ms |
| 200 | 17.05 ms | 0.26 ms |

JSON `t200 / t1` = **72.5** (a second run gave 0.26 / 4.00 / 16.88 ms, ratio 64.0).
SQLite `t200 / t1` ≈ 0.9. The source plan's 0.84 / 19.6 / 97.8 ms were **not**
reproduced on this machine; the shape (linear in store size) was.

Observed file mode **immediately after `temporary.replace()` inside the JSON store's
`save()`**: `0o644`. The `chmod(0o600)` that follows closes the window, so the mode
observed after `save()` returns is `0o600`. The SQLite file is `0o600` from creation
(`os.open(..., 0o600)` before the first `sqlite3.connect`), sampled at every connect.

| # | Item | Disposition |
|---|---|---|
| B1-1 | **`mutate()` is not on the `LiveWorkspaceRepository` Protocol and the three authorization-bearing call sites are not migrated.** Premise (B1 steps 6–7): *"Add one method to the Protocol … Implement on both repositories … Migrate exactly three call sites."* Reality: the Protocol is implemented structurally by `FailFinalSaveRepository` at `backend/tests/test_workspace_approval_recovery.py:185-206`, passed as a `LiveWorkspaceRepository` at `:530` and `:535` and driving `approve_decision` at `:539` — and that file is in **no** track's ownership row. With `mutate` on the Protocol, `python -m mypy backend` reports 30 errors in three files: 28 in Track B's own `test_claude_code_runtime.py` and `test_claude_code_enforcement.py` (fixable), 2 at the lines above (not). Migrating `approve_decision` would also raise `AttributeError: 'FailFinalSaveRepository' object has no attribute 'mutate'` in `test_final_repository_save_failure_reuses_durable_authorization`. **Delivered instead:** `mutate()` on both concrete stores (`workspaces/repository.py`), atomic under one `BEGIN IMMEDIATE` / one `RLock`, proven by the two-process test in `test_service_concurrency.py`. **To unblock:** a four-line delegating `mutate` on `FailFinalSaveRepository`, then add `mutate` to the Protocol and migrate `session_enforcement.py:324` `mark_redirect_delivered`, `orchestrator.py:816` `approve_baseline` (its `save` at `:870`) and `orchestrator.py:1116` `approve_decision` (its first `save` at `:1169`). The goal still matters: those three are the sites where a lost update loses an *authorization decision*. | **ESCALATED — ownership gap.** |
| B1-2 | **Orchestrator read-modify-write sites left on `get()` → mutate → `save()`, by instruction** (`AGENTS.md:34` invariant 8; each loses display state, not an authorization decision). `orchestrator.py` — `preview_decision` get `:717`; `authorize` get `:875` / save `:904`; `propose_decision` `:913` / `:961`; `cancel_pending_decision` `:966` / `:989`; `record_approval_rejection` `:1009` / `:1097`; `approve_decision`'s later saves `:1191`, `:1198`, `:1215`, `:1240`, `:1280`; `verify_initial_grant` `:1285` / `:1328`; `update_plan` `:1337` / `:1362`; `reauthorize` `:1367` / `:1405`; `verify_replacement_grant` `:1410` / `:1450`. Plus the two B1-1 sites once unblocked. | **FOLLOW-UP**, own change with own tests. |
| B1-3 | **Sibling stores inherit the `.sqlite3` suffix but stay JSON documents.** `services/authority_api.py:731` `_workspace_store_sibling` and `services/agent_api.py:680` `_crustdata_replay_store` derive `live-workspaces-<label>.sqlite3` names for `JsonCrustDataDeliveryReplayStore` and the Slack approval-thread / delivery stores. Functional; misleadingly named. T0-3 predicted this. Not changed in B1 — it is not the enforcement hot path and it is not a `LiveWorkspaceRepository`. | **LOGGED.** |
| B1-4 | **A JSON document sitting under the `.sqlite3` name** (any tree that ran between T0.3 and B1) is refused at startup with `RuntimeError: Live Workspace store is not a SQLite database: … rename it to live-workspaces.json and restart to migrate it.` Nothing is renamed or deleted automatically; the migration only reads a sibling `.json` and leaves it in place. | **BY DESIGN**, disclosed. |
| B1-5 | **`live_services` in `test_live_workspaces.py` now runs every service-level workspace test against both stores** (fixture `params=("json", "sqlite3")`), so the existing flows are proven on SQLite rather than assumed. Three assertions that scanned the store as UTF-8 text now scan bytes, because the SQLite file is binary. Test count in that file: 74 → 88 (+2 xfailed). | **DISCLOSED.** |
