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
| DEMO-ENV-1 | **4 low-severity npm advisories**, all one transitive edge: `elliptic` under `@hexclave/shared`, inherited by `@hexclave/react` and `@hexclave/ui`. `npm audit fix` cannot resolve them without changing the Hexclave dependency itself. | Low | **DEFERRED by the user's explicit call** — "four low-severity transitive advisories are not worth a dependency change tonight". Not exploitable from this surface: `@hexclave/react` is imported by `/approvals` only, and browser sign-in is off by default, so the SDK is not even constructed on the demo path. Revisit after the recording. |
| DEMO-ENV-2 | **`scripts/demo/ack.sh` renders a garbled blocked-session line** — `blocked by yesnonointerruptedgraph-v17graph-v18`, several fields concatenated with no separators — then reports `[FAIL]` for a session that `writai dev ack` considers not blocked. | Low | **LOGGED, NOT FIXED.** Nothing on the staged path needs it: a denied-until-acknowledged session is released by its own hook echoing the `redirect_id` on the next tool call, with no human step. `dev ack` is for the narrower case of an assignment invalidated outright with no corrected plan, and it answers `DECISION_ID_UNRESOLVED` correctly for anything else. STAGE_RUNBOOK.md now says do not run this script on stage. |
| DEMO-ENV-3 | **The venv held an editable install of the pre-rename `dragback` package pointed at `/Users/ranjivj/DragBack`.** `dragback …` ran old code from the archive tree and looked entirely normal doing it, while `.venv/bin/writai` did not exist at all — so the runbook's own `writai` commands and `writai doctor` could not run. | Medium | **FIXED** in `writai-verify`: `pip uninstall dragback && pip install -e .`. Step 0 of STAGE_RUNBOOK.md now checks for it. **Any other checkout on this machine may still have it** — the check is `ls .venv/bin/dragback`; if that file exists, the venv is stale. |
| DEMO-ENV-4 | **`docs/BUILD_LANE_A.md` still specifies "deny once"** (line ~99), which INT-3 superseded with deny-until-acknowledged. | Low | **NOT EDITED.** It is a build-lane design record of what was specified at the time, not operator-facing guidance, and rewriting history there would hide that the design moved. STAGE_RUNBOOK.md — the document an operator actually reads — has been corrected. |

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

## Plan execution - Track A

- **A2 skipped under standing rule 8.** Premise: add `CREATE CONSTRAINT artifact_id_unique IF NOT EXISTS FOR (a:Artifact) REQUIRE a.id IS UNIQUE` inside `Neo4jGraphStore.reset()`'s seed transaction before the seeding writes. Reality: `backend/writai/graph/neo4j_store.py:75-76` executes `MATCH (n) DETACH DELETE n` in that transaction first, and Neo4j 5.26.30 rejects the subsequent schema statement with `Neo.ClientError.Transaction.ForbiddenDueToTransactionType`; schema modifications and data writes cannot share that transaction. The shared graph-store parity goal still matters, but moving constraint creation outside the transaction would be an unrequested replacement design.
- **A5 skipped under standing rule 8.** Premise: the positive-control and guard test can POST the v18 change to `/decisions/change`. Reality: `backend/writai/services/authority_api.py:245` defines POST `/decisions/ingest`, while `/decisions/change` is absent. Request-scoping `last_report` still matters, but substituting a different API contract is forbidden by the phase rules.

### Corrected completion of A2 and A5

- **A2 resolved after premise correction.** The original skip remains recorded above: Neo4j rejects schema and data writes in the same transaction. The authorised correction creates `artifact_id_unique` in its own schema transaction before `reset()` opens the existing seed data transaction; duplicate artifacts, missing edge endpoints, and artifact ordering now match the memory-store contract.
- **A5 resolved after premise correction.** The original skip remains recorded above: `/decisions/change` does not exist. The corrected guard uses the repository's real `POST /decisions/ingest` request shape, and authorization provenance is now supplied explicitly by in-process callers instead of inherited from shared `last_report` state.
- **Parity divergence, resolved:** `MemoryGraphStore.outgoing_edges` returned insertion order, while `neo4j_store.py` orders by `target_id, kind`. It was latent because authority traversal re-sorts edges with `authority_edge_sort_key` in Python. Memory now sorts by `(target_id, kind)`; `test_outgoing_edges_is_ordered_by_target_then_kind` in `backend/tests/test_graph_store_contract.py` inserts its edges out of order and pins both backends (see the "Latent parity divergence" section below for the controls).

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
| B1-1 | **`mutate()` is not on the `LiveWorkspaceRepository` Protocol and the three authorization-bearing call sites are not migrated.** Premise (B1 steps 6–7): *"Add one method to the Protocol … Implement on both repositories … Migrate exactly three call sites."* Reality: the Protocol is implemented structurally by `FailFinalSaveRepository` at `backend/tests/test_workspace_approval_recovery.py:185-206`, passed as a `LiveWorkspaceRepository` at `:530` and `:535` and driving `approve_decision` at `:539` — and that file is in **no** track's ownership row. With `mutate` on the Protocol, `python -m mypy backend` reports 30 errors in three files: 28 in Track B's own `test_claude_code_runtime.py` and `test_claude_code_enforcement.py` (fixable), 2 at the lines above (not). Migrating `approve_decision` would also raise `AttributeError: 'FailFinalSaveRepository' object has no attribute 'mutate'` in `test_final_repository_save_failure_reuses_durable_authorization`. **Delivered instead:** `mutate()` on both concrete stores (`workspaces/repository.py`), atomic under one `BEGIN IMMEDIATE` / one `RLock`, proven by the two-process test in `test_service_concurrency.py`. **To unblock:** a four-line delegating `mutate` on `FailFinalSaveRepository`, then add `mutate` to the Protocol and migrate `session_enforcement.py:324` `mark_redirect_delivered`, `orchestrator.py:816` `approve_baseline` (its `save` at `:870`) and `orchestrator.py:1116` `approve_decision` (its first `save` at `:1169`). The goal still matters: those three are the sites where a lost update loses an *authorization decision*. **Round 2:** the orchestrator verified the gap — `test_workspace_approval_recovery.py` belongs to no track, a plan defect — and authorised one edit to that file for this change only. `mutate` is on the Protocol (`workspaces/repository.py:39`); `FailFinalSaveRepository.mutate` (`:209`) applies the same `CHANGE_APPLIED` / `decision_approval_intent is None` / one-shot `fail_final_once` predicate to the record the callable returns and only then delegates, so the injected `OSError` still fires from `save` at the final write and `test_final_repository_save_failure_reuses_durable_authorization` proves what it proved. Migrated: `session_enforcement.py:326` `mark_redirect_delivered` (mutation at `:354`), `orchestrator.py:833` `approve_baseline` (`:893`), `orchestrator.py:1139` `approve_decision` (write-ahead intent at `:1211`; a `get()` at `:1146` still serves the idempotent completed-approval return, which must not write). Each goes red on `get()`/`save()`: `test_an_approval_keeps_a_write_that_lands_between_its_read_and_its_write[baseline|change]` (`test_workspace_authority_contexts.py`) and `test_redirect_delivery_keeps_an_interrupt_that_lands_between_read_and_write` (`test_claude_code_enforcement.py`). Trade-off, disclosed: the authority transport call inside `approve_baseline` and `_ensure_context` inside `approve_decision`'s intent write now run inside the store's `BEGIN IMMEDIATE`; the orchestrator's own `RLock` already serialised them in-process, and readers never wait under WAL. **Round 4 — the `approve_baseline` half of that trade-off no longer exists.** `approve_baseline` (`orchestrator.py:833`) now takes the same split as the round-3 sites: `get()` → binding check → `_ensure_context` → `self._transport.approve_baseline` (`:883`, outside any transaction) → `mutate()` (`:913`) whose callable re-runs the binding check against the *fresh* record's `baseline_proposal_instance_id` and recomputed fingerprint, never against the earlier read. `test_baseline_approval_conflicts_when_the_proposal_is_rebound_in_flight` (`test_workspace_authority_contexts.py`) rebinds the proposal between the read and the write and asserts a conflict with the record left `IMPORTED`; with the inner check hand-pointed at the stale read it fails `DID NOT RAISE`. The invariant is now enforced, not reviewed: `test_no_store_transaction_wraps_a_network_call` (`test_service_concurrency.py`) walks the AST of `orchestrator.py`, resolves every callable passed to `self._repository.mutate(...)`, and fails naming the method, callable and line if any contains a call on `self._transport`, `self._executor` or `self._supervisor_runtime`. Before the split it failed on exactly `approve_baseline -> approve: self._transport.approve_baseline at orchestrator.py:864`; after, it passes. **Still disclosed:** the `_ensure_context` half. `approve_decision`'s `record_intent` (`:1212`, mutate at `:1267`) calls `self._ensure_context(record)` at `:1237` on a first attempt only (when no durable intent exists). That is a transport round trip one method-hop removed, so the AST walk, which matches direct attribute calls, does not see it. The `RLock` / WAL-reader mitigation above still applies to it. **Round 5 — the `_ensure_context` half no longer exists, and the invariant sees one hop further.** `test_no_store_transaction_wraps_a_network_call` now resolves `self._helper(...)` calls to methods on the same class and searches their bodies too, one hop only: the point is to catch the helper that hides a round trip, not to build a whole-program analysis, and a helper's own helpers belong to that helper's contract. It carries an explicit allow-list, `_ALLOWED_HELPERS_IN_TRANSACTION` (`test_service_concurrency.py:240`), which is **empty: no site is excused**. Widened, and before any orchestrator change, it failed on 14 lines at 7 call sites: `approve_decision -> record_intent: self._ensure_context` (six transport calls at `:283/287/288/292/301/317`), and six supervisor-runtime sites — `authorize -> record_authorization: _dispatch_supervisor`, `approve_decision -> record_mutation: _apply_supervisor_invalidation`, `verify_initial_grant -> record_verification: _enforce_supervisor_interrupts`, `update_plan -> update: _redirect_supervisor`, `reauthorize -> record_reauthorization: _resume_supervisor`, `verify_replacement_grant -> record_verification: _complete_supervisor`. The runtime counts although today's only adapter is in-process: the product's second adapter is live, and a call on it inside `BEGIN IMMEDIATE` would be a real round trip under the write lock. All seven took the split; none needed an exclusion. `approve_decision` (`:1269`) now checks the binding on the read (`require_bound_proposal`, `:1286`, re-run inside), rehydrates the context before the intent write on a **first attempt only** (`:1330`; a retry must not rebuild a context that may already hold the applied mutation — `_recover_intent_mutation` reconciles that), and the intent write (`:1374`) conflicts unless the fresh read carries the lineage the context was rehydrated for (`_context_lineage`, `:284`: definition, graph version, baseline approval and role, approved mutations). The six supervisor helpers (`:536–:722`) return a transitioned deep copy computed before `mutate()` and never touch the record (`_supervisor_to_transition`, `:496`); `_apply_supervisor_transitions` (`:512`) inside the callable conflicts if the fresh supervisor or graph version differs from the read they were computed from, and the caller retries from the changed state. After the split the widened walk passes. Tests: `test_a_runtime_transition_conflicts_when_the_supervisor_changes_in_flight[authorize|verify-initial|update-plan|reauthorize|verify-replacement]`, `test_the_change_approval_conflicts_when_the_supervisor_changes_between_its_writes` (the change lands between the intent write and the mutation write; the durable intent is left in place and the retry recovers the applied mutation and re-interrupts from the changed state), and `test_the_approval_intent_conflicts_when_the_lineage_changes_in_flight` (`test_workspace_authority_contexts.py`). Control: with both re-checks hand-disabled all seven fail; restored, all pass. **Disclosed, same kind as the transport sites:** the runtime call now precedes the write, so a conflict after a live adapter has transitioned a session leaves the runtime ahead of the record until the retry recomputes and re-transitions. `update_plan` previously absorbed a hook redirect delivered between its read and write; it now conflicts on it and the caller retries from the delivered state rather than overwriting it. **Outside this module, not changed here:** `session_enforcement.py:380` `_deliver_redirect` calls `self._runtime.transition` inside `mark_redirect_delivered`'s `mutate()` (`:354`); the walk covers `orchestrator.py` only. | **RESOLVED (round 2); `approve_baseline` trade-off removed and the invariant made a test (round 4); `_ensure_context` trade-off removed and the invariant follows one hop, allow-list empty (round 5).** |
| B1-2 | **Orchestrator read-modify-write sites left on `get()` → mutate → `save()`, by instruction** (`AGENTS.md:34` invariant 8; each loses display state, not an authorization decision). `orchestrator.py` — `preview_decision` get `:717`; `authorize` get `:875` / save `:904`; `propose_decision` `:913` / `:961`; `cancel_pending_decision` `:966` / `:989`; `record_approval_rejection` `:1009` / `:1097`; `approve_decision`'s later saves `:1191`, `:1198`, `:1215`, `:1240`, `:1280`; `verify_initial_grant` `:1285` / `:1328`; `update_plan` `:1337` / `:1362`; `reauthorize` `:1367` / `:1405`; `verify_replacement_grant` `:1410` / `:1450`. Plus the two B1-1 sites once unblocked. **Round 2, re-read after B1-1 closed:** `preview_decision` get `:734`; `authorize` `:898` / `:927`; `propose_decision` `:936` / `:984`; `cancel_pending_decision` `:989` / `:1012`; `record_approval_rejection` `:1032` / `:1120`; `approve_decision`'s later saves `:1222`, `:1229`, `:1246`, `:1271`, `:1311` (the last is the one `FailFinalSaveRepository` targets); `verify_initial_grant` `:1316` / `:1359`; `update_plan` `:1368` / `:1393`; `reauthorize` `:1398` / `:1436`; `verify_replacement_grant` `:1441` / `:1481`. None migrated, by instruction. **Round 3 — the "display state" framing was wrong and is withdrawn.** Re-classified per site, by what a lost update would lose: `authorize` — the initial authorization result *with its signed grant*, the `AUTHORIZED` transition, the supervisor dispatch, and the `authorization.evaluated` audit event; `propose_decision` — the pending proposal, the `proposal_sequence` increment that makes instance ids unique, and `decision.proposed`; `cancel_pending_decision` — the cancellation and `decision.proposal-canceled`; `record_approval_rejection` — nothing *but* an audit event (`decision.approval-rejected`), so a plain save traded the whole write for whatever landed in between; `approve_decision` `:1222`/`:1229` — the intent clear after a rejected mutation; `:1246` — the authority's mutation evidence, invalidation report and every supervisor interrupt; `:1271` — the post-change authorization result with its grant; `:1311` — the approved mutation, `CHANGE_APPLIED` and `decision.approved`; `verify_initial_grant` — the executor verification and enforced interrupts; `update_plan` — the corrected plan and every redirect; `reauthorize` — the replacement grant and every resume; `verify_replacement_grant` — the final verification and completion. Every one is authorization or audit state; none is presentation. **All thirteen migrated to `mutate()`.** Pure sites (`propose_decision`, `cancel_pending_decision`, `record_approval_rejection`, `update_plan`) run their whole body inside the callable. Transport sites (`authorize`, `verify_initial_grant`, `reauthorize`, `verify_replacement_grant`, and `approve_decision`'s three post-recovery writes) were **split rather than wrapped**: `get()` → precondition check → `_ensure_context` and the authority/executor call → `mutate()` whose callable re-runs the same precondition on the fresh read and applies the result. No network call runs inside a `BEGIN IMMEDIATE` transaction at any of these sites (the two B1-1 sites, `approve_baseline` and `approve_decision`'s intent write, kept their disclosed trade-off untouched in this round; `approve_baseline` was split in round 4 and the invariant is now enforced by `test_no_store_transaction_wraps_a_network_call`, see B1-1). Re-check specifics: `reauthorize` also conflicts if `current_plan` changed, since `PLAN_UPDATED` admits a further `update_plan`; the `approve_decision` writes require the durable intent on the fresh read to be the one this approval wrote (fingerprint and instance id) and `clear_intent` clears only that intent; `verify_initial_grant` and `reauthorize` keep another writer's result if it landed first, matching their idempotent early return. `preview_decision` and `is_slack_authority_user_bound` are reads and stay on `get()`. Test: `test_every_lifecycle_write_keeps_a_write_that_lands_between_its_read_and_its_write[authorize|propose|cancel|reject|verify-initial|update-plan|reauthorize|verify-replacement]` in `test_workspace_authority_contexts.py` lands a bare `concurrent.write` event between each site's read and write and asserts it survives, the site's own audit event follows it, and sequences stay contiguous. Control: with `authorize` hand-reverted to `get()`/`save()` the `[authorize]` parameter fails `assert 0 == 1` on the concurrent event count while the other seven pass; restored, all eight pass. **Round 5:** "no network call runs inside a `BEGIN IMMEDIATE` transaction at any of these sites" was true of direct calls only. The supervisor-runtime calls inside `authorize`, `approve_decision`'s mutation write, `verify_initial_grant`, `update_plan`, `reauthorize` and `verify_replacement_grant` were still inside `mutate()` one method-hop down, and the widened invariant test found all six. Each is now split the same way as the transport calls (details and tests under B1-1). `update_plan` (`:1601`) was a pure site and now has the `get()` → precondition → runtime call → `mutate()` shape, with the precondition re-run inside. The eight-parameter interleaving test passes unchanged. | **RESOLVED (round 3); the runtime calls one hop down split out (round 5).** |
| B1-3 | **Sibling stores inherit the `.sqlite3` suffix but stay JSON documents.** `services/authority_api.py:731` `_workspace_store_sibling` and `services/agent_api.py:680` `_crustdata_replay_store` derive `live-workspaces-<label>.sqlite3` names for `JsonCrustDataDeliveryReplayStore` and the Slack approval-thread / delivery stores. Functional; misleadingly named. T0-3 predicted this. Not changed in B1 — it is not the enforcement hot path and it is not a `LiveWorkspaceRepository`. | **LOGGED.** |
| B1-4 | **A JSON document sitting under the `.sqlite3` name** (any tree that ran between T0.3 and B1) is refused at startup with `RuntimeError: Live Workspace store is not a SQLite database: … rename it to live-workspaces.json and restart to migrate it.` Nothing is renamed or deleted automatically; the migration only reads a sibling `.json` and leaves it in place. | **BY DESIGN**, disclosed. |
| B1-5 | **`live_services` in `test_live_workspaces.py` now runs every service-level workspace test against both stores** (fixture `params=("json", "sqlite3")`), so the existing flows are proven on SQLite rather than assumed. Three assertions that scanned the store as UTF-8 text now scan bytes, because the SQLite file is binary. Test count in that file: 74 → 88 (+2 xfailed). | **DISCLOSED.** |
| B1-6 | **Sol's round-2 findings on the SQLite store** (`backend/tests/review/test_track_b_adversarial.py`, kept byte-identical apart from the ruff import-block fix, as with the T0 review test). **F2 (BLOCKER):** a zero-length `live-workspaces.sqlite3` beside a JSON store was opened by SQLite as an empty database, so the migration was skipped and every approved workspace vanished behind a clean store. `_require_sqlite_database()` (`workspaces/repository.py`) now reads the 16-byte SQLite header before the store is opened and refuses any existing file that lacks it — zero-length, JSON, zeroed, or otherwise — naming the path and its size and leaving the file as found; the `sqlite3.DatabaseError` guard still catches a headed-but-corrupt file, with the same message. Positive control run: with the check disabled Sol's test fails `DID NOT RAISE RuntimeError`. **F1 (MAJOR):** `_secure_permissions()` now chmods the `-wal` and `-shm` siblings with the main file, tolerating absence, because the WAL carries committed rows — signed grants included — until checkpoint. A store this constructor half-created and then discarded on a failed migration is unaffected (`_discard_database_files` still removes it). | **RESOLVED (round 2).** |

### B2 — Per-developer hook credentials

| # | Item | Disposition |
|---|---|---|
| B2-1 | **`.env.example` documents `WRITAI_HOOK_API_KEY` (line 125) but not `WRITAI_HOOK_API_KEYS`.** The service now parses `WRITAI_HOOK_API_KEYS=developer_id:secret,developer_id:secret` (`services/supervisor_api.py` `parse_hook_credentials`) and keeps `WRITAI_HOOK_API_KEY` as the single-developer fallback mapped to developer `default`. `.env.example` is T0's and was not edited. | **Resolved** post-integration under orchestrator authorisation: `WRITAI_HOOK_API_KEYS` documented in `.env.example` beside `WRITAI_HOOK_API_KEY`, and allow-listed in `_READ_OUTSIDE_SETTINGS` in `backend/tests/test_runtime_config.py`. No `Settings` field added. |
| B2-2 | **`HookApiKeyVerifier` survives as an alias of `HookCredentialVerifier`.** `backend/tests/test_five_session_demo.py:438-451` constructs `HookApiKeyVerifier(expected_api_key="test-key")` and is in no track's ownership row, so the rename keeps the old name and the `expected_api_key` constructor field (`supervisor_api.py`, the comment above the alias says why). Drop the alias when that caller moves. | **SHIM, disclosed.** |
| B2-3 | **`owner_id` defaults to `default` on the in-process enforcement API** (`ClaudeCodeSessionRegistry.register`, `ClaudeCodeSessionEnforcement.start/check/end/acknowledge`). The router always passes the id `resolve()` returned, so every HTTP call is owner-checked; the default exists because callers outside Track B's ownership drive the enforcement object directly — `test_five_session_demo.py:255,276,460` and `test_interrupt_escalation.py:565,752,977`. The default never bypasses the check: a session registered by `alice` is still refused to a caller that omits the owner. | **DESIGN CHOICE, disclosed.** |
| B2-4 | **`GET /supervisor/sessions` gains `owner_id` on every entry** via `RegisteredSession.to_payload`. `cli_dev.py:621-623` and `scripts/ci/writai_ci_check.py` accept extra keys, so nothing renders wrong; the list is still not filtered by owner (it is an authenticated read model for `dev status`, and the plan did not ask for filtering). | **LOGGED.** |

### B3 — Bounded authority context registry

| # | Item | Disposition |
|---|---|---|
| B3-1 | **`DynamicAuthorityContextRegistry.__init__` gives `max_contexts` a default of `settings.max_authority_contexts`** (`workspaces/authority_contexts.py`), so `authority_api.py` passes it explicitly and callers that omit it — `backend/tests/test_workspace_approval_recovery.py:55-61`, outside Track B's ownership — get the same configured bound rather than "unbounded". This imports `writai.config` into the registry module; `workspaces/transport.py:8` already did. | **DESIGN CHOICE, disclosed.** |
| B3-2 | **The LRU touch lives in `_access` only.** `state`, `approve_baseline`, `approve_mutation`, `authorize` and `verify_grant` all pass through it, so `authorize` needs no second `move_to_end`; `test_authorize_and_verify_count_as_use` proves the enforcement-facing paths count as use. | **BY DESIGN.** |
| B3-3 | **The `evaluate_plan` call the plan cites as `authority_contexts.py:417` is now at `:459`** because of the additions above. It still has no `report=` argument — that line is T1's on the merged tree. | **FOR T1**, line number updated. |

## Plan execution — Track C

| Phase | Premise versus reality | Disposition |
|---|---|---|
| C5, steps 4–6 | The original prompt said `config.py:66` defaults `grant_secret` to `"writai-local-demo-secret"`. T0's changes shifted the definition to `backend/writai/config.py:103` through `DEFAULT_DEMO_GRANT_SECRET`; the original `path:line` was stale. | **RESOLVED.** The work was originally skipped under standing rule 8 because publishing a stale citation in Known limits would make the audit less trustworthy. The orchestrator re-derived the citations against the Track C worktree, and C5 steps 4–6 are now complete with the corrected `config.py:66` → `config.py:103` premise. |
| C5, post-merge citation verification | Known-limits citations into `workspaces/repository.py`, `workspaces/session_enforcement.py`, and `graph/neo4j_store.py` are correct in the Track C worktree but those files are being rewritten by Tracks A and B. | **OPEN for T1.** Re-verify every affected `path:line` after Tracks A and B merge. |

---

## Documentation consistency

| id | Item | Severity | Disposition |
|---|---|---|---|
| DOC-1 | **Two documents disagree about whether a Hexclave team exists.** `TASKS.md:88-90` ticks "Provision a Hexclave team, then configure `HEXCLAVE_TEAM_ID`" and states the current secret key and team resolve through `writai doctor hexclave`. `INTEGRATION_REPORT.md:500` and `:765` say the project returns zero teams and there is therefore no valid `HEXCLAVE_TEAM_ID`, so authenticated approvals fail closed and the demo uses the gated in-process seam. | Low (documentation) | **RECORDED, not resolved.** The `TASKS.md` claim is the later of the two and `INTEGRATION_REPORT.md` is a historical build record rather than current operator guidance, so the checkbox is probably right — but neither can be confirmed without live Hexclave credentials, which are not present in this environment. Asserting either as current fact would be the kind of unverified claim this register exists to catch. Run `writai doctor hexclave` on a configured machine and settle it in one place; `docs/SPONSORS.md:117,150` also documents the provisioning step and should agree. |
| DOC-2 | **`ENV-1`/`ENV-2` were reused for two unrelated tables.** The T0 environment block and the demo-eve block both used those ids for different items, so anything keying on an id collided. | Low | **RESOLVED.** The demo-eve block is now `DEMO-ENV-1`…`DEMO-ENV-4`, and the roll-up references match. |
| DOC-3 | **B2-1 read as both skipped and resolved.** The rule-8 ledger recorded it "Skipped under rule 8" while the B2 table and the roll-up recorded it resolved. | Low | **RESOLVED.** Both were true of different moments: the ledger is a chronological record of why a stage stopped, not a status table. The ledger row now says so and points at the B2 table and the commit that closed it. |

## Round 2 re-check — reviewer verdicts

The build protocol's round 2 is *builder responds → reviewer re-checks → verdicts*. The
first three rounds stopped after the builder's response and the orchestrator's
verification; the reviewers never re-examined their own findings. That pass has now run
against merged `main`. **All 16 findings came back UPHELD-FIXED** — Fable's 8 on Track A
and 4 on Track C, Sol's 2 on Track B, 1 on T0 and 1 on PR 2 — each re-verified by
running the original test and reading the fix at `file:line`.

Two results from that pass are recorded here rather than lost in a transcript.

| id | Item | Severity | Disposition |
|---|---|---|---|
| RC-1 | **The two graph stores disagree about nested transactions.** Track A's F2 fix made `Neo4jGraphStore.transaction()` join an already-open transaction, but `MemoryGraphStore` was not made join-aware. Where an outer block swallows an inner failure and continues — outer adds A, inner adds B and raises, outer catches and adds C — memory yields `['A', 'C']` and Neo4j yields `['A', 'B', 'C']`. | Low (latent) | **RESOLVED.** Not a regression: before the F2 fix Neo4j returned `['C']`, so the fix narrowed the gap rather than opening it. Latent because `apply_decision_change` is the only caller and never nests. No contract test pinned nesting parity, which is why it survived the review. `MemoryGraphStore.transaction()` (`backend/writai/graph/memory.py`) is now join-aware: a nested block joins the outermost, and only the outermost snapshots and restores. Pinned for both backends by `test_a_nested_transaction_joins_the_outer_one` in `backend/tests/test_graph_store_contract.py`, using exactly the probe in this row. Control: with the memory change reverted by hand the test fails `assert ['A', 'C'] == ['A', 'B', 'C']` on `[memory]`; restored, it passes on `[memory]` and `[neo4j]`. |
| RC-2 | **A review test went vacuous when its finding was fixed.** Track C's F2 test searched the documented walkthroughs for `scripts/demo/up.sh`; the fix removed every mention, so the test now passes without asserting anything. | Low | **RESOLVED.** The reviewer noticed during the re-check and verified the finding a different way — replaying the documented flow against three isolated services on an empty store: import, approve and authorize returned `ALLOW` on `graph-v17`, and after a change `verify --grant initial` exited 1 with `STALE_SNAPSHOT` on `graph-v18`. The general lesson is worth more than the instance: a test written to catch a specific wrong string stops testing anything once that string is gone. `test_finding_f2_the_no_hexclave_baseline_path_arms_the_documented_workspace` (`backend/tests/review/test_c1_review_docs.py`) keeps its name and history and now asserts the right thing: each documented no-Hexclave baseline block must import a fixture that exists, approve the baseline of the workspace id that fixture declares with a script that exists, and authorize that same workspace; a document with no such block fails outright. `test_the_f2_test_goes_red_when_the_baseline_path_names_an_unimported_workspace` is the checked-in control: the README copy approves `csv-exports` after importing `refund-operations` and the F2 test fails naming both ids. |

## Plan execution — integration

T1 ran on the merged tree (`df28f11`; Tracks A, B and C merged). Gate `bash scripts/check.sh`
exit 0: 930 passed, 21 skipped, 3 xfailed; total coverage 85.25% against the 83% floor;
all seven per-module floors hold — `grants.py` 97.37%, `authority/engine.py` 95.18%,
`config.py` 98.45%, `workspaces/repository.py` 97.47%,
`workspaces/session_enforcement.py` 91.73%, `services/support.py` 94.81%,
`services/supervisor_api.py` 97.70%. `make demo` still ends `writ.ai proof complete` with
zero environment variables, and `WRITAI_ENV=production` still raises the
`WRITAI_GRANT_SECRET` `RuntimeError` at import.

### Standing-rule-8 skips across the plan, and where each stands now

| Track / phase | Premise that failed | State |
|---|---|---|
| T0 | none | — |
| A2 | `CREATE CONSTRAINT` inside the seed transaction (Neo4j `ForbiddenDueToTransactionType`) | **Resolved** after premise correction: the constraint is created in its own schema transaction. |
| A5 | the positive-control test could POST to `/decisions/change` | **Resolved** after premise correction against the real `POST /decisions/ingest`. |
| B | none recorded under rule 8 (B1 steps 6–7 were delivered differently and closed in round 2; see B1-1) | — |
| C5 steps 4–6 | `config.py:66` default | **Resolved**: re-derived to `config.py:103`. |
| C5 post-merge citation verification | citations into three files being rewritten by A and B | **Resolved by T1** (table below). |
| T1 `.env.example` line | orchestrator note: "you may add that one line to `.env.example`" for `WRITAI_HOOK_API_KEYS` (B2-1) | **Skipped under rule 8.** `backend/tests/test_runtime_config.py:106-118` `test_every_documented_env_var_has_a_settings_field` fails for any `NAME=` in `.env.example` that is neither in `_READ_OUTSIDE_SETTINGS` (`:77`, whose hook entry at `:80` lists only `WRITAI_HOOK_API_KEY`) nor present in `config.py` source, and `WRITAI_HOOK_API_KEYS` is neither. That test file is T0's, outside T1's ownership. The goal still matters: B2-1 needs one `.env.example` line plus one allow-list entry in the same change. **Superseded — both landed in `76afc1e`; see B2-1 in the B2 table. This row records why it was skipped at the time, not its current status.** |

### T1 step 1 — the cross-track line

- Landed: `backend/writai/workspaces/authority_contexts.py:466` passes
  `report=context.authority.last_report`. Test:
  `test_a_workspace_authorization_still_reports_its_own_invalidation_path`
  (`backend/tests/test_live_workspaces.py`, both store parameters).
- **Premise correction, disclosed.** The T1 prompt states that until the line lands
  workspace `/authorize` responses "silently return empty" provenance fields. On the merged
  tree they do not: A5 kept a fallback at `backend/writai/authority/engine.py:472`
  (`resolved_report = report if report is not None else self.last_report`), and each
  workspace context owns its own `IntentAuthority`, so the fallback is the same object the
  explicit argument passes. The requested reverted control therefore passes with and without
  the line (2 passed both ways). With the fallback removed locally (`resolved_report = report`,
  never committed), the test passes with the line and fails without it (`assert [] != []`),
  so it covers the line under the condition the engine's own comment at `:365` anticipates
  ("Track B call sites do not yet pass report"). Whether to drop the fallback now that every
  in-process call site passes `report=` is Track A's decision; T1 did not touch `engine.py`.

### Known-limits citations re-verified on the merged tree

| Citation as Track C wrote it | Merged tree | Action |
|---|---|---|
| `services/support.py:170` (token derivation) | `def internal_service_token` at `:177` (`:170` was `code="RATE_LIMITED"` on Track C's own tree too) | corrected → `:177` |
| `services/support.py:180` (enforcement) | `def require_internal_service` at `:187` | corrected → `:187` |
| `workspaces/authority_contexts.py:173` | `def _signing_secret` at `:188` | corrected → `:188` |
| `workspaces/repository.py:89` | JSON `def get` at `:108` | corrected → `:108` |
| `workspaces/session_enforcement.py:244` | `class RepositorySupervisorAssignmentGateway` at `:246` | corrected → `:246` |
| `workspaces/repository.py:63-64` | `temporary.replace` / `chmod(0o600)` at `:82-83` | corrected → `:82-83`; bullet now names the JSON repository |
| `graph/neo4j_store.py:115`, `:140`, `:145` | `add_artifact` (`:145`) raises `ValueError` at `:152`; `list_artifacts` (`:174`) has `ORDER BY a.id` at `:176`; `add_edge` (`:180`) raises `KeyError` at `:189` — Track A closed all three | **prose was false, not merely stale**: bullet rewritten to name the closed items and the one still open (below) |
| `config.py:103`, `config.py:44`, `config.py:212`; `grants.py:13-17` | unchanged | confirmed as written |

### Dead code retained under `AGENTS.md:34` invariant 8 (mentioned, not deleted)

- `backend/writai/workspaces/live_interrupt.py` (`LiveClaudeCodeInterruptPort`): used only by
  `backend/tests/test_claude_code_enforcement.py:25,266,327`.
- `backend/writai/supervisor_contract.py:35` (`NullSupervisorInterruptPort`): used only by
  `backend/tests/test_supervisor_contract.py:7,62`. The module around it is live.
- `backend/writai/workspaces/interrupt_port.py` is **not** dead: `scripts/demo/seed.py:47,479`
  uses `WorkspaceSupervisorInterruptPort`, as does `backend/tests/test_claude_code_runtime.py:20,229,251`.

### Latent parity divergence (Track A found it and correctly did not fix it) — resolved

`MemoryGraphStore.outgoing_edges` (`backend/writai/graph/memory.py:70-75`) returned insertion
order; `Neo4jGraphStore.outgoing_edges` (`backend/writai/graph/neo4j_store.py:207`) has
`ORDER BY target_id, kind`. Latent because the authority traversal re-sorts with
`authority_edge_sort_key` (`backend/writai/authority/engine.py:262`), and
`test_outgoing_edges_is_ordered_by_target_then_kind` in the contract suite inserted its edges
already sorted, so the suite could not observe it either.

**Resolved** in its own change: memory now sorts by `(target_id, kind)`, and the contract test
inserts `(B-2, CREATES), (A-1, DECOMPOSES_TO), (A-1, CREATES)` — reversed on both keys.
Control: with the sort reverted by hand the test fails on `[memory]` with
`('B-2', CREATES) != ('A-1', CREATES)` at index 0; restored, it passes on `[memory]` and
`[neo4j]`. No caller depended on insertion order: the engine traverses via
`downstream_subgraph`, the orchestrator re-sorts, and `test_selective_invalidation.py` only
counts calls.

### Still open after integration

- B2-1 (`.env.example` `WRITAI_HOOK_API_KEYS`) — **resolved** post-integration; see the B2 table.
- B1-3 (sibling stores named `.sqlite3`),
  B2-2 (`HookApiKeyVerifier` alias), B2-4 (session list not filtered by owner), T0-1
  (`uv.lock` consumed by nothing), T0-2 (two unread env vars), INT-2 (PR check grant
  validation), A1-1 to A1-5, DEMO-ENV-1 to DEMO-ENV-4. The `outgoing_edges` divergence above,
  RC-1, RC-2 and B1-2 (orchestrator read-modify-write sites) are now resolved.

---

## Polish — PR 2, the trust boundary

A route audit found 49 mutating routes across the three services, 26 of them with no guard.
PR 2 guarded the dangerous subset and wrote the rest down rather than sweeping all 26: a sweep
would need either a browser-shipped secret, which `frontend/src/approvals/api.ts:100-121`
rejects on purpose, or a session layer the repository does not have.

| # | Item | Severity | Why it is open |
|---|---|---|---|
| P2-1 | **23 mutating routes remain unauthenticated by design: 7 on the authority service (shared-runtime `/authorize` and `/grants/verify`, five Scenario Lab context routes) and 16 on the agent service (the `/demo/*` steps, Scenario Lab runs, and the Workspace import/authorize/propose/cancel/plan/reauthorize/grant-verify routes).** Anyone who can reach the ports can re-trigger an already-approved action or reset and corrupt demo state. On their own they cannot create authority a human did not grant (the demo-gated ingest route can; see P2-2): the baseline check (`workspaces/authority_contexts.py:452-458`), `evaluate_plan`'s `REPLAN`-without-grant path (`authority/engine.py:485-487`), fingerprint-bound approvals (`agent_api.py:1261-1273`, `STALE_CONFIRMATION`) and Callwright's one-entry allowlist (`executor_api.py:189`) hold regardless of caller. | Medium (disclosed) | **TRACKED.** The browser posts to these routes with no session layer, so the fix is a session layer, not a header. Closed in this PR: `POST /execute` and the workspace-context `authorize` / `grants/verify` routes now require the internal-service capability, so a leaked grant token cannot be replayed into a live call from the network. `backend/tests/test_route_authentication.py` freezes every mutating route to a tier and reads the README counts back, so a new unguarded route must be added to the map consciously. The README section *Where the trust boundary is* is the operator-facing statement. |
| P2-2 | **Anonymous `POST /decisions/ingest` → `POST /authorize` mints a signed grant while the demo gate is open.** Found by Sol in PR 2 round 1. `/decisions/ingest` (`services/authority_api.py:250`) accepts a caller-asserted **approved** `DecisionMutation` and applies it to the shared runtime (`:258`) with no identity check, only `settings.demo_reset_enabled`. A plan that satisfies the ingested requirement then gets `ALLOW` and a real grant from the open `/authorize` route (`:1696`); a non-matching plan gets `REPLAN` and no grant, which is why a casual probe misses it. The gate is open on a fresh clone: `WRITAI_ENV` defaults to `development` (`config.py:33`) and the flag defaults to on for a demo environment on the memory backend (`config.py:83`, `:95`). Round 1's README sentence claimed the open routes could not create authority; that was false in the default configuration. | Medium (disclosed, demo-gated) | **TRACKED, by design.** The seam is the demo path and stays. `WRITAI_ENV=production`, or any value outside `DEMO_ENVIRONMENTS` (`config.py:51`), makes step 1 answer `403 FIXTURE_INGEST_DISABLED`, and `require_production_secrets` (`config.py:273`) refuses to boot such a deployment on a published placeholder secret. Round 2 rewrote *Where the trust boundary is* to state the precondition where the claim is made and to name the two-step chain. `backend/tests/review/test_pr2_trust_boundary.py` runs the chain both ways and reads the README sentences back: `..._cannot_mint_authority_when_demo_ingest_is_disabled` pins the property we claim, `..._can_mint_authority_while_demo_ingest_is_open` pins the disclosed exposure so it cannot widen silently, and so that anyone who later closes the seam sees it go red and updates the README with it. |
