# Lane D — Approval screen (fourth agent, frontend only)

You are building one screen in the existing React app. Three other agents are editing this repository right now. **You touch no backend code, no scripts, and no other frontend feature.**

---

## Hard isolation rules — read twice

**You own one new directory: `frontend/src/approvals/`.** Everything you create lives there.

**You may make exactly two surgical edits outside it, and nothing else:**

1. `frontend/src/App.tsx` — add `"approvals"` to the `WritaiRoute` union, one `pathname.startsWith("/approvals")` branch in `routeForPath`, and one branch in the component. The file is 17 lines. Re-read it immediately before editing in case someone else touched it, and keep the diff to those three lines.
2. `frontend/src/App.test.ts` — add the matching `routeForPath` case.

**Do not open or edit any of these.** Other agents own them or a collision would be expensive:

```
backend/                      scripts/
frontend/src/live-workspace/  frontend/src/scenario-lab/
frontend/src/styles.css       frontend/src/api.ts
frontend/src/types.ts         package.json   vite.config.*
Makefile  cli.py  config.py  AGENTS.md  CLAUDE.md  .env*
```

**Add no dependencies.** React is already there, there is no router library and you do not need one, and there is no chart library because you are not building charts.

**Work in your own worktree.** `frontend/node_modules` is gitignored and will not carry over, so install once:

```bash
git worktree add ../db-ui -b lane-d-approvals
cd ../db-ui/frontend && npm install
```

---

## The visual spec already exists — match it

`docs/mocks/approval-screen.html` in this repo is a working, interactive mock of exactly the screen you are building. **Open it in a browser first.** Match its layout, spacing, type scale, colour usage and motion. It is the specification; do not redesign it.

Design tokens live on `.sl-root` in `scenario-lab.css`, which will not be loaded on your route. Do **not** import that file and do **not** edit it. Copy the token block into your own `approvals/approvals.css` so the route is self-contained, using the same variable names (`--sl-ink`, `--sl-muted`, `--sl-border`, `--sl-green`, `--sl-amber`, `--sl-faint`, `--sl-font-sans`, `--sl-font-mono`, `--sl-radius-panel`, `--sl-motion-fast`).

Three things from the mock that are load-bearing, not decoration:

- **The five faces are the largest element after the number.** On approve, three desaturate with a small pause badge, two gain a green ring. It has to be readable from across a room with the sound off.
- **The chain is a single vertical rail**, not a graph. On approve an amber gradient travels down it over ~820ms and the unaffected branch stays green. One animation, then it stops.
- **The Slack message renders verbatim** as a Slack message, with *"no ticket referenced · no one tagged"* beneath it.

---

## The backend endpoint does not exist yet

Lane B is building `writai approve` as a CLI command, not a web API. So build against a fixture and make wiring later a one-file change.

Create `approvals/fixtures.ts` with data in exactly this shape, and `approvals/api.ts` that fetches the real endpoint and **falls back to the fixture on any error or 404**. Log the fallback to the console so it is never silently mistaken for live data.

```ts
export type Person = { assignmentId: string; name: string; initials: string; taskId: string };

export type PendingChange = {
  id: string;
  source: { channel: string; author: string; authorInitials: string; timestamp: string; text: string };
  decision: { id: string; supersedes: string; scope: string; was: string; now: string };
  provenancePath: { id: string; title: string; detail: string; affected: boolean }[];
  blastRadius: { interrupted: Person[]; preserved: Person[] };
  approverPermission: string;
};
```

Assume `GET {AUTHORITY_URL}/approvals/pending` and `POST {AUTHORITY_URL}/approvals/{id}/approve`. Read the authority URL from `import.meta.env.VITE_AUTHORITY_URL` the same way the other routes do. If the real endpoints turn out to differ, adapt `api.ts` and nothing else.

---

## The invariant you must not break

**The browser decides nothing.** `blastRadius` arrives from the server, which computes it from `SupervisorInterruptPort.preview()`. Your component renders it. It must never derive who is affected by comparing scopes client-side — that is a permission decision, and permission decisions do not happen in a browser.

Likewise, approve **posts** and re-renders whatever comes back. It does not mark anything approved locally and then tell the server. The repo already states this discipline for the existing routes: *the browser does not traverse the graph, decide verdicts, sign grants, calculate pass results, or invent loop state.* It holds here.

---

## Deliverables, in priority order. Stop wherever time runs out.

1. **The route renders from fixture data and matches the mock.** `/approvals` shows the message, the delta, the chain, the big number, the five people, and the approve control. This alone is the whole deliverable if time is short.
2. **Approve works.** Posts if the endpoint exists, otherwise transitions local state, and plays the animation either way. Include the replay control from the mock — you will rehearse this repeatedly.
3. **A pending list** when more than one change is waiting. Skip entirely if there is only ever one.
4. **Live refresh** over the existing SSE stream rather than polling. Only if the endpoint exists.
5. **The developer view** at `/approvals/why` — the same components rearranged to answer "why did my agent change", with *work preserved* given equal weight to *work invalidated*.

---

## Before you report done

`npm run typecheck` and `npm run test` must both pass — the repo already has both scripts. Your route must not change the behaviour of `/` or `/scenario-lab`; the existing `App.test.ts` cases must still pass unchanged.

Report which deliverables shipped, the exact URL to open, whether it is running on fixture or live data, and anything in the assumed endpoint contract that needs Lane B to confirm.

---

## Addendum — shared tokens, and what you must not restyle

**The app is already visually unified.** `live-workspace.css` and `scenario-lab.css` both use the `.sl-root` wrapper and the same `--sl-*` token set; `live-workspace.css` defines no tokens of its own and consumes scenario-lab's. Your job is to join that system, not to build a parallel one.

**So do this instead of copying the token block:**

1. Create `frontend/src/tokens.css` containing the `--sl-*` variable block on `:root`, with values copied exactly from the `.sl-root` block at the top of `scenario-lab.css`.
2. Add one line to `frontend/src/main.tsx` — `import "./tokens.css";` — above the existing `import "./styles.css";`. That file is 238 bytes and no other lane touches it. This is your **third and final** permitted edit outside `approvals/`.
3. Your `approvals.css` **consumes** the tokens and never redefines them.

Leave the existing `.sl-root` block in `scenario-lab.css` exactly where it is. Duplicate identical values are harmless, and removing it risks the two working routes. A later cleanup can delete it once someone has time to verify.

**Do not edit `live-workspace.css` or `scenario-lab.css`. At all.** They are 126KB combined and they render the two demos that serve as the fallback if the rest of the build does not land. A cosmetic improvement is not worth risking them today.

**After your own screen works**, do a read-only consistency audit and report it — do not act on it. Open `/`, `/scenario-lab` and `/approvals` side by side and list concrete divergences with file and line: heading scale, button treatment, card borders and shadows, spacing rhythm, empty states. Deliver that as a list at the end of your report. It becomes the post-hackathon cleanup, and it is genuinely useful — but changing 126KB of stylesheet unattended, today, is not.
