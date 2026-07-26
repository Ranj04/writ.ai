# Integration report

Four build lanes plus a CI lane, merged and finished. `pre-integration` tags the
tree as it stood before any of this; `pre-integration-lane-c` and
`pre-integration-lane-d` tag those branches. Nothing was force-pushed, no branch
was deleted, and no lane's commits were discarded.

**Suite:** 635 passed, 2 skipped. Frontend 175 passed. `ruff`, `mypy`,
`compileall`, the 53 CI-check tests under Python 3.9, frontend typecheck and the
production build are all clean. The canonical CSV proof and all 12 Examples
pass unchanged.

---

## 1. What merged, and in what order

| # | Merge | Result |
|---|---|---|
| 1 | `main ← lane-a` | Fast-forward. Lane A had already merged Lane B, and Lane E's `scripts/ci/` was already tracked. 579 passed. |
| 2 | `main ← lane-d-approvals` | Frontend only. Two doc conflicts. 579 passed, frontend 165. |
| 3 | `main ← lane-c-demo` | Five conflicts. 579 passed. |
| 4 | Lane E's PR workflow | `.github/workflows/dragback-pr-authorization.yml` was untracked in the working tree; committed. |

Local `main` was 27 commits behind `origin/main` at the start — `origin/main`
already equalled `lane-a`. Lane C and Lane D were both branched from the *stale*
local `main` (`e8df117`), which both lanes flagged. That is the root cause of
most of what follows.

### Every conflict, and how it was resolved

| Conflict | Resolution |
|---|---|
| `ASSUMPTIONS.md`, `HANDOFF.md` (add/add, twice) | Kept Lane A's at the root — it is the integration baseline. Lane C's and Lane D's are preserved verbatim under `docs/lanes/LANE_{C,D}_{HANDOFF,ASSUMPTIONS}.md`. Nothing was discarded; the decisions in them are cited throughout this report. |
| `scripts/demo/README.md`, `ack.sh`, `fallback.sh` | **Took Lane C's wholesale**, as instructed — purpose-built and cross-model hardened. |
| `scripts/demo/seed.py` | **Kept Lane A's.** It is not on `lane-c-demo` at all, so this resolved itself. The two seeders now coexist deliberately; see §4. |
| `scripts/demo/prompts/session-{1..5}.txt` | Byte-identical on both branches. No conflict. |

---

## 2. Phase 1 — conflicts between lanes

### 1. Two hook implementations → one

Lane A's `hooks/dragback_*.py` ships. Lane B's `hooks/claude_code_hook.py` is
deleted.

**The live risk was not the duplicate file.** It was that
`managed-settings.example.json` — the organisation-managed configuration that
project and user settings *cannot override*, the thing that makes enforcement
non-removable — named the **unhardened** implementation on all three hooks. The
one file whose entire purpose is "the developer cannot switch it off" was
pointing at the hook that had not survived the fail-open audit. It now names the
three shipping scripts, and `test_managed_settings_make_the_hook_non_removable_and_short_timeout`
fails if that ever regresses.

**Ported from Lane B's hook** (behaviours Lane A's did not have):

- the **whole stdout payload** stays under 10,000 characters, not just
  `additionalContext`. The field budget drops 10,000 → 9,000 to reserve room for
  the enclosing JSON and the reason. Measured worst case: 8,235.
- an **oversized response body** is a transport failure, which denies, instead of
  an unbounded `read()` inside a process on a command timeout.
- a **non-HTTP endpoint** falls back to the default. `file://` would otherwise
  make `urllib` hand back a local file as a verdict.
- the verdict cache is written **0600 inside a 0700 directory**.

`backend/tests/test_claude_code_hook.py` was **repointed, not deleted**: all
eight of Lane B's assertions are re-expressed against Lane A's surface, plus
three for the ported hardenings and one that fails if a second hook
implementation ever reappears in `hooks/`.

### 2. `GET /supervisor/sessions`

Already present on `lane-a` (Lane A closed it after the handoff was written —
see ASSUMPTIONS A-8). It returned **bindings**, and a binding has no state, so
every caller that needed one made a second `GET /live-workspaces/{id}` per
workspace and could reach a different answer than the hook would.

Each entry now also carries `state`, `decision_snapshot`,
`current_decision_snapshot`, `snapshot_current`, `deny_spent`, `bound` and
`assignment_missing`, read through **the same gateway `check` reads**. Every
field is a fact; `check` is still the only place a verdict is made. The wire
shape stays flat, because the existing readers accept a bare binding and read
`session_id` at the top level — adding keys cannot break them, moving them would.

---

## 3. Phase 2 — every gap, and its state

| # | Item | Built by | Reviewed by | State |
|---|---|---|---|---|
| 3 | `.dragback/attach` wired end to end | **sol** | fable | **Done**, 1 defect found and fixed |
| 4 | CI `--require-grant` on by default | fable | sol | **Done** |
| 5 | `check.sh` hard-fails on the three silent killers | fable | sol | **Done**, 4 defects found |
| 6 | CrustData observation path | **sol** | fable | **Done**, fixture is reconstructed — see below |
| 7 | Lane D: `/approvals/why` + consistency audit | Lane D / fable | sol | **Done** |
| 8 | Lane C: `--record`, tmux, Superset | Lane C / fable | sol | **Done**, launcher was fully broken — see below |
| 9 | Gate the seeder's auth bypass | fable | sol | **Done** |
| 10 | Real-vs-simulated panel | fable | sol | **Done** |

### 3 — `.dragback/attach` (built by sol)

`dragback dev attach` wrote a file the server never read:
`ClaudeCodeSessionRegistry.attach()` was only ever called from tests, so the
marker that is **first** in the documented binding order was a no-op against the
live service.

The registry now reads `.dragback/attach` during `register()` and honours it in
the explicit slot. The read mirrors `_read_task_file` exactly — `lstat` first,
refuse a symlink or non-regular file, refuse oversized, tolerate
`OSError`/`UnicodeError`.

**Format decision (sol's):** the file keeps carrying the assignment id alone, so
stage directories that already ran `dragback dev attach` keep working. The match
is therefore on assignment id across all candidates, and an id naming more than
one workspace's assignment never resolves to a guess. This is the same rule
`scripts/ci/dragback_ci_check.py` already applies, so the PR check and the
service agree.

**This marker took two rounds of review to get right**, and the final shape came
out of both:

- **fable, reviewing sol:** an *unreadable* marker — a symlink, an oversized
  file, more than one line — stopped at UNBOUND. An unbound session is allowed
  everything, so a corrupt `.dragback/attach` **switched enforcement off**. It
  now falls through to the branch task id and `.dragback/task`, with the binding
  detail recording that the marker was ignored and why. That also matches the CI
  check's `read_marker_file`, which falls through on exactly these cases.
- **sol, reviewing fable:** stopping there was not enough. A marker that *is*
  read successfully but names no live assignment still resolved to UNBOUND — and
  unbound *passes* the PR check as well as being allowed everything by the hook.
  So the off switch survived in a slightly different form. It is now its own
  state, `SessionBindingSource.UNRESOLVED_ATTACHMENT`, which the hook **denies**
  and the check **fails** regardless of `--require-binding`.

The resulting rule is the honest one: *absence* of binding information is
permissive, a *failed request* for binding is not.

### 4 — CI check defaults

`--require-grant` is now the default. The scoping is **structural, not a flag**:
`evaluate` returns at step 1 for an unbound branch and never reaches the grant
check, so a docs PR still passes, while a branch that resolves to a live
assignment must show a current grant. `--allow-missing-grant` turns it off;
`--require-grant` still parses so existing invocations work. `--require-binding`
left off, as instructed.

`RedirectedBranchTests` covers the case a naive implementation fails **forever**:
a branch authorized → invalidated → redirected → re-authorized against the new
snapshot must pass again. Nothing ever clears `invalidated_task_ids`, so a gate
consulting it would permanently fail the developer who did exactly what the
redirect asked. Both halves are covered — the re-authorized branch passes, *and*
the same branch fails before it is re-authorized, so the pass is earned rather
than the check going blind.

`scripts/ci/README.md` now **opens** with the fact that the check is advisory
until *Branch authorization is current* is a required status check, with the
click path for both branch protection and rulesets, and the note that the check
must run once before GitHub offers it by name.

### 5 — `check.sh` (four real defects found by running it)

The killers now get the last word: a dedicated `STOP` block after every other
section, naming which of the three fired and how many. They are checked **per
registered session** using the state the service reports, so a session this
launcher did not create, or one bound into another workspace, is covered too.

Running it against a live seeded service found four defects:

1. **`deny_spent` counted `INTERRUPTED` as spent.** It is the **armed** state —
   the hook denies on it every time and only then advances to `REDIRECTED`.
   Verified against the running service: check #1 denied with mode `once`, check
   #2 allowed. My own definition was wrong, and it would have reported a
   correctly armed stage as broken. Fixed in `SPENT_DENY_STATES`, with a test
   pinning the set.
2. **`demo_api.py` read `task_id`/`assignment_id` flat**, but the binding nests
   them in an `AssignmentLocator` — the same shape bug ASSUMPTIONS A-8 records
   being fixed in the CLI, never fixed here. **Every session read as UNBOUND**,
   so the loudest possible alarm fired on a healthy stage.
3. **`demo_api.py` sent no hook API key**, so `GET /supervisor/sessions` answered
   401 and the check reported "did not answer with a session list" against a
   service that was working fine.
4. **The generated session settings omitted `DRAGBACK_HOOK_API_KEY`.** The
   service fails closed without it, so every hook call would be rejected and
   deny — including the two sessions whose *survival* is the proof.

The snapshot rule is also correctly scoped now. A snapshot behind the graph is
leftover state only **before** a fire; after one it is the expected shape and is
what makes an interrupted assignment deny. And a `continuing` sibling is behind
**by design** — `_apply_supervisor_invalidation` advances it without touching its
snapshot — so flagging it sent the operator to re-seed the one thing that was
working.

### 6 — CrustData observation path (built by sol)

Person id → `ApprovalEvidence.approver_user_id` → decision ids. Deterministic;
no LLM in this path at all.

**Flagging is not invalidating.** The output is a review flag carrying the
person, what changed with old and new values, which decisions they approved, each
approval's evidence ref and time, and a sentence saying why. `graph_mutated` and
`human_confirmation_required` are `Literal`-typed so they cannot drift, and a
test asserts no code path writes `ValidityStatus`, touches `invalidated_scopes`,
or calls `apply_decision_change`.

**The fixture is not a real capture, and says so.** `CRUSTDATA_API_KEY` is empty
and the sandbox has no network, so sol reconstructed the payload from CrustData's
documented Person Entity Watcher shape. The label is pinned as a `Literal` —
*"documentation-reconstructed payload, replayed (not captured from CrustData)"* —
and appears on the API response, the SSE event, the CLI output, and inside the
fixture file. It cannot be shown as live, and it is not claimed to be a real
payload either.

Verified against the real app: unconfigured bearer → 503
`CRUSTDATA_AUTHENTICATION_NOT_CONFIGURED`, wrong bearer → 401, correct bearer →
200, second identical delivery → `duplicate: true` with zero new flags.

**Known limitation, not fixed:** the join compares a CrustData integer person id
against a Hexclave user id. Those are different namespaces, so on real data this
needs an identity mapping. It works on the fixture because both sides are seeded
consistently. Recorded rather than papered over.

### 7 — Lane D

`/approvals/why` was already shipped by Lane D and is confirmed correct: *Needs
redoing* and *Still stands* render as two columns of **equal weight**, and the
rail marks the surviving sibling out-of-scope rather than dropping it.

Lane D acted on three of the seven audit divergences itself and left #1's second
half to an integrator, because adding an `"approvals"` member to `AppShellView`
edits a file both fallback demo routes render. **Owned here.** `AppShell` gains
one plain `<a>`, so the scenario-lab shell never becomes responsible for a route
it does not render.

Verified in a browser, not just in tests, because "stop if anything in `/` or
`/scenario-lab` changes behaviour" was the stated condition: `/` renders the
workspace flow with Workspace still current; `/scenario-lab` renders all 12
Examples with Examples still current; `/approvals` and `/approvals/why` render
with headers aligned across all four routes.

The remaining four divergences stay unfixed for Lane D's stated reasons — three
are the mock's own values, and the token block is duplicated on purpose.

### 8 — Lane C: the launcher could not arm at all

All three items (`--record`, tmux, Superset) were already shipped by Lane C's
final commit. **Running the launcher against the merged tree found something
worse:** `up.sh` and `fire.sh` both call `dragback workspace approve-baseline` /
`approve-change`, and Lane B **disabled both on purpose** — they took `--role` on
trust, and every approval now goes through an `ApprovalAttemptEnvelope` carrying
a Hexclave-resolvable token. Lane C was built on `main` before that landed, so
the launcher stopped dead at *"baseline approved … failed"* and never reached a
session, a pane, or a recording.

- `approve-change` **has** a working replacement — `dragback approve change` is
  implemented now, contrary to Lane C's note that it was a `NOT_IMPLEMENTED`
  placeholder. `fire.sh` uses it and `check.sh` probes it instead of the disabled
  one. It prints the blast radius and asks the operator to confirm, which is the
  product invariant, so the prompt is left alone on stage; only `--yes`
  (documented "not on stage") answers it non-interactively.
- `approve-baseline`'s stated replacement is the authenticated Workspace UI,
  which needs a browser and a token no local demo has. Both approvals therefore
  fall back to `scripts/demo/approve_in_process.py` — the same seam `seed.py`
  already uses. `fire.sh` tries the authenticated path **first, every time**, and
  only falls back after it fails to resolve an approver, by which point the
  operator has already seen the blast radius and confirmed. With `HEXCLAVE_*`
  configured the fallback never runs.

**Re-verified on this machine:** `--record` executes its degradation branch
correctly (`screencapture` needs a one-time macOS Screen Recording grant a human
must give — the success branch remains unexecuted). The tmux layout builds one
operator pane plus five panes rooted in the five session directories on tmux
3.7b. **Superset is absent here**, so the fallback to plain directories is what
ran; the real CLI remains unexercised, exactly as Lane C recorded.

### 9 — The seeder's auth bypass is now opt-in

`seed.py` and `approve_in_process.py` both refuse unless
`DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1`. Only the exact value `1` counts — a
half-set variable is not consent. The gate runs **before** the stage directory is
deleted, which is asserted. The refusal names the variable and says what is and
is not bypassed. The runbook and handoff commands carry the opt-in so nobody
learns it by hitting the error.

What is bypassed: the **channel** authentication. What is not: role, scope,
confidence, the three-way requirement match, and the proposal binding — all run
unchanged, with the fingerprint and instance id read from the stored proposal.

### 10 — Real-vs-simulated panel

Carries the standing caveats in **both** live and simulated mode, because they
are true of the build either way: the PR check only closes the fail-open hole
when it is required *and* can reach its authenticated service with an honest
binding and current grant; the CrustData watcher cannot fire live and its
payload is documentation-reconstructed, not captured; the seeder's
unauthenticated approval and its gate; and that the event broker is process-local
and the session registry in-memory, so a restart makes every open session read
as unregistered.

---

## 4. Phase 3 — the six unconfirmed claims

### Has a Gemini extraction succeeded end to end with the live key?

**Yes, to the human-review boundary.** After the original failed runs recorded
below, Gemini was changed to quote exact source text while Python derives offsets,
to emit mandatory requirements, and to choose only from the workspace's trusted
scope vocabulary. A live rerun on the same sentence produced a valid
`export.authorization` candidate; `SlackDecisionIntake` routed it to `PENDING`
and built a proposal with validated evidence and `human_reviewed=false`.

After structural failures were added to Gemini's retry loop, a second five-run
characterisation on 2026-07-25 passed **5 of 5**: every candidate selected
`export.authorization`, returned a non-empty requirement under that exact key,
and carried evidence that validated after Python derived the quote offsets. A
composed test now takes that candidate through shared human approval and a real
`graph-v18` write; the model still never supplies the approval verdict.

It still does not approve itself. A real signed Composio delivery and authenticated
Hexclave approval have not been exercised here.

#### What originally failed

`GEMINI_API_KEY` is set; `LLM_PROVIDER=gemini`, model `gemini-3.6-flash`, against
the canonical Slack sentence the demo uses (194 characters):

```
Dana Kaur (Compliance) 2:41 PM
Approved - CSV data exports must be restricted to administrators only, effective
immediately. This supersedes our earlier decision to expose exports to all users.
```

The HTTP call works and returns well-formed structured JSON every time. Here is
one complete response, unedited:

```json
{
  "decision_id": "decision-restrict-csv-exports",
  "title": "Restrict CSV data exports to administrators only",
  "supersedes_id": "decision-expose-csv-exports-all-users",
  "affected_scopes": ["compliance", "data-export", "security"],
  "decision_scopes": ["compliance", "data-export", "security"],
  "requirements": null,
  "evidence_spans": [{ "start": 0, "end": 181 }]
}
```

```
evidence_span_error -> Evidence span 0 does not exactly match the supplied source text.
```

#### The demo beat: an LLM proposed structure and deterministic code refused it

Three things in that response are wrong, and **none of them can reach the graph**:

1. **The evidence span does not match the source.** The model claims characters
   0–181 are the quote it relied on; they are not. `evidence_span_error`
   (`llm/extractor.py`) compares `raw_text[start:end] != span.text` and returns
   `HUMAN_REVIEW` with **no graph write**. An earlier run claimed `(0, 202)` on a
   184-character source — a span running off the end of the text it was given.
2. **`requirements` is `null`.** `apply_decision_change` (`engine.py:122-179`)
   requires a three-way exact match between `decision.scopes`,
   `mutation.affected_scopes`, and the keys of
   `decision.attributes["requirements"]`. With no requirements at all that match
   is unsatisfiable, so the mutation cannot be applied under any circumstances.
3. **The scopes are invented.** `compliance`, `security`, `data-export` are
   plausible English but they are not this workspace's scope vocabulary, which is
   `export.authorization` and `export.generation`. Even with requirements
   present, the three-way match would fail against the real graph.

**This is the product's central claim, demonstrated on live model output rather
than asserted in a README.** The model was articulate, confident, and wrong in
three separate ways; deterministic code refused all three; nothing became an
`ALLOW`, a `REPLAN`, a `BLOCK`, or a graph write. Use it as a demo beat — it is
far stronger than the fixture path, because the failure is real and unrehearsed.

#### It is also not deterministic, which is the honest part

Five runs against the identical input:

| Run | Evidence span | Scopes | `requirements` | Span validation |
|---|---|---|---|---|
| 1 | `(0, 202)` on a 184-char source | 1 | missing | **rejected** — runs past the end |
| 2 | `(0, 181)` | 3 | missing | **rejected** — does not match |
| 3 | `(31, 193)` | 3 | missing | passed |
| 4 | `(0, 192)` | 2 | missing | **rejected** — does not match |
| 5 | `(0, 187)` | 3 | missing | **rejected** — does not match |

The span gate catches most runs but not all. **The gate that catches every run is
the missing `requirements`**: it was absent in 5 of 5, so no run could produce an
applicable mutation. The two failures are independent, and the structural one is
the reliable one.

#### The consequence, stated plainly

**Resolved at the extraction boundary.** The model now supplies exact quotes and
mandatory requirements, trusted code computes offsets, and the Slack pipeline
supplies the only scope identifiers the model may choose. The deterministic gate
still rejects fabricated evidence, unknown scopes, malformed requirements, low
confidence, and unauthorized authors. The live rehearsal created a proposal for
human review; it did not create an authority verdict or graph write.

The explicit delta remains the deterministic fallback used by the staged demo:

```bash
dragback approve --text "<the message>" --scope export.authorization \
  --was all_users --now admin_only
```

That bypasses extraction entirely, reads the current requirement from the
workspace's own approved decisions, and refuses if `--was` does not match it.

**The bypass had no tests at all.** It has five now: it builds a
three-way-matching proposal with an `effective_at`; it refuses a stale `--was`
quoting the real current value; it refuses an ungoverned scope; it demands JSON
objects when a scope carries several requirement fields; and it never constructs
an extractor.

This is recorded on the real-vs-simulated panel and in the runbook. Neither
implies that a real Composio webhook or Hexclave-authenticated approval ran.

### Is Composio delivering real webhooks?

**No. Message text is supplied by hand.** `COMPOSIO_API_KEY` is set, but
`COMPOSIO_WEBHOOK_SECRET`, `COMPOSIO_SLACK_AUTH_CONFIG_ID` and
`DRAGBACK_SLACK_CHANNEL_ID` are all empty. `ComposioSlackWebhookVerifier`
raises `SlackWebhookError("COMPOSIO_WEBHOOK_SECRET must be configured.")` at
construction, so no signed delivery can be verified and none has been. The
verification code is real and tested; it has never been fed a live delivery.

### Is Hexclave doing real permission checks?

**Yes — there is no hardcoded allowlist anywhere.**
`HexclavePermissionChecker.has_permission` makes a real HTTP call per
`(user_id, permission_id)` with a short positive/negative cache, and raises on a
malformed response rather than defaulting to allow. A search for an allowlist or
stub checker in `auth/`, `intake/approval.py` and `agent_api.py` returns nothing.

**But it cannot yet be completed, and it fails closed.** The configured project
id and secret server key authenticate, but the Hexclave project currently
returns zero teams. There is therefore no valid `HEXCLAVE_TEAM_ID` to configure;
inventing one would only turn a provisioning gap into a misleading credential.
The checker requires project id, secret key and team id, so authenticated
approval remains unavailable and the demo still needs the gated in-process seam.

### Does every approval channel funnel through one `approve()`?

**Yes.** All five `ApprovalChannel` members route through
`ApprovalCoordinator.approve`, which is the only place a permission check runs.
There are exactly three call sites: `notify/slack.py` (slack reaction) and two in
`agent_api.py` (one for workspace-ui/cli, one carrying `attempt.channel` for
push and email).

Per-channel tests exist: CLI and slack-reaction in `test_approval_coordinator.py`
and `test_slack_reaction_e2e.py`; workspace-ui in `test_live_workspaces.py` and
`test_workspace_approval_recovery.py`; push and email in
`test_notification_approval_routes.py`, `test_push_notifications.py` and
`test_email_notifications.py`. `test_every_approval_channel_uses_the_shared_permission_check`
is parametrized over `tuple(ApprovalChannel)`.

**Gap I closed:** that parametrized test proves the coordinator checks
permission for every channel — it cannot prove a channel *uses* the coordinator.
A sixth route minting its own `ApprovalEvidence` would pass it and skip the check
entirely. The construction sites are now pinned to four: the coordinator itself,
the envelope builder in `agent_api`, and the two demo-only seams that refuse
without the opt-in.

### Does the five-session demo run from a cold machine, twice, with a re-seed?

**Yes.** Two consecutive full cycles through Lane C's launcher — `reset.sh` →
`up.sh` → register five sessions → `check.sh` → `fire.sh` — with a full reset
between:

```
REHEARSAL 1   check.sh: 0 silent killers      RESULT: 3 denied, 2 allowed
REHEARSAL 2   check.sh: 0 silent killers      RESULT: 3 denied, 2 allowed
```

Identical both times. The second run still denies, which is the failure mode
Lane A's handoff called "most likely to embarrass you on stage".

Separately proven through the real `PreToolUse` route on the merged service:
three sessions denied **once** naming `DEC-018` at `graph-v18` with the full
provenance path, two preserved on the untouched scope, and all five allowed on
the second call — deny-once holding exactly.

### Do the canonical CSV proof and all 12 Examples still pass?

**Yes, unchanged.** `make demo` runs the full six-step proof: `graph-v18` arrives,
`TASK-101` is preserved while `TASK-102` is invalidated along
`DEC-018 → DEC-004 → SPEC-009 → TICKET-100 → TASK-102`, the executor rejects the
stale `graph-v17` grant, the loop enters `REPLAN`, and the corrected grant is
accepted. All 12 scenarios are served by `/scenario-lab/scenarios` and all 12
render in the browser.

---

## 5. Which model built what, and who reviewed it

Every item was built by one model and adversarially reviewed by the other.
Neither reviewed its own work.

| Item | Built | Reviewed | Outcome |
|---|---|---|---|
| `.dragback/attach` wiring | sol | fable | 1 defect found (enforcement-off via corrupt marker), fixed |
| CrustData observation path | sol | fable | Accepted; fixture provenance verified honest |
| Hook consolidation | fable | sol | See below |
| Session-list enrichment | fable | sol | See below |
| CI check defaults | fable | sol | See below |
| `check.sh` hardening | fable | sol | 4 defects found by *running* it, all fixed |
| Lane C/D finishing | fable | sol | See below |
| Seeder gate + panel | fable | sol | See below |

### Defects the reviews found

**fable reviewing sol — `.dragback/attach`:** an unreadable marker resolved to
UNBOUND, and an unbound session is allowed everything, so writing one corrupt
file in a working directory switched enforcement off for that session. Fixed by
falling through to the remaining binding rules while recording in the binding
detail that the marker was skipped and why. Confirmed, not disproved.

**sol's own disclosure:** it could not commit (the worktree's git metadata sits
outside the writable sandbox) and five socket tests could not run under sandbox
loopback restrictions. Both were re-run in the main tree and pass. sol also
flagged a `scripts/ci/` parity follow-up it was forbidden to touch; that turned
out to need no change to the check at all — the fix above brought the service to
the check, not the other way round.

**Defects found by execution rather than by reading** (the four in `check.sh`,
the Lane C launcher break, and the wrong `deny_spent` definition) are recorded in
§3 above. The `deny_spent` error was **mine**, found by running the code against
a live service after the tests I had written agreed with me.

### sol reviewing fable — the final pass over the whole integration diff

`gpt-5.6-sol`, read-only, against `git diff pre-integration...HEAD` and
`CLAUDE.md`. It returned **ten** defects. **Nothing was disproved** — every one
was a real weakness. **Nine are now closed; INT-2 alone is deliberately
deferred.**

**Closed in the initial integration pass:**

1. **The check could be rewritten by the branch it checks.** The workflow ran
   `scripts/ci/` *from the PR head*, so one commit changing
   `dragback_ci_check.py` to `sys.exit(0)` disabled the backstop — and the same
   step handed that edited code `DRAGBACK_CI_API_KEY`. **The most serious finding
   in the entire integration.** The checker now comes from the PR base; the head
   is checked out separately as inspected data and nothing from it is executed.
2 & 5. **`.dragback/attach` was an off switch,** on both sides. An attachment
   read successfully but naming no live assignment resolved to UNBOUND — which
   *passes* the PR check and is *allowed everything* by the hook. One junk marker
   file opted a branch out of enforcement. Now its own state
   (`UNRESOLVED_ATTACHMENT` / `unresolved_explicit`), which the hook denies and
   the check fails regardless of `--require-binding`. The workflow also passes
   `--require-binding`. sol was right that my earlier fix, which only covered
   *unreadable* markers, stopped short.
7. **The hook could outlive its own deadline.** `MAX_TIMEOUT_SECONDS` was 10
   against a 5-second command timeout, so a slow supervisor got the process
   killed before it wrote anything — which Claude Code treats as allow. Ceiling
   dropped to 4s, subprocess budgets to 2s, with a test asserting every budget
   stays under the timeout the shipped settings actually configure.
10. **An abandoned CrustData reservation lost the review.** A crash between
   reserving a delivery and completing it left it permanently `reserved`, and
   every retry answered "duplicate, zero flags" — silently dropping the human
   review it should have raised. A reservation that never completed is now
   reclaimed; only a completed one blocks.

**Confirmed and since closed:**

3. **`dragback_ci_check.py` silently discarded malformed workspace or assignment
   objects**, so a degraded response could empty the candidate set, resolve the
   branch to UNBOUND and *pass*. Closed with the rule `.dragback/attach` settled:
   absence of binding information is permissive, failure to obtain it is not. A
   clean answer with no candidates still passes; a workspace, supervisor,
   assignments value or assignment that is not the shape it claims raises
   `MalformedServiceResponse` and fails under its own verdict code. Schema drift
   that still parses keeps passing.
6. **`session_enforcement.py` persisted `REDIRECTED` before the denial reached
   the hook.** Closed with deny-until-acknowledged — see §5a. A lost delivery now
   re-delivers the same redirect instead of allowing.
8. **Approval bindings survived fallback and concurrent stale loads.** Closed —
   proactively, because failed local authentication was never a durable safety
   boundary. The Hexclave project still has zero teams and no valid
   `HEXCLAVE_TEAM_ID`, but the path is safe before that provisioning gap is
   resolved. Bindings now live in a `WeakMap` keyed by the change **object** and
   frozen on creation, so a fixture card sharing the composite id was never bound
   and cannot borrow live credentials.
9. **A lost response after a successful approval rendered as a rehearsal.**
   Closed — see §5b. This was the one that could contradict the room.

**Deferred deliberately — do not "fix" under time pressure:**

4. **`dragback_ci_check.py` — grant validation ignores `run_id`, `task_id` and
   `plan_hash`.** An ALLOW grant for another run, task or plan passes if its
   snapshot and expiry match.

   **Invariant 5 still holds where it is enforced.** `services/executor_api.py`
   verifies the grant — including those three fields — before anything executes.
   The PR check is a weaker second opinion layered on top, not the only gate.

   Closing it properly needs a **server endpoint** that performs canonical grant
   verification and returns a verdict. It must *not* be closed by reimplementing
   grant verification inside a stdlib-only, 3.9-compatible script: that would be
   a second copy of the rules, free to drift from the real one, in the file whose
   whole value is that it mirrors the service exactly. Considered change, not a
   demo-eve change.

`outputs/OPEN-ITEMS-REGISTER.md` records all five integration dispositions.
INT-1, INT-3, INT-4 and INT-5 are closed; INT-2 is the sole deferred finding.

### 5a. INT-3, closed: deny **until acknowledged**

`/check` returns the deny and the redirect and no longer advances the
assignment. The verdict carries a `redirect_id` — the existing
`_interrupt_key`, which is content-derived, so it is stable across
re-deliveries and changes when the redirect itself changes. The hook records
that id in the verdict cache it already keeps and echoes it as
`acknowledged_redirect_id` on its next `/check`. The service advances only
when the id matches the current interrupt. No extra round trip, no new
endpoint.

**The ordering is the fix.** The acknowledgement is written only after
`emit_json` succeeds, so a hook killed between receiving a verdict and writing
it acknowledges nothing. A replayed cached deny never acknowledges either —
the service did not see that call, so its interrupt stays open.

Degrades correctly: a lost delivery costs one repeated redirect, never a
missed one. It still terminates — one delivered response ends it.

Proven against the real hook binary under `python3.9` and a live service.
Delivery wiped after every call:

```
Delivery keeps failing (cache wiped after every call):
   call: deny
   call: deny
   call: deny
   call: deny
Delivery finally lands (cache kept):
   call: deny
   call: allow

Assignment state on the service:
   state=redirected  deny_spent=True
```

Before the change, call two allowed.


---

### 5b. INT-5 and INT-4, closed: the approval screen cannot contradict the room

**INT-5.** A lost or malformed response *after* a successful approval used to
render as a rehearsal — the screen saying *no approval was recorded* while the
interrupt had landed and agents were visibly being redirected behind it. One
wifi hiccup mid-approval and the UI contradicted reality on stage.

`ApprovalOutcome` is now `applied | rehearsal | indeterminate`:

- a **4xx** refusal is a rehearsal — the server decided, nothing landed;
- a **5xx**, a network failure, an unreadable body, or a response carrying no
  partition is **indeterminate**, and is reconciled by re-reading the workspace,
  whose `supervisor.applied_interrupts` is the server's own record of what it did;
- if that re-read *also* fails, it stays indeterminate. **A failed read is never
  evidence that nothing happened.** The screen says *Sent · outcome not
  confirmed*, names the reason, and points at `dragback dev status`.

Under-reporting a real mutation is the worse error, so the boolean is gone.

**INT-4.** Approval bindings lived in a `Map` keyed by `PendingChange.id`, the
composite `"{workspaceId}:{decisionId}"` — and the fixture reuses the live ids on
purpose. A fixture card could find a live binding left behind by an earlier load
and POST a real approval from a screen labelled *fixture data*.

I had recorded this as unreachable while no local approval could authenticate.
That was only an accidental guard, so INT-4 was fixed before authentication can
make the path reachable. The Hexclave project currently has zero teams and no
valid `HEXCLAVE_TEAM_ID`, but the implementation no longer relies on that.
Bindings now live in a `WeakMap` keyed by the change **object** and frozen on
creation, so the fixture's objects were never bound and two concurrent loads
cannot cross-contaminate.

## 6. Known limits, stated plainly

- **The PR check is now required on protected `main`.** GitHub reports strict
  status checks with *Branch authorization is current* in the required contexts;
  force-pushes and deletions are disabled.
- **The required check is configured but not operationally green.** It fails
  closed until a GitHub runner can reach a narrowly exposed authenticated agent
  service and the PR has an honest task binding plus a current unexpired grant.
  A missing URL, unreachable service, missing binding or missing grant blocks
  the merge; none is treated as a pass.
- **INT-2 remains deliberately deferred.** The PR check is a weaker second
  opinion because it does not validate `run_id`, `task_id` or `plan_hash`; the
  executor verifies all three before execution. Proper closure requires a
  canonical server verification endpoint, not duplicated rules in the
  stdlib-only Python 3.9 checker.
- **Hooks still fail open** when the process never starts or is killed.
  `allowManagedHooksOnly` stops a developer *removing* the hook, not the process
  dying.
- **Composio has never delivered a live webhook** here. Message text is supplied
  by hand.
- **Hexclave is real but cannot yet be fully configured.** The project and
  secret credentials authenticate, but the project has zero teams, so there is
  no valid `HEXCLAVE_TEAM_ID`. Authenticated approvals fail closed and the demo
  uses the gated in-process seam.
- **The CrustData fixture is reconstructed from documentation**, not captured.
  `CRUSTDATA_API_KEY` is unset, and its person-id → user-id join still needs a
  real identity mapping.
- **Superset was never exercised against the real CLI.**
- **`--record`'s success branch is unexecuted** — it needs a one-time macOS
  Screen Recording permission a human must grant.
- **`EventBroker` is process-local** with a 100-event history, and the session
  registry is in memory: a service restart makes every open session read as
  unregistered, survivors included.
- **The JSON store is single-writer.** `approve_in_process.py` writes it from a
  second process, which is safe only in the arm-time window where nothing else
  is writing.
- **`NullSupervisorInterruptPort` is still the active binding** in
  `services/agent_api.py`. That one-line swap remains deliberately left for a
  human — it changes what the running service does for every lane.

---

## 7. What is not live

Say all of this out loud rather than letting a screen imply otherwise. It is on
the real-vs-simulated panel and at the top of `docs/STAGE_RUNBOOK.md`.

| | Status |
|---|---|
| Scope-aware invalidation, provenance, grant signing, stale-grant rejection | **Real** |
| `PreToolUse` enforcement against live Claude Code sessions | **Real**, proven end to end |
| Slack → decision **extraction** | **Live to a pending proposal in direct rehearsal.** No real Composio delivery or authenticated approval. See §4. |
| Composio webhook delivery | **Never delivered here.** Message text supplied by hand. |
| Hexclave approval identity | **Real code, blocked on provisioning.** The project has zero teams, so no valid `HEXCLAVE_TEAM_ID` exists; the demo uses the gated in-process seam. |
| CrustData person watcher | **Replayed.** `CRUSTDATA_API_KEY` is unset and the payload is documentation-reconstructed, not captured. |
| Superset worktrees | Wired, **never run against the real CLI**. |
| `--record` success branch | **Unexecuted** — needs a one-time macOS permission a human must grant. |

## 8. Run the demo from a cold machine

```bash
# 0. Once per machine.
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 1. Green build. Record the exact final totals after this run.
PYTHONPATH=backend .venv/bin/python -m pytest

# 2. The demo approves without channel authentication. Say so, once per shell.
export DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1
export DRAGBACK_HOOK_API_KEY=dragback-demo-hook-key
export DRAGBACK_DEMO_PYTHON="$PWD/.venv/bin/python"

# 3. Reset, arm, and prove it is armed. Never skip the reset between rehearsals:
#    deny-once is per assignment.
scripts/demo/reset.sh
scripts/demo/up.sh                 # arms 5 sessions in tmux and STOPS
scripts/demo/check.sh              # must print READY with 0 silent killers

# 4. On cue. It prints the blast radius and asks you to confirm. Answer it.
scripts/demo/fire.sh

# 5. The beat. `dragback` is a console script inside the venv and is NOT on PATH
#    unless you activate it — ASSUMPTIONS A-7. Use the explicit path, or run
#    `source .venv/bin/activate` first.
.venv/bin/dragback dev status              # 3 interrupted, 2 continuing
.venv/bin/dragback dev why <session-id>    # the path from the decision to that task
scripts/demo/ack.sh                        # the human beat — releases blocked sessions

# 6. If the live run dies.
scripts/demo/fallback.sh           # newest recording, full screen
```

Two-terminal alternative, using Lane A's seeder instead of Lane C's launcher —
do **not** run both at once, they collide on ports and bind sessions to
different stores:

```bash
DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1 \
  PYTHONPATH=backend .venv/bin/python scripts/demo/seed.py --serve
# wait for "agent service listening", then in two more terminals:
cd /tmp/dragback-stage/sara  && claude "$(cat PROMPT.txt)"   # survives
cd /tmp/dragback-stage/priya && claude "$(cat PROMPT.txt)"   # denied once
```

The frontend must run on **port 5173** — `DEMO_FRONTEND_ORIGINS` allows only
`localhost:5173` and `127.0.0.1:5173`, and any other port fails CORS and renders
"could not load" against a service that is working fine.

```bash
cd frontend && npm install && npm run dev -- --port 5173
```
