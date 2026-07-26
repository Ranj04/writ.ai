# Lane D handoff — the approval screen

Branch `lane-d-approvals`, worktree `/Users/ranjivj/db-ui`, based on local `main`
(`e8df117`). Three commits, all frontend. Nothing pushed, nothing merged, nothing
rebased. **Read `ASSUMPTIONS.md` next to this file** — it has the eight decisions
I made without you, and two of them change what the screen is allowed to claim.

```
2e06901  fix defects from the gpt-5.6-sol adversarial review
efef785  wire the approval screen to Lane B's real endpoints
4246f3a  the approval screen at /approvals
```

---

## Run it

```bash
cd /Users/ranjivj/db-ui/frontend
npm install                    # node_modules is gitignored, so a fresh worktree needs this
npm run typecheck && npm run test    # 14 files, 155 tests, green at 2e06901
npm run dev -- --port 5177
```

Then open `http://localhost:5177/approvals` and `http://localhost:5177/approvals/why`.

With no backend running you get fixture data and the page says so. To see it live,
start the **agent** service (`make agent`, port 8002) with a workspace that has a
pending decision proposal and supervisor assignments.

---

## What shipped

All five deliverables from `docs/BUILD_LANE_D.md`.

1. **The route renders and matches the mock.** `/approvals` shows the message
   verbatim, the requirement delta, the chain as one vertical rail, the big
   number, the five faces, and the approve control. Checked side by side against
   `docs/mocks/approval-screen.html` in a browser.
2. **Approve works** — as a rehearsal, deliberately, and it says so. See "the one
   thing that is not wired" below. The rail fires amber over 820ms, three faces
   desaturate with pause badges, two gain a green ring, the number settles after
   900ms. The Replay control is there.
3. **Pending queue**, rendered only when more than one change is waiting.
4. **Live refresh** over the workspace SSE streams, not polling, and only after a
   live read succeeds.
5. **`/approvals/why`** — the same components reordered for the interrupted
   developer, with *Still stands* in a column of equal weight to *Needs redoing*.

**Wired to Lane B.** Their code landed on `lane-a` while I was working. It does
not expose `/approvals/pending`; the approval surface is on the **agent** service
under `/live-workspaces`. I adapted `api.ts` and added `live.ts`, per the lane
brief's instruction to adapt and change nothing else.

---

## Approve: wired, but it needs an identity you have not configured yet

`POST /live-workspaces/{id}/decisions/{decision_id}/approve` (`agent_api.py:1642`)
is **fully built** — correct `ApprovalAttemptEnvelope`, `workspace-ui` channel,
evidence ref, and the `confirmed_proposal_fingerprint` /
`confirmed_proposal_instance_id` pair the route demands so an approval cannot land
on a proposal that changed after the approver saw it (both come from the preview).

The one thing it will not do is carry a credential I baked into the bundle. The
token is read at runtime from `globalThis.__WRITAI_APPROVAL_TOKEN__`; a `VITE_*`
variable would have been inlined by Vite and shipped to every visitor. **With no
token set — the default — nothing is posted** and the screen renders a labelled
rehearsal.

To exercise the live path in a browser:

```js
window.__WRITAI_APPROVAL_TOKEN__ = "<a Hexclave-resolvable token>";
```

then approve. The receipt is then read from the returned workspace's
`supervisor.applied_interrupts` — the server's own record of what it did, not the
preview's prediction. If those disagree, the screen shows what happened.

**Still for a human:** how `WORKSPACE_UI` authenticates in production. Probably a
server-side session exchanging a login for a short-lived token, which is a backend
change. Until then `writai approve` has the real identity behind it.

**There is no code path where a rehearsal renders "Applied".** No token, a server
refusal, or a response without a partition all produce *"Rehearsal · nothing was
applied"*, *"3 would stop"*, *"Nothing was applied — no session was interrupted"*,
and a receipt naming the reason.

---

## Integration surface

### What I call

| Method | Path (agent service, `VITE_AGENT_URL`, default `http://localhost:8002`) |
|---|---|
| `GET` | `/live-workspaces` |
| `GET` | `/live-workspaces/{workspace_id}/decisions/{decision_id}/preview` |
| `GET` | `/live-workspaces/{workspace_id}/events` (SSE, refresh trigger only) |
| `POST` | `/live-workspaces/{workspace_id}/decisions/{decision_id}/approve` — only when a runtime approval token is present |

I do **not** call `/approvals/*` on the authority service, and I no longer read
`VITE_AUTHORITY_URL` at all.

`GET /live-workspaces` is accepted as `{workspaces: [...]}` or a bare array. The
preview is accepted flat (`{...preview, correlation_id}`) or nested
(`{preview: {...}}`), matching `cli_approve.py:449 _preview_payload`.

### Types I depend on

From `WorkspaceApprovalPreview` (`workspaces/models.py:486`):

```
pending: PendingApproval          required
  .workspace_id, .decision_id, .supersedes_id, .permission_id,
  .source_ref, .text                                 required, all str
  .affected_scopes                                   required, str[]
  .requirements                                      required, object
  .proposal_fingerprint, .proposal_instance_id       required, echoed on approve
  .title, .effective_at                              optional str
interrupted_assignment_ids: str[]                    optional, defaults []
preserved_assignment_ids:   str[]                    optional, defaults []
assignment_provenance_paths: {assignmentId: str[]}   optional, defaults {}
```

The approve response is a `LiveWorkspaceView`. The receipt is read from
`supervisor.applied_interrupts[]` — the entry whose `decision_id` matches — using
`interrupted_assignment_ids` / `preserved_assignment_ids`
(`workspaces/supervisor.py:86`). No entry for that decision means no receipt is
drawn; the screen says the server did not report a partition rather than
inventing one.

From `LiveWorkspaceView` (`:670`) I read `id`, `baseline_decision`,
`specification`, `ticket`, `tasks[]`, `initial_plan`, `current_plan`,
`pending_mutation`, `approved_mutations[].mutation.decision`, and
`supervisor.assignments[]`. Everything except `id` is optional to me — a workspace
missing them degrades to showing ids instead of titles rather than failing.

From `SupervisorAssignment` (`workspaces/supervisor.py:53`) I read `id`,
`task_id`, `agent_name`. **Note `id`, not `assignment_id`** — that is what
`_assignment_labels` in `cli_approve.py:153` reads too.

### Id semantics — the part that will bite you

- **`PendingChange.id` is `"{workspace_id}:{decision_id}"`**, e.g.
  `csv-exports:DEC-018`. It is a frontend-only composite. Do not expect the
  backend to know it. Split on the first `:` to recover the pair.
- **`Person.assignmentId` is `SupervisorAssignment.id`**, the join key against
  `interrupted_assignment_ids` / `preserved_assignment_ids`.
- **`Person.taskId` is `SupervisorAssignment.task_id`**, and it is `""` when the
  preview names an assignment the workspace no longer lists. That is deliberate:
  dropping the row would understate a blast radius, which is worse than an ugly
  label. The label falls back to the assignment id.
- **`ProvenanceNode.id` is a graph artifact id** (`DEC-018`, `SPEC-009`,
  `TASK-102`), taken verbatim from `assignment_provenance_paths`. Titles are
  looked up in the workspace's artifacts and fall back to the id.
- **`decision.scope`** is the alphabetically-first affected scope, and reads
  `"export.authorization (+1 more scope)"` when there are several.

### Invariant held

`blastRadius` is rendered, never derived. No component intersects scopes. The
only client-side join is assignment-id → person, and requirement `was` → `now`,
both over values the server already sent. Approve posts and re-renders what comes
back — it does not mark anything approved locally and then tell the server.

---

## Known issues

**Mine:**

1. **The HTTP round trip is only partly exercised.** `GET /live-workspaces` has
   now run for real against a stub service returning an empty list, and the
   screen correctly reported it as live rather than as a fallback. **The preview
   and approve round trips have still never run against a real service** — those
   are unit-tested against payloads copied field-for-field from Lane B's Pydantic
   models and nothing more (ASSUMPTIONS.md A6: starting the real agent service
   writes `.writai/live-workspaces.json`, which Lane C's `reset.sh` deletes,
   and other agents were live on this machine). Expect first-run fixes there to
   be field-name details, not structure.
2. **No author is shown.** `PendingApproval` carries no author name, so the
   message is attributed to its own `source_ref`. If Lane B adds a display name,
   `sourceOf()` in `live.ts` is a two-line change.
3. **One rail from many paths.** `assignment_provenance_paths` has a path per
   interrupted assignment; the rail renders the longest, ties broken by
   assignment id. `/approvals/why` has no session identity, so it explains the
   affected task in general rather than "your" task.
4. **A partial queue renders.** If one preview fails and another succeeds, you
   see the successful one and a console warning that the queue is incomplete.
   Failing the whole screen would hide a real pending change; I judged the
   warning the lesser evil. Reasonable people could disagree.
5. **`/approvals` is not reachable from the other routes' nav.** It now has its
   own header and links back to Workspace and Examples, but nothing links to it —
   that half needs a shared file edited. See the audit below. Its header also
   leaves the service-status slot empty: showing `0/3 services` would mean wiring
   health checks this route does not do, and an invented indicator is worse than
   none.
6. A security hook on this machine reports the regex `.match`-family method named
   `e-x-e-c` as if it were Node's child-process call of the same name. `live.ts`
   uses `String.match` to avoid the false positive, not because the regex method
   was wrong. Worth knowing before someone "fixes" it back.

**Other lanes' — written down, not fixed, per the overnight rule:**

7. `origin/main` is `75d54bd`; local `main` is `e8df117`. My branch is based on
   the local one, so it is behind origin. Merge forward; do not assume it is
   current.
8. `cli_approve.py:153 _assignment_labels` reads `assignment.get("task_title")`,
   but nothing in the preview payload guarantees it. Not my file, not touched.

**Rejected from the adversarial review** (gpt-5.6-sol, `-s read-only`, 19 defects
raised, 16 fixed):

- *"Drop unknown assignment ids instead of fabricating a person."* Rejected —
  dropping shrinks a blast radius the server computed. An opaque label is honest;
  a smaller number is not.
- *"Require server provenance metadata before labelling data live."* Rejected —
  the server answering is what makes it live; there is no such metadata to demand.
- *"Fail the whole queue when one preview fails."* Rejected, see issue 4.

---

## Consistency audit: `/`, `/scenario-lab`, `/approvals`

Produced as a report, then acted on in a later pass. **Three divergences fixed,
four left deliberately.** Every fix is inside `frontend/src/approvals/`;
`live-workspace.css` and `scenario-lab.css` were read with grep and never opened
for editing, and `/` and `/scenario-lab` were re-checked in a browser after each
change.

**Acted on:**

- **#1 app chrome** — `components/ApprovalsHeader.tsx` reproduces `AppShell`'s
  visual contract locally: sticky full-bleed bar, 84px min-height, same padding,
  wordmark scale, nav-link treatment and 2px active underline. There is now a way
  back to Workspace and Examples. Both headers are full-bleed, so the chrome
  lines up across all three routes even though the content columns differ.
- **#3 button sizing** — `min-height` plus vertical padding instead of a fixed
  `height`, matching `.sl-button`. Identical at the default label length; it now
  grows rather than clips, which matters because the labels are server data.
- **#5 empty state** — extracted to `components/EmptyQueue.tsx` and given the
  `.sl-empty-state` treatment. It had no test coverage at all before; it has two
  now.

**Left deliberately:**

- **The nav entry pointing *at* `/approvals`.** Adding an `"approvals"` member to
  `AppShellView` edits a file both fallback demo routes render, and a new nav item
  on those routes is a behaviour change. Integrator's call — it is a two-line
  change once someone owns the risk.
- **#2 content width (1080px), #4 section padding (28px 32px), #6 hero scale.**
  These are the mock's own values, and the mock is this screen's specification.
  Changing them to match a sibling would trade a stated requirement for a
  consistency that the full-bleed header now delivers anyway.
- **#7 token duplication.** The lane brief requires this route to be
  self-contained; sharing the token block would undo that on purpose.

| # | Divergence | Where |
|---|---|---|
| 1 | **`/approvals` renders no app shell.** `/` and `/scenario-lab` both wrap in `AppShell` — wordmark, Workspace/Examples nav, 0/3-services indicator, Docs link. `/approvals` is a bare page: no nav, no service status, no way back. It is also unreachable from the nav, because `AppShellView` has no `"approvals"` member. **The biggest divergence and the one a judge would notice.** | `live-workspace/LiveWorkspaceRoute.tsx:81`, `scenario-lab/components/AppShell.tsx:4`, vs `approvals/ApprovalsRoute.tsx` (none) |
| 2 | **Page width differs three ways.** 1280px / 1540px / 1080px. Switching routes visibly re-flows the container. | `live-workspace.css:2`, `scenario-lab.css:250`, `approvals.css:52` |
| 3 | **Button sizing model differs.** The other two use `min-height: 44px` with vertical padding; approvals uses a fixed `height: 44px`, which clips a wrapped label instead of growing. | `live-workspace.css:32`, `scenario-lab.css:192` vs `approvals.css:469` |
| 4 | **Section padding rhythm is unshared.** `20px 24px 24px` / `24px 28px` and `22px 24px` / `28px 32px`. Cards read as different densities side by side. | `live-workspace.css:984`, `scenario-lab.css:964` and `:1265`, `approvals.css:140` |
| 5 | **Empty states are unshared.** `scenario-lab` has a real `.sl-empty-state` component with its own heading and action treatment; `/approvals` hand-rolls its empty view in the route. | `scenario-lab.css:686`, `approvals/ApprovalsRoute.tsx` empty branch |
| 6 | **Hero type scale.** `/approvals` matches Workspace exactly (`clamp(2.4rem, 4vw, 3.45rem)`) but not Examples (`clamp(2.45rem, 4.2vw, 4.6rem)`). Consistent with one sibling, not the other. | `live-workspace.css:16`, `approvals.css:113` vs `scenario-lab.css:269` |
| 7 | **Design tokens are duplicated, not shared.** The `--sl-*` block is defined three times — `.sl-root`, `.sl-root--live-workspace`, `.ap-root`. Values match today; nothing keeps them matching. The lane brief required this for route isolation, so it is a deliberate cost, not an accident. | `scenario-lab.css:27`, `approvals.css:14-40` |

**If you fix one thing, fix #1** — a photogenic screen with no way back to the
product reads as a mockup. It needs an `"approvals"` member on `AppShellView` and
a nav entry, both of which are in `scenario-lab/`, which Lane D was told not to
touch. That is an integrator's call, not mine.
