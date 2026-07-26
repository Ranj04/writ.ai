# Lane C — handoff

**Branch `lane-c-demo`, worktree `/Users/ranjivj/db-demo`, based on `main` (`e8df117`).**
Five commits, nothing pushed, nothing merged, nothing rebased. `main` is untouched.

You have not seen this session. Everything you need is below; `ASSUMPTIONS.md`
next to this file has the questions I could not ask you and what I assumed.

---

## 1. What this is

The stage machinery for the five-session demo — the scripts that get five Claude
Code sessions armed, prove they are armed, fire one approved decision change, and
put the machine back so you can do it again. Bash plus the Python standard
library. `tmux`, `jq` and `screencapture` are used when present and never
required.

Everything lives in **`scripts/demo/`**. Nothing else on this branch changes,
except `ASSUMPTIONS.md` and this file at the root.

---

## 2. Run it

```bash
cd /Users/ranjivj/db-demo            # or wherever this branch is checked out

scripts/demo/reset.sh                # ~1s. Between every rehearsal. Exits non-zero
                                     #   if it cannot guarantee a clean re-seed.
scripts/demo/up.sh                   # arms 5 sessions and STOPS. Fires nothing.
scripts/demo/check.sh                # must print READY before you go on stage
#   ... talk over the armed sessions ...
scripts/demo/fire.sh                 # the only script that mutates the graph
dragback dev status                  # 3 interrupted, 2 continuing
dragback dev why <session-id>        # the path from the decision to that task
scripts/demo/ack.sh                  # the human beat — releases blocked sessions
#   ... the three agents rewrite their own work to admin-only ...

scripts/demo/fallback.sh             # if the live run dies: newest recording, full screen
```

`up.sh --sessions N`, `up.sh --record`, `up.sh --no-agents`, `reset.sh --recordings`,
`fire.sh --yes`, `ack.sh --yes` all exist. `--help` on any of them.

### If you run it in THIS worktree rather than the main checkout

This worktree has no `.venv`, and it is based on `main`, which does not carry
Lane A's `hooks/` or `services/supervisor_check.py`. So:

```bash
export DRAGBACK_DEMO_PYTHON=/Users/ranjivj/DragBack/.venv/bin/python
export DRAGBACK_DEMO_ROOT=/Users/ranjivj/db-demo-stage     # optional
```

Without Lane A merged, `check.sh` correctly reports red for sessions and hooks —
nothing registers, because the endpoint the hook posts to does not exist on
`main`. That is the designed degradation, not a bug. **Merge Lane A first and the
same commands go green.**

---

## 3. What shipped

| Deliverable (docs/BUILD_LANE_C.md) | State |
|---|---|
| 1. `reset.sh` | **Shipped, verified.** ~1s, idempotent, re-seed guaranteed or non-zero exit. |
| 2. `up.sh` — arm then wait | **Shipped, verified.** Reads task ids from the seeded graph; nothing hardcoded. |
| 3. `prompts/session-1..5.txt` | **Shipped, verified.** Genuine export work — see §5. |
| 4. `check.sh` | **Shipped, verified.** Red only when something is actually wrong, and now red on all three silent demo-killers (see §4a). |
| 5. `fallback.sh` | **Shipped, executed.** Selection and the player invocation both run; the AppleScript is now bounded and detached after an unplayable file hung it. |
| 6. `up.sh --record` | **Shipped, executed — degradation branch.** `screencapture` needs one-time macOS Screen Recording permission; until it is granted it silently produces nothing, which `up.sh` detects and reports while arming everything else. Grant it once and the success branch runs. |
| 7. tmux layout | **Shipped, executed.** tmux 3.7b installed via brew; 6 panes verified — operator in pane 0 with the fire command typed but not run, five agent panes each in its own session directory, all doing real work. |
| extra: Superset worktrees | **Shipped, logic verified against a stub — never against the real CLI** (Superset is not installed here). Falls back to plain directories cleanly. See §4b. |
| extra: `fire.sh`, `ack.sh` | Shipped. Firing must be separate from arming; `ack.sh` is explained in §6. |

Proven end to end **earlier tonight in the integrated tree** (before this branch
existed, with Lane A's hooks present): five real Claude Code sessions registered
and bound, one approved change, **three denied with the full provenance path
`DEC-018 → DEC-004 → SPEC-009 → TICKET-100 → TASK-20x → PLAN-027`, two allowed
and still writing files.** After `ack.sh`, an interrupted agent rewrote its own
`AUDIENCE = "all_users"` to `administrators`, added the `is_admin` check,
rewrote its tests, and carried on — nobody typed into a session.

Proven **on this branch**, at the assignment level, twice in a row: `reset → up →
fire` interrupts exactly three and leaves two continuing, **both times**.

---

## 4. The two corrections you gave me

**Prompts must be doing genuine export work.** Done, and it is the difference
between the demo working and not. A model that receives a redirect which does not
match what it is doing calls it misrouted and refuses — correctly. So the two
`export.generation` sessions write a real CSV serialiser and a real streaming
writer, and the three `export.authorization` sessions genuinely decide the
audience: every file they produce carries `AUDIENCE = "all_users"` and the
visibility or authorization function implementing it. The objective and the
requirement are quoted into `NOTES.md` and the prompt **from the seeded graph, in
the graph's own words** (`Expose the export control to all users`,
`audience: all_users`) — the same words the deny payload uses. Every prompt still
ends with "check that the denial names your scope and your task; apply it if it
does, refuse it and stop if it does not", because refusing a misrouted redirect
is behaviour worth keeping.

**Deny-once is per assignment, so reset must fully re-seed.** Done, and hardened
past what I had:

- `reset.sh` resolves the workspace store from `DRAGBACK_WORKSPACE_STORE`, then
  the repo's `.env`, then the default — a store it does not know about is exactly
  how a partial reset happens.
- It names anything left in `.dragback/` that it does not recognise instead of
  deleting it blind or pretending the reset was total.
- It prints a **verified** line confirming the store is gone, and **exits
  non-zero** when it cannot guarantee that: external store left in place, a
  service still answering on its port, or the store still present.
- `up.sh` now **exits 1** rather than arming on top of an already-fired
  workspace, and also on an unreadable or unknown status.
- `check.sh` marks an already-fired workspace **red**, not amber.

---

## 4a. check.sh now hard-fails on the three silent killers

Each of these leaves a workspace that looks armed and then does nothing on
stage, which is indistinguishable from the product being broken. None is a
warning:

- **Any unbound session** — registered but matched to no assignment, so it is
  allowed everything and will never be interrupted. Checked for *every*
  registered session, including ones this launcher did not create.
- **Any snapshot mismatch** — an assignment pinned to a different graph version
  than the workspace is state that survived an earlier run.
- **Any assignment whose deny is already spent** — deny-once is per assignment,
  so anything already `interrupted`, `redirected`, `resumed` or `completed` will
  not be interrupted again; firing allows it straight through.

Verified: green before firing (`5 assignment(s) pinned to graph-v17 with an
unspent deny`), red on every assignment after.

**Restoring these found a real bug.** `reset.sh` probed `/health` 0.5s after
signalling the services. A uvicorn that has been signalled keeps answering for a
moment, so `up.sh` saw "already healthy", reused a dying process, and rehearsed
against the workspace still in *its* memory — while the store on disk was
already gone. That made rehearsals nondeterministic in exactly the way this lane
exists to prevent. `reset.sh` now waits (up to 10s per service) for the ports to
go quiet and only then reports them clear.

## 4b. Superset worktrees — wired, unverified against the real CLI

Grounded in the published CLI reference (docs.superset.sh/cli/cli-reference):

```
superset workspaces create --project <id> --name <name> --branch <branch> \
                           --base-branch <b> --local --json
superset workspaces delete <id...>
```

Configuration: `DRAGBACK_DEMO_SUPERSET_PROJECT` (required — `--project` has no
default), `DRAGBACK_DEMO_SUPERSET_BASE_BRANCH` (default `main`).

**What I verified**: detection and project gating, provisioning, JSON parsing,
the manifest carrying the real worktree path, session content landing inside the
worktree, and `reset.sh` removing workspaces through `superset workspaces delete`
rather than `rm -rf` — all against a **stub** on `PATH` that mimics the
documented contract. Superset is not installed on this machine and there is no
API key, so **the real CLI was never invoked**.

**What could still be wrong**: the `--json` field spellings. The reference
documents that the workspace object carries the id and worktree path but not
their names, and the CLI is in beta. `demo_api.py superset-parse` tries the
plausible spellings, then any string value that is an existing directory, and
**returns non-zero rather than guessing** — so a shape I got wrong falls back to
a plain directory instead of pointing a session at nothing. First real run:
watch for `superset could not provision session-N`.

`--agent`/`--prompt` are deliberately not passed. Superset would launch the
agent itself, bypassing this launcher's hook configuration and canned prompt.

## 5. Integration surface

**This lane exposes no Python API and imports no repo module.** It is a client of
services and of the `dragback` CLI. Precise contact points:

### Consumed — HTTP (agent service, default `http://127.0.0.1:8002`)

| Call | Used for | Failure handling |
|---|---|---|
| `GET /health` on 8001/8002/8003 | readiness; `{"status":"ok"}` required, `graph_version` shown if present | timeout 2s, retried to a 40s ceiling with a message on expiry |
| `GET /live-workspaces/{id}` | status, `graph_version`, `supervisor.assignments[]`, `tasks[]`, `baseline_decision.attributes.requirements`, `invalidation_report.invalidated_task_ids` | 404 → "not seeded"; unreachable → red line, never a crash |
| `GET /supervisor/sessions` | binding table; reads `session_id`, `task_id`, `assignment_id`, `source`, `cwd`, `decision_id` per entry, accepting either a bare binding or `{"binding": {...}}` | absent (pre-Lane-A) → red line, everything else still runs |

### Consumed — CLI

`dragback workspace import|approve-baseline|authorize|propose-change|approve-change`,
`dragback dev ack`. **`dragback approve --text "<message>"` from the spec does not
exist**; the real surface is `approve pending` / `approve change WS DEC --role R`
and it is a Lane B placeholder that exits 2 with `NOT_IMPLEMENTED`. `fire.sh`
uses the `workspace` commands, which work today. `check.sh` reports the
placeholder as a warning, so it goes green when Lane B lands.

### Id semantics this lane relies on

- **`assignment_id` is `ASSIGNMENT-<task_id>`** (`workspaces/supervisor.py:167`).
  Read from the API, never constructed here — the manifest carries whatever the
  service returned.
- **`task_id`** is the binding key. `up.sh` writes it verbatim into
  `<session-dir>/.dragback/task`; the `SessionStart` hook reports it as
  `task_file_task_id` and **the server resolves the binding** (`REPO_FILE`
  source). This lane never resolves a binding itself.
- **`workspace_id` is `csv-exports`**, fixed by the shipped fixture.
- **`DEC-018`** is the change decision; role `approve_compliance`. Baseline role
  is `approve_product`.
- Session directories are `session-1..N` in fixture task order, so `session-1` is
  always Sara/`TASK-201`. Prompts are matched by the task's rank in the **full**
  assignment list, so `--sessions 3` still gives `TASK-203` its own prompt.

### Files written outside this repo

`$DEMO_ROOT/session-N/` (default `<repo>/../dragback-demo`, override
`DRAGBACK_DEMO_ROOT`), each containing `.dragback/task`, `.claude/settings.json`
(hook config), `NOTES.md`, `prompt.txt`, `data/accounts.csv`, `progress.log`.

**Hook configuration rule:** that `.claude/settings.json` is generated at run time
**inside a session directory, outside every checkout**. Nothing is written to
`~/.claude/settings.json`, and this branch adds no `.claude/` path to any
repository. Verified.

### Internal record format — read this before editing the scripts

Rows between `demo_api.py` and the shell use the **ASCII unit separator (`\x1f`),
not tab**. Bash treats tab as IFS whitespace, so `IFS=$'\t' read -r a b c d`
collapses runs of tabs and shifts every field after an empty one. If you add a
column, add it to `demo_api.py`'s emitter **and** to every `IFS=$'\037' read -r`
in `up.sh`, `check.sh` and `ack.sh`. The manifest is 9 columns:
`index, task_id, assignment_id, agent_name, scopes, directory, prompt_index, title, requirement`.

---

## 6. Things you should know before you rehearse

1. **The demo needs the acknowledgement beat.** With the shipped fixture the
   three interrupted assignments have no corrected plan, so the verdict endpoint
   denies them on **every** call (rule 5) rather than once (rule 4). Without
   `ack.sh` an interrupted agent cannot even write the correction it has worked
   out — the hook that stopped the wrong work also stops the fix. One agent said
   so itself. If you want deny-once-then-continue with no human in the loop, a
   corrected plan has to be stored for the workspace first.
2. **Another lane wrote `scripts/demo/seed.py`** into the directory this lane
   owns, in the shared checkout. It is not on this branch and I did not touch it.
   It is a parallel implementation of the same stage: same `csv-exports`
   workspace, same ports, different store (`/tmp/dragback-stage`), and it applies
   `DEC-018` as part of seeding rather than keeping arming and firing separate.
   **Pick one.** Running both collides on ports and binds sessions to a different
   store than the operator seeded.
3. **My files also still exist in the shared checkout** at
   `/Users/ranjivj/DragBack/scripts/demo/`. I left them rather than deleting
   files out from under an agent working there. This branch is the authoritative
   copy — integrate from here, then delete the duplicates.
4. **First launch in a fresh directory** may show Claude Code's one-time
   folder-trust prompt in each pane. Accept once; paths are stable across resets.
5. **A `SessionStart` handoff hook in user settings lands in every demo session
   too.** One agent opened by reporting a document about unrelated work. Clear it
   before rehearsing.
6. **`reset.sh` stops any Dragback service on its ports**, including one another
   lane started. It refuses to kill a process whose command line is not a
   Dragback service. Ports are overridable (`DRAGBACK_DEMO_AUTHORITY_PORT` etc.)
   if two checkouts must rehearse at once; defaults are unchanged.

---

## 7. Known issues and things I did not fix

**Machine change made to execute deliverable 7:** `brew install tmux` (3.7b).
The scripts detect tmux and never require it — this was only so the layout path
could actually be run. Reverse with `brew uninstall tmux` if unwanted.

**Mine, unfixed:**

- `--record`'s success branch is still unexecuted: it needs Screen Recording
  permission granted once in System Settings › Privacy & Security. Until then
  `screencapture -v` produces nothing and `up.sh` reports it.
- In print mode (no tmux) a session's log only fills in when it finishes, so
  during a run the live evidence is `progress.log` in each session directory and
  `dragback dev status`. An interrupted print-mode session that gives up also
  releases its binding via `SessionEnd`, so run `dragback dev why` **before** it
  exits.
- `demo_api.py` uses a per-socket timeout, not a total deadline; a slow-drip
  response from a local service could hang a check. Localhost, judged low.
- PIDs are matched by number, not by start time, so a PID reused within seconds
  could in principle be signalled. Every kill path additionally verifies the
  command line for port-derived PIDs.
- The `.env` store parser is a simple literal grep and does not implement
  `python-dotenv` semantics (interpolation, `export` prefixes, quoting edge
  cases). If your `.env` uses those, check the store path by hand.

**Not mine — written down, not touched:**

- `backend/tests/test_hooks.py::test_worst_case_context_length_is_recorded` fails
  in the shared checkout: it asserts an exact context length (`8235 == 9496`)
  against Lane A's in-flight hook wording. Not on this branch. **This branch's
  suite is green: 282 passed, 2 skipped.**
- The deny payload quotes the decision text and task titles but never the new
  requirement value — `audience: admin_only` appears nowhere in it, so a
  corrected agent paraphrases (`administrators`). Correct in substance, not the
  graph's word. Lane A's `now_required` is where that belongs.

---

## 7a. Found by executing the last two deliverables

Executing the tmux path on a checkout without Lane A merged surfaced two defects
the background path had hidden. Both are fixed on this branch.

1. **A settings file naming a missing hook script bricks every session.** `up.sh`
   used to warn that `hooks/` was incomplete and then launch anyway, writing hook
   config that pointed at scripts which did not exist. Claude Code reports
   `PreToolUse:Read hook error … No such file or directory` on *every* tool call,
   and the agents correctly refused to work around it — five bricked panes. The
   cross-model review flagged this and I had declined it, reasoning that
   `BUILD_LANE_C.md` says degrade rather than exit. The reviewer was right and my
   reading was wrong: degrading means running *without enforcement*, not writing
   a broken hook config. Now the hooks block is omitted entirely when the scripts
   are absent, and the banner reads ARMED WITHOUT ENFORCEMENT.
   (Worth noting: the agents' refusal was exactly the behaviour the prompts ask
   for — each one checked whether the denial named its scope and task, found it
   did not, declined to work around it, and stopped.)
2. **`fallback.sh` could hang.** With a file QuickTime cannot open, the
   full-screen AppleScript waits behind a modal error dialog indefinitely. It is
   now bounded (`with timeout of 5 seconds`) and detached, so the script returns
   in ~0.25s regardless.

And one operational fact that is not a defect but will bite on stage:

3. **The folder-trust prompt returns every rehearsal.** `reset.sh` deletes the
   session directories and `up.sh` recreates them, so Claude Code asks to trust
   each one again — five panes sitting on a security prompt. A person must
   answer it; `up.sh` now prints a ready-made `tmux send-keys … Enter` one-liner
   that accepts all panes at once.

## 7b. Proposed Makefile targets — NOT applied

`docs/BUILD_LANE_C.md` forbids this lane from touching the Makefile. Here is the
diff for a human to apply if wanted:

```diff
-.PHONY: install demo test check authority agent executor frontend stack neo4j cli
+.PHONY: install demo test check authority agent executor frontend stack neo4j cli \
+	demo-reset demo-up demo-check demo-fire demo-ack demo-fallback
 
+demo-reset:
+	./scripts/demo/reset.sh
+
+demo-up:
+	./scripts/demo/up.sh
+
+demo-check:
+	./scripts/demo/check.sh
+
+demo-fire:
+	./scripts/demo/fire.sh
+
+demo-ack:
+	./scripts/demo/ack.sh
+
+demo-fallback:
+	./scripts/demo/fallback.sh
```

## 8. Verification commands

```bash
cd /Users/ranjivj/db-demo
PYTHONPATH=backend /Users/ranjivj/DragBack/.venv/bin/python -m pytest    # 282 passed, 2 skipped
for f in scripts/demo/*.sh; do bash -n "$f"; done                        # syntax
python3 -m py_compile scripts/demo/demo_api.py                           # 3.9-compatible

# The re-seed proof, end to end, twice:
export DRAGBACK_DEMO_PYTHON=/Users/ranjivj/DragBack/.venv/bin/python
scripts/demo/reset.sh && scripts/demo/up.sh --no-agents && scripts/demo/fire.sh --yes
python3 scripts/demo/demo_api.py assignments http://127.0.0.1:8002 csv-exports | tr '\037' ' '
scripts/demo/reset.sh && scripts/demo/up.sh --no-agents && scripts/demo/fire.sh --yes
python3 scripts/demo/demo_api.py assignments http://127.0.0.1:8002 csv-exports | tr '\037' ' '
scripts/demo/reset.sh
# Both runs: TASK-203/204/205 interrupted, TASK-201/202 continuing.
```

A cross-model adversarial review (`codex exec -m gpt-5.6-sol -s read-only`) was
run against the full branch diff and returned 22 findings. The confirmed ones are
fixed in commit `1402275`, each verified before and after; the rest are listed in
§7 with the reason they were judged not worth acting on. One finding I
deliberately declined: making missing hooks fatal in `up.sh`. `docs/BUILD_LANE_C.md`
is explicit that a script which exits because a component is not merged yet is
useless at 2am, so a missing hook stays a loud warning.
