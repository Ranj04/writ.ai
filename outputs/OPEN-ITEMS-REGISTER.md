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
| INT-1 | ~~`dragback_ci_check.py` silently discards malformed workspace or assignment objects.~~ | ~~Medium~~ | **CLOSED.** The rule is the one `.dragback/attach` settled: absence of binding information is permissive, failure to obtain it is not. A response that parses cleanly and yields no candidates is UNBOUND and passes; a workspace, supervisor, assignments value or assignment that is not the shape it claims raises `MalformedServiceResponse` and fails with its own verdict code. The question was never which field broke — it is whether we got a clean answer at all, so schema drift that still parses keeps passing. |
| INT-2 | **The PR check's grant validation ignores `run_id`, `task_id` and `plan_hash`.** An ALLOW grant for another run, task or plan passes that weaker check when its snapshot and expiry match. | Medium | **DEFERRED — deliberately, do not "fix" this under time pressure.** CLAUDE.md invariant 5 still HOLDS where it is enforced: `services/executor_api.py` verifies the grant, including `run_id`, `task_id` and `plan_hash`, before anything executes. The PR check is a weaker second opinion on top of that, not the only gate. Closing it properly needs a server endpoint that performs canonical grant verification and returns a verdict — **not** a reimplementation of grant verification inside a stdlib-only, 3.9-compatible script, which would be a second copy of the rules that can silently drift from the real one. That is a considered change, not a demo-eve change. |
| INT-3 | ~~`mark_redirect_delivered()` persists `REDIRECTED` before the denial reaches the hook.~~ | ~~High~~ | **CLOSED.** Now deny-until-acknowledged: `/check` returns the redirect without advancing, the hook echoes the `redirect_id` on its next call once the verdict is actually on stdout, and the service advances only then. A lost delivery re-delivers the identical redirect instead of allowing. Proven against the real hook under python3.9. See `INTEGRATION_REPORT.md` §5a. |
| INT-4 | ~~`approvals/api.ts` — approval bindings survive fallback and concurrent stale loads.~~ | ~~Low~~ | **CLOSED proactively.** Local authentication still cannot make the path reachable because the Hexclave project currently has zero teams and therefore no valid `HEXCLAVE_TEAM_ID`, but that absence is not a safety boundary. Bindings are now held in a `WeakMap` keyed by the change OBJECT and frozen on creation, so a fixture card carrying the same composite id was simply never bound and cannot borrow live credentials, and two concurrent loads cannot cross-contaminate. |
| INT-5 | ~~`approvals/api.ts` — a lost or malformed response after a successful approval renders as a rehearsal.~~ | ~~Medium~~ | **CLOSED.** `ApprovalOutcome` is now `applied \| rehearsal \| indeterminate`. A 4xx refusal is a rehearsal (the server decided, nothing landed); a 5xx, a network failure, an unreadable body or a missing partition is indeterminate and is reconciled by re-reading the workspace. A failed read NEVER resolves to "not applied" — the screen says *sent, outcome not confirmed* and points at `dragback dev status`. One wifi hiccup mid-approval no longer makes the UI contradict the room. |
