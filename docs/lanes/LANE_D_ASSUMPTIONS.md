# Lane D — assumptions made overnight

Unattended run, 2026-07-24 night into 2026-07-25. Nobody was available to answer,
so each question below was resolved with the most conservative option and
recorded here. A human should review every one of these.

---

## A1 — Lane B's endpoints exist, but not at the assumed paths

**Question.** `docs/BUILD_LANE_D.md` told me to assume
`GET {AUTHORITY_URL}/approvals/pending` and
`POST {AUTHORITY_URL}/approvals/{id}/approve`, and to adapt `api.ts` if the real
endpoints differed. They differ substantially. Which do I wire to?

**What is actually there** (read from Lane B's merged code on `lane-a`, never edited):

| Purpose | Real route | Service |
|---|---|---|
| list workspaces | `GET /live-workspaces` | agent, :8002 |
| one workspace + its pending proposal + `supervisor.assignments` | `GET /live-workspaces/{workspace_id}` | agent |
| blast radius (`SupervisorInterruptPort.preview()`) | `GET /live-workspaces/{workspace_id}/decisions/{decision_id}/preview` | agent |
| approve | `POST /live-workspaces/{workspace_id}/decisions/{decision_id}/approve` | agent |

There is no `/approvals/pending` and no `/approvals/{id}/approve`. The authority
service does own `/approvals/email/*` and `/approvals/push/*`, but those are the
signed magic-link redeem paths for other channels, not a queue.

**Assumption.** Wire to the real routes on the **agent** service and read the base
URL from `import.meta.env.VITE_AGENT_URL` (default `http://localhost:8002`),
matching how `live-workspace/api.ts` and `scenario-lab/api.ts` already read it.
`VITE_AUTHORITY_URL` is no longer used by this route.

**Conservative because** it uses Lane B's real, tested surface rather than asking
them to add a second one, and it keeps the change inside `frontend/src/approvals/`.

---

## A2 — approve posts, but never with a credential I put in the bundle

**Question.** `POST …/approve` takes an `ApprovalAttemptEnvelope` whose
`approval_token` is resolved through Hexclave to a user id
(`agent_api.py:1642-1676`). `ApprovalChannel.WORKSPACE_UI` is an accepted channel,
so a web approval is anticipated — but the `hexclave_*` config keys ship empty.
Where does the token come from?

**Assumption.** The POST path is fully built — correct envelope, `workspace-ui`
channel, evidence ref, and the `confirmed_proposal_fingerprint` /
`confirmed_proposal_instance_id` pair the server requires so an approval cannot
land on a proposal that changed after the approver saw it. The token is read at
runtime from `globalThis.__WRITAI_APPROVAL_TOKEN__`. **When it is absent —
which is the default — nothing is posted and the screen renders a labelled
rehearsal.**

**Conservative because** the obvious alternative is a `VITE_*` variable, and Vite
inlines those into the client bundle, shipping an approval credential to every
visitor. A `window` value an operator sets for one session is not a good
production answer either, but it is not baked into a build artifact, and it makes
the wiring real and testable instead of stubbed.

**Still for a human:** how a `WORKSPACE_UI` approval should authenticate in
production — most likely a server-side session exchanging a login for a
short-lived token, which is a backend change. Until then `writai approve` is
the path with a real identity behind it.

**What the screen never does:** claim an approval happened. Without a token, on a
server refusal, or when the response does not report which sessions were
interrupted, it renders "Rehearsal · nothing was applied" and names the reason.
The applied receipt is read from the server's own
`supervisor.applied_interrupts`, not from the preview — so if state moved between
preview and approval, the screen shows what happened, not what was predicted.

---

## A3 — no author name exists in the pending payload

**Question.** `PendingChange.source` needs `author`, `authorInitials`, `channel`,
`timestamp`. `PendingApproval` (`intake/approval.py:45`) carries `source_ref`,
`title`, `text`, `effective_at`, `evidence_refs` — and no author.

**Assumption.** Derive `channel` from `source_ref` when it parses as
`slack://<channel>/<id>`, use `effective_at` for `timestamp`, and when no author is
available **attribute the message to the source reference itself** rather than to a
person. `authorInitials` falls back to the channel's first two letters.

**Conservative because** the alternative is inventing a name. On a screen whose
job is to show who decided something, a fabricated author is the worst possible
defect — worse than a blank. The fixture still carries "Dana Kaur" because the
fixture is explicitly labelled fixture.

**For a human:** if Lane B can add the Slack author's display name to
`PendingApproval`, the screen picks it up with no frontend change beyond deleting
the fallback.

---

## A4 — requirement delta is joined client-side from two server values

**Question.** `PendingChange.decision.was` / `.now` are a before/after pair. The
server sends `PendingApproval.requirements` (the new values) but not the old ones.

**Assumption.** `now` comes from `pending.requirements[scope]`; `was` comes from
the currently-approved decision that owns that scope, read from the workspace's
`baseline_decision` / `approved_mutations`. This is the same join
`cli_approve.py:494 _requirement_delta_lines` already does, ported verbatim in
intent. When no prior value is found, `was` renders as `not previously constrained`
rather than guessing.

**This is formatting, not deciding.** Both values are server-owned; the browser
only pairs them for display. It does not decide whether the change applies.

---

## A5 — one rail, many per-assignment provenance paths

**Question.** `WorkspaceApprovalPreview.assignment_provenance_paths` is a map of
assignment id → path, one per interrupted assignment. The mock is a single
vertical rail.

**Assumption.** Render the longest interrupted path as the rail (ties broken by
assignment id, so it is deterministic), mark its nodes `affected: true`, then
append each preserved assignment's task id as an `affected: false` node — which is
what makes the surviving-sibling branch visible. Per-assignment paths that differ
from the rendered one are not silently dropped: `/approvals/why` renders the path
for the session being explained.

**Conservative because** it never merges two different lineages into one
misleading chain.

---

## A6 — no live backend was available to test against

**Question.** Should I start an agent service to verify the wiring end to end?

**Assumption.** No. I unit-tested the adapter against literal payloads copied from
Lane B's Pydantic models (`WorkspaceApprovalPreview`, `PendingApproval`,
`SupervisorAssignment`, `LiveWorkspaceView`) and left the network path unexercised.

**Conservative because** starting the agent service writes
`.writai/live-workspaces.json` in the shared repo root, and Lane C's `reset.sh`
deletes `.writai/` — two other agents are running on this machine all night and
a half-seeded workspace store is a bad thing to hand someone at 9am.

**Known gap, stated plainly in HANDOFF.md:** the JSON shapes are verified against
the models, the HTTP round trip is not. First integration run should expect to fix
field-name details, not structure.

---

## A7 — `origin/main` has moved ahead of local `main`

Observed, not acted on. Local `main` is `e8df117`; `origin/main` is `75d54bd`. My
branch is based on local `main`. I did not fetch, merge, rebase, or push, per the
overnight rules. The morning integrator should expect `lane-d-approvals` to be
behind origin and merge forward rather than assuming it is current.

---

## A8 — defects found in other lanes, not fixed

Recorded per the rule that another lane's wrong code gets written down, not fixed.
See "Known issues" in `HANDOFF.md`. Nothing outside `frontend/src/approvals/`,
`frontend/src/App.tsx` and `frontend/src/App.test.ts` was modified in this branch.
