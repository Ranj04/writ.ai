# Stage runbook — five-session deny

Cold machine to the deny landing. Read top to bottom, one command per step.

---

## 0. Prerequisites

**There is exactly one tree. It is `/Users/ranjivj/writai-verify`.**

```
cd /Users/ranjivj/writai-verify && .venv/bin/python --version && .venv/bin/writai --help >/dev/null && ls .env && git log --oneline -1
```

The other two checkouts on this machine were renamed so they cannot be entered
by accident: `DragBack-ARCHIVE` (pre-rename code, still holds the lane branches
— **do not delete it**) and `writ.ai-STALE` (had the `.env`, no venv, behind).
`/Users/ranjivj/DragBack` is now an empty directory containing only a signpost
back here.

All four checks must pass. What each one catches:

- **`Python 3.12.x`** — system `python3` is 3.9 and will not work.
- **`.venv/bin/writai`** — the console script, and proof the venv is real. Two
  separate versions of this went wrong:
  - the venv held an *editable* install of the old `dragback` package pointed at
    a **different directory**, so `dragback …` ran pre-rename code from the
    archive tree and looked completely normal doing it;
  - and `.venv` here was a **symlink into `/Users/ranjivj/DragBack/.venv`** — the
    canonical tree had no interpreter of its own and silently borrowed the
    archive's, which is what made the first problem possible.

  Both are fixed: `.venv` is now a real directory built by `python3.12 -m venv`
  with `pip install -e ".[dev]"`. If `ls -ld .venv` ever shows a symlink, or
  `.venv/bin/dragback` exists, stop and rebuild:
  ```
  rm .venv && python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
  ```
- **`.env`** — read from the **working directory**. Run step 1 from a tree
  without one and every integration reports `[ ---- ] not configured` and the
  command **exits 0** — a clean-looking preflight that proves nothing. If step 1
  says everything is absent, suspect the tree before you suspect the keys.

  (This was genuinely broken until tonight: `load_dotenv()` resolves relative to
  the *installed package*, so with the venv in one checkout and the operator in
  another it found no `.env` at all and reported six absent integrations while a
  populated `.env` sat in the cwd. It now searches the working directory first.)
- **the commit** — more than one checkout of this repo exists on this machine.
  Confirm you are on the one you mean to record.

Every command below sets `PYTHONPATH=backend`. Omitting it fails on `import
writai`. `.venv/bin/writai …` is equivalent once the install above is correct.

---

## 1. `writai doctor` — what is live, and what to do about what isn't

One command, before you touch anything else.

```
.venv/bin/writai doctor
```

Read it as: **the marks are the state, the `so:` lines are the plan.** Every
integration that is not `[ LIVE ]` prints a `so:` line naming the concrete
command or switch that gets you through the demo without it. You should not
need a second document.

| | Meaning | Do |
|---|---|---|
| `[ LIVE ]` | A real call proved the credential works | nothing |
| `[ DEAD ]` | Set, and broken. **Worse than absent** — the code takes the live path | follow its `so:` line |
| `[  ??  ]` | Could not be checked. **Not a pass** | treat as unknown |
| `[ ---- ]` | Not configured. A choice, not a fault | follow its `so:` line |

**Exit 1 means a dead credential, not a blocked demo.** At the last run: Gemini
`LIVE`; Hexclave and Composio `DEAD`; Callwright, CrustData and Superset
`----`. The five-session deny below depends on none of them — it runs on the
seeded fixture and the unauthenticated approval seam, which is what
`WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1` on every command below is for.

The two `DEAD` ones change what you may *claim*, not whether the demo runs.
Read "What is NOT live" at the bottom before you say anything on stage.

---

## 2. Seed

Deletes and rebuilds `/tmp/writai-stage/` — store, hook copy, and the five session dirs.

```
WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1 \
  PYTHONPATH=backend .venv/bin/python scripts/demo/seed.py
```

It prints the five directories, marked `survives` (Sara TASK-201, Alex TASK-202) or `interrupted` (Priya TASK-203, Marcus TASK-204, Dan TASK-205).

---

## 3. Start the server

Leave this terminal open. It re-seeds, then serves on `127.0.0.1:8002`.

```
WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1 \
  PYTHONPATH=backend .venv/bin/python scripts/demo/seed.py --serve
```

Wait for the line `agent service listening on http://127.0.0.1:8002`.

If port 8002 is already bound by another demo process, kill it first:

```
lsof -tiTCP:8002 -sTCP:LISTEN | xargs kill
```

---

## 4. Verify before you touch Claude Code

The session routes authenticate the hook and fail closed without a key, so both
calls carry the demo key the seeder set.

```
curl -s -X POST http://127.0.0.1:8002/supervisor/sessions/start -H 'Content-Type: application/json' -H 'X-writ.ai-Hook-API-Key: writai-demo-hook-key' -d '{"session_id":"sess-priya","cwd":"/tmp/writai-stage/priya","branch":""}'
```

```
curl -s -X POST http://127.0.0.1:8002/supervisor/sessions/sess-priya/check -H 'Content-Type: application/json' -H 'X-writ.ai-Hook-API-Key: writai-demo-hook-key' -d '{"session_id":"sess-priya","tool_name":"Edit","timestamp":"2026-07-25T09:00:00+00:00"}'
```

Expect, in the second response:

```
"decision": "deny"
"denial_mode": "until-acknowledged"
"binding_source": "task-file"
"redirect_instruction": "Exports are admin-only. …"     (non-null)
"provenance_path": ["DEC-018","DEC-004","SPEC-009","TICKET-100","TASK-203","PLAN-027"]
```

There is **no `decision_snapshot` field** in this response — an earlier version of
this runbook said to look for `"decision_snapshot":"graph-v18"`, and an operator
who went looking for it would have concluded a working demo was broken. The
snapshot is on the assignment, not the verdict; `writai dev why <session>` shows
the graph transition.

**This curl consumes nothing, and you can run it as often as you like.** A bare
curl never acknowledges: the deny is held open **until acknowledged**, and the
acknowledgement is a `redirect_id` the service issued on a previous deny and the
*hook* echoes back on its next call. Curl doesn't echo it, so curl keeps getting
`deny` — which is the correct answer, not a stuck session. (Older text here said
the deny was one-shot and that this curl "burned" TASK-203. It is not, and it
does not.)

To watch the whole beat by hand, echo the id back the way the hook does:

```
curl -s -X POST http://127.0.0.1:8002/supervisor/sessions/sess-priya/check -H 'Content-Type: application/json' -H 'X-writ.ai-Hook-API-Key: writai-demo-hook-key' -d '{"session_id":"sess-priya","tool_name":"Edit","timestamp":"2026-07-25T09:00:00+00:00","acknowledged_redirect_id":"<redirect_id from the deny>"}'
```

That returns `"decision":"allow"`. Denied until acknowledged, then released — no
second round trip, no human in the loop for a redirect the agent already saw.

---

## 5. Launch the five sessions

One terminal each. `PROMPT.txt` is the starter prompt. `.claude/settings.local.json` in each dir wires
SessionStart, PreToolUse and SessionEnd to the repo's `hooks/writai_*.py`, with the
endpoint and API key inlined so the directory targets the server you seeded for.

```
cd /tmp/writai-stage/sara && claude "$(cat PROMPT.txt)"
```

```
cd /tmp/writai-stage/alex && claude "$(cat PROMPT.txt)"
```

```
cd /tmp/writai-stage/priya && claude "$(cat PROMPT.txt)"
```

```
cd /tmp/writai-stage/marcus && claude "$(cat PROMPT.txt)"
```

```
cd /tmp/writai-stage/dan && claude "$(cat PROMPT.txt)"
```

Sara and Alex run to completion. Priya, Marcus and Dan hit the deny on their first tool call and adopt the admin-only redirect.

---

## 6. RE-SEED BETWEEN EVERY RUN — this is what breaks the second rehearsal

A session that has already **acknowledged** its redirect is allowed from then on,
and the acknowledgement is stored per assignment. So a second rehearsal against a
used store silently allows all five sessions and looks like the product is
broken.

(The mechanism, since it moved: the deny is held **until acknowledged**, not
delivered once. Every tool call is denied until the hook echoes back the
`redirect_id` the service issued — which it does automatically on the very next
call, so a live session sees exactly one deny and continues. The store remembers
that, which is why it has to be rebuilt between rehearsals.)

Between runs, in the server terminal: `Ctrl-C`, then

```
WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1 \
  PYTHONPATH=backend .venv/bin/python scripts/demo/seed.py --serve
```

Or, leaving the server up, re-seed the store from a second terminal (the server re-reads the file per request):

```
WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1 \
  PYTHONPATH=backend .venv/bin/python scripts/demo/seed.py
```

Also close and relaunch the five `claude` sessions — a session that already saw the deny will not see it again.

---

## 7. If it does not deny

- **The deny lands on the FIRST tool call, whichever tool that is.** Usually `Read`, not `Edit`. If the session read the file before you were watching, the deny already fired — check the scrollback.
- **A previously acknowledged assignment stays allowed.** The step 4 curl does
  *not* cause this — it never acknowledges — but a completed rehearsal does.
  Re-seed between runs.
- **The server must be running** on 8002. Confirm: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8002/openapi.json` → `200`. The hook fails closed, so a dead server denies with `writ.ai hook failed closed:` instead of the WRITAI block — that is a different failure.
- **The session dir must contain `.writai/task`.** Without it the session binds to
  nothing, reads as unbound, and an unbound session is **allowed everything** — which
  looks identical to a working demo. Confirm: `cat .writai/task` in the session dir.
- **Wrong port.** The endpoint is baked into each `.claude/settings.local.json` when you
  seed. If you started the server with `--port N`, you must have seeded with `--port N`
  too, or every session silently registers nowhere.
- **You must launch from inside the session dir.** `.claude/settings.local.json` is per-directory; running `claude` from elsewhere loads no hook.
- **Wrong python.** `PYTHONPATH=backend .venv/bin/python`, never `python3`.
- Survivors denying, or interrupted sessions allowing, means a stale store. Re-seed.

---

## What is NOT live — do not claim otherwise on stage

- **Slack extraction now works, and its wording is not reproducible.** Measured
  over 14 live runs on the demo's own sentence: 14/14 produced a valid proposal,
  and 6/6 driven through approval reached a **graph write** with the correct
  blast radius — `graph-v17 → graph-v18`, TASK-203/204/205 invalidated,
  TASK-201/202 preserved, every time.

  **But all 14 runs invented a different requirement shape**, and none used the
  workspace's own `audience` key. The baseline says
  `{"audience": "all_users"}`; extraction writes `{"allowed_roles":
  ["administrators"], "format": "CSV"}`, `{"restricted_to":
  "administrators_only"}`, and twelve other variants. **The scope-level verdict
  is stable and correct; the requirement text handed to a redirected agent is
  not.** Do not promise a judge that the redirect wording is deterministic.

  The staged demo does not depend on any of this: `scripts/demo/fire.sh` fires
  the seeded change fixture through the explicit delta, with no extraction in
  the path. Show extraction as its own beat, not as the demo's spine.
- **No real Composio delivery or Hexclave-authenticated approval has exercised
  the Slack route.** The measurement above bypassed channel authentication
  exactly as the seeder does. Every authority check ran; nobody proved who the
  approver was.
- **No Composio webhook has ever been delivered here.** Message text is supplied
  by hand. `COMPOSIO_WEBHOOK_SECRET` is empty, so no signed delivery can be
  verified.
- **Approvals are not authenticated locally.** `HEXCLAVE_TEAM_ID` is empty, so
  `writai approve change` correctly fails with
  `APPROVAL_AUTHENTICATION_FAILED` and `fire.sh` falls back to the in-process
  seam, which needs `WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1`. Every authority
  check still runs; only the channel authentication is bypassed.

  **Browser sign-in exists now and is OFF by default. Leave it off.** `/approvals`
  reads a Hexclave identity only when `VITE_WRITAI_HEXCLAVE_SIGN_IN=1`. Setting
  it against the current project — which has a valid key but **zero teams** —
  hangs the browser tab: not a slow load, an unresponsive tab that stops
  answering clicks and navigation. `/approvals` is the screen you fall back TO
  when the live path breaks, so it must never be the thing that breaks. With the
  flag off it renders, approves on the unauthenticated seam, and labels itself
  `Rehearsal · nothing was applied` — verified by loading it, not by reasoning
  about it. Turn sign-in on only after `writai doctor hexclave` reads `LIVE`, and
  re-load `/approvals` before relying on it in front of anyone.
- **The CrustData payload is documentation-reconstructed, not captured.**

## Facts worth having on hand

- Store: `/tmp/writai-stage/live-workspaces.json`
- Hooks: `hooks/writai_session_start.py`, `writai_pre_tool_use.py`, `writai_session_end.py`
- Hook auth header: `X-writ.ai-Hook-API-Key`, demo value `writai-demo-hook-key`
  (export `WRITAI_HOOK_API_KEY` before seeding to override; a real key is then
  inherited from your shell rather than written into the settings files)
- Agent service `127.0.0.1:8002`, authority service `127.0.0.1:8001`
- Decision `DEC-018` supersedes `DEC-004`, scope `export.authorization`
- Provenance: `DEC-018 → DEC-004 → SPEC-009 → TICKET-100 → TASK-203 → PLAN-027`
- The seeder never writes to `~/.claude/settings.json`, and hook config is never
  committed to `.claude/settings.json`.
- `writai` is **not on PATH** inside a stage session. An interrupted session that
  tries `writai dev why` gets "command not found". Run it from the repo root as
  `.venv/bin/writai dev why`, or `PYTHONPATH=backend .venv/bin/python -m
  writai.cli dev why`. Both are verified working; `dev status` and `dev why`
  render correctly against the running service.
- `writai dev ack` releases only a session the service reports as **blocked and
  awaiting a human** — an assignment invalidated outright with no corrected plan
  to hand over. A session that is merely denied-until-acknowledged is not that:
  its hook acknowledges automatically on the next tool call, and `dev ack`
  correctly answers `DECISION_ID_UNRESOLVED`. That is not a bug; do not go
  looking for one on stage.
- **Known cosmetic defect, `scripts/demo/ack.sh`:** its blocked-session line
  renders as `blocked by yesnonointerruptedgraph-v17graph-v18` — several fields
  concatenated with no separators — and it then reports `[FAIL]` for a session
  `dev ack` considers not blocked. The demo does not need this script (the hook
  self-acknowledges), so **do not run it on stage.** Logged, not fixed.
- **Two rehearsals in a row without a re-seed is the single most likely way to make
  a working demo look broken.** Step 6 exists for that reason.
