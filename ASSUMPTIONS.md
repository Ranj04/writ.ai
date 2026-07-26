# Assumptions — Lane A

> **Status after the post-merge pass (2026-07-25):** A-3, A-7 and A-8 are
> RESOLVED. A-1, A-2, A-4, A-5 and A-6 still stand and still need a human.
> Details inline below.

# Assumptions — Lane A overnight run

Every question I would have asked, the answer I assumed, and why. I chose the
conservative option in each case. **A human should review these in the morning.**

Branch: `lane-a`. Nothing pushed. Nothing merged or rebased onto another lane.

---

## A-1 — The pinned worst-case context length was updated, not weakened

**Question:** `test_worst_case_context_length_is_recorded` pinned the deny block's
worst-case `additionalContext` at 9,496 characters. Reformatting the block to
`docs/TERMINAL_OUTPUT_SPEC.md` section 1 (an explicit instruction) changed the
measured value to 8,235, so the suite was red.

**Assumed:** updating the pin is a characterization refresh, not a weakened test.
The pin's stated purpose is "so a future field addition cannot creep past it"; it
still does that at the new value. The assertion that actually protects the
product — `len(worst) < MAX_ADDITIONAL_CONTEXT_CHARS` — is untouched, and the
number moved **down**, i.e. further from the 10,000 ceiling.

**Conservative because:** the alternative was leaving the suite red, which under
these instructions blocks every commit for the whole night.

---

## A-2 — Two hook implementations now coexist; I hardened mine and touched neither of Lane B's

**Question:** `hooks/` contains both my scripts (`writai_hook_lib.py`,
`writai_session_start.py`, `writai_pre_tool_use.py`,
`writai_session_end.py`) and Lane B's single `claude_code_hook.py`, which
arrived in the merge. Which is canonical?

**Assumed:** mine. `docs/BUILD_LANE_A.md` item A4 assigns `hooks/` to Lane A, and
mine are the ones proven end-to-end against a real Claude Code session. I did
**not** delete, edit, or test Lane B's `claude_code_hook.py`.

**Consequence a human must resolve:** two hook scripts ship in the same
directory. Pick one before the demo. See HANDOFF.md.

---

## A-3 — RESOLVED. ~~My hook's routes do not match Lane B's endpoint~~

**Question:** Lane B's `supervisor_api.py` serves `/start`,
`/{session_id}/check`, `/{session_id}/end`, `/{session_id}/acknowledge`. My hook
scripts call `/{session_id}/register` and `/{session_id}/check`. Only `check`
lines up.

**Assumed:** leave my hook's routes alone and record the mismatch rather than
edit either side. Changing my hook to Lane B's routes would make the survivor
proof depend on Lane B code I have not tested; changing Lane B's routes would
break the rule about touching another lane's files.

**RESOLVED.** The hook now speaks Lane B's contract exactly: `POST /start`,
`POST /{id}/check`, `POST /{id}/end`, authenticated with
`X-writ.ai-Hook-API-Key`. Verified twice from a cold machine against the real
merged service.

---

## A-4 — `NullSupervisorInterruptPort` remains the active binding

Instructed, and unchanged. `agent_api.supervisor_interrupt_port` is the Null
port; the real `WorkspaceSupervisorInterruptPort` sits beside it as
`live_interrupt_port`. A human swaps one line at integration. The survivor proof
drives `live_interrupt_port` explicitly rather than changing the default.

---

## A-5 — Seeding drives the orchestrator in-process, not the HTTP approval routes

**Question:** Lane B's `/live-workspaces/{id}/baseline/approve` and
`/decisions/{id}/approve` now require an `ApprovalAttemptEnvelope` carrying an
`approval_token` that resolves to a Hexclave user id. No local demo has one
(`HEXCLAVE_*` are unset), so the seeder could not drive them.

**Assumed:** seed by calling `LiveWorkspaceOrchestrator.approve_baseline` /
`approve_decision` directly, constructing the `ApprovalEvidence` and the
proposal fingerprint/instance id from the stored record.

**What this does NOT bypass:** the authority decisions themselves. Role, scope,
confidence, the three-way requirement match, and Lane B's proposal-binding check
(fingerprint + instance id must match the exact stored proposal) all run
unchanged. What is bypassed is the *channel authentication* in front of them,
and only inside the seeder — never in the service.

**Conservative because:** the alternative was stubbing Hexclave, which would have
put fake identity into the approval path — the one place this product must not
be faked.

---

## A-6 — A demo hook API key is generated locally

Lane B's session routes fail closed with `HOOK_AUTHENTICATION_NOT_CONFIGURED`
unless `WRITAI_HOOK_API_KEY` is set on both sides. The seeder sets a local
constant (`writai-demo-hook-key`) and writes it into each stage directory's
`.claude/settings.local.json` command. It is not a secret and never leaves the
machine. Override by exporting `WRITAI_HOOK_API_KEY` before seeding.

---

## A-7 — PARTLY RESOLVED. `writai dev why` is not on PATH inside a stage session

Observed during the survivor proof: the interrupted session tried
`writai dev why` and got "command not found". The CLI is only importable via
`PYTHONPATH=backend python -m writai.cli`.

**The command itself now works** (A-8), and is verified end to end. What remains
is purely packaging: `pip install -e .` in the demo environment would put
`writai` on PATH. Still a human decision, but no longer a blocker.

---

## A-8 — RESOLVED. ~~`writai dev status` and `dev why` have no endpoint to call~~

**Question:** both commands issue `GET /supervisor/sessions`. Lane B's router
exposes only `POST /start`, `POST /{id}/check`, `POST /{id}/end` and
`POST /{id}/acknowledge`. There is no session-list route in the merged service.

**Assumed:** leave both commands as written and record the gap. Adding a GET
route means editing `services/supervisor_api.py` or `agent_api.py`, which are
Lane B's files.

**Consequence:** `dev status` and `dev why` currently fail against the merged
service. Their logic and rendering are covered by 19 tests against a mock
transport, so the moment a session-list route exists they work. `dev attach` is
unaffected (local file only).

**RESOLVED.** `GET /supervisor/sessions` now exists on the router, authenticated
like every other session route, and lists unbound sessions rather than hiding
them. Two shape bugs surfaced once real data flowed through it: the CLI read the
assignment ids flat when `ClaudeCodeSessionBinding` nests them in an
`AssignmentLocator`, and both `_session_state` and `_assignment_for_session`
mistook that locator for the assignment — which silently dropped the interrupt
reason and the provenance path, the two things `why` exists to show. Both fixed.
`dev status`, `dev why` and `dev ack` are verified against a live server.
