# Stage runbook — five-session deny

Cold machine to the deny landing. Read top to bottom, one command per step.

---

## 0. Prerequisites

`.venv` must exist at the repo root and be **python3.12**. System `python3` is 3.9 and will not work.

```
cd /Users/ranjivj/DragBack
```

```
.venv/bin/python --version
```

Expect `Python 3.12.x`. If `.venv` is missing, stop and build it first.

Every command below sets `PYTHONPATH=backend`. Omitting it fails on `import dragback`.

---

## 1. Seed

Deletes and rebuilds `/tmp/dragback-stage/` — store, hook copy, and the five session dirs.

```
DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1 \
  PYTHONPATH=backend .venv/bin/python scripts/demo/seed.py
```

It prints the five directories, marked `survives` (Sara TASK-201, Alex TASK-202) or `interrupted` (Priya TASK-203, Marcus TASK-204, Dan TASK-205).

---

## 2. Start the server

Leave this terminal open. It re-seeds, then serves on `127.0.0.1:8002`.

```
DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1 \
  PYTHONPATH=backend .venv/bin/python scripts/demo/seed.py --serve
```

Wait for the line `agent service listening on http://127.0.0.1:8002`.

If port 8002 is already bound by another demo process, kill it first:

```
lsof -tiTCP:8002 -sTCP:LISTEN | xargs kill
```

---

## 3. Verify before you touch Claude Code

The session routes authenticate the hook and fail closed without a key, so both
calls carry the demo key the seeder set.

```
curl -s -X POST http://127.0.0.1:8002/supervisor/sessions/start -H 'Content-Type: application/json' -H 'X-Dragback-Hook-API-Key: dragback-demo-hook-key' -d '{"session_id":"sess-priya","cwd":"/tmp/dragback-stage/priya","branch":""}'
```

```
curl -s -X POST http://127.0.0.1:8002/supervisor/sessions/sess-priya/check -H 'Content-Type: application/json' -H 'X-Dragback-Hook-API-Key: dragback-demo-hook-key' -d '{"session_id":"sess-priya","tool_name":"Edit","timestamp":"2026-07-25T09:00:00+00:00"}'
```

Expect `"decision":"deny"` with `"decision_snapshot":"graph-v18"` and a non-null `redirect_instruction`.

**This curl consumes the deny-once on `sess-priya`.** Re-seed (step 5) before the real run, or skip this check.

---

## 4. Launch the five sessions

One terminal each. `PROMPT.txt` is the starter prompt. `.claude/settings.local.json` in each dir wires
SessionStart, PreToolUse and SessionEnd to the repo's `hooks/dragback_*.py`, with the
endpoint and API key inlined so the directory targets the server you seeded for.

```
cd /tmp/dragback-stage/sara && claude "$(cat PROMPT.txt)"
```

```
cd /tmp/dragback-stage/alex && claude "$(cat PROMPT.txt)"
```

```
cd /tmp/dragback-stage/priya && claude "$(cat PROMPT.txt)"
```

```
cd /tmp/dragback-stage/marcus && claude "$(cat PROMPT.txt)"
```

```
cd /tmp/dragback-stage/dan && claude "$(cat PROMPT.txt)"
```

Sara and Alex run to completion. Priya, Marcus and Dan hit the deny on their first tool call and adopt the admin-only redirect.

---

## 5. RE-SEED BETWEEN EVERY RUN — this is what breaks the second rehearsal

Deny-once is **per assignment**. Once an assignment has delivered its redirect, every later check on it returns `allow`. A second rehearsal against a used store silently allows all five sessions and looks like the product is broken.

Between runs, in the server terminal: `Ctrl-C`, then

```
DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1 \
  PYTHONPATH=backend .venv/bin/python scripts/demo/seed.py --serve
```

Or, leaving the server up, re-seed the store from a second terminal (the server re-reads the file per request):

```
DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1 \
  PYTHONPATH=backend .venv/bin/python scripts/demo/seed.py
```

Also close and relaunch the five `claude` sessions — a session that already saw the deny will not see it again.

---

## 6. If it does not deny

- **The deny lands on the FIRST tool call, whichever tool that is.** Usually `Read`, not `Edit`. If the session read the file before you were watching, the deny already fired — check the scrollback.
- **Deny-once is per assignment, so a smoke test consumes it.** The step 3 curl burns `TASK-203`. Re-seed before the real run.
- **The server must be running** on 8002. Confirm: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8002/openapi.json` → `200`. The hook fails closed, so a dead server denies with `Dragback hook failed closed:` instead of the DRAGBACK block — that is a different failure.
- **The session dir must contain `.dragback/task`.** Without it the session binds to
  nothing, reads as unbound, and an unbound session is **allowed everything** — which
  looks identical to a working demo. Confirm: `cat .dragback/task` in the session dir.
- **Wrong port.** The endpoint is baked into each `.claude/settings.local.json` when you
  seed. If you started the server with `--port N`, you must have seeded with `--port N`
  too, or every session silently registers nowhere.
- **You must launch from inside the session dir.** `.claude/settings.local.json` is per-directory; running `claude` from elsewhere loads no hook.
- **Wrong python.** `PYTHONPATH=backend .venv/bin/python`, never `python3`.
- Survivors denying, or interrupted sessions allowing, means a stale store. Re-seed.

---

## What is NOT live — do not claim otherwise on stage

- **No real Composio delivery or Hexclave-authenticated approval has exercised
  the Slack route.** Live Gemini extraction now reaches a `PENDING` proposal in
  direct rehearsal: exact quotes are resolved to offsets in Python, requirements
  are mandatory, and the workspace supplies the trusted scope vocabulary.
  Deterministic validation still refuses fabricated evidence or unknown scopes,
  and the proposal remains `human_reviewed=false`. The staged demo still fires
  through `scripts/demo/fire.sh`, which uses the seeded change fixture and the
  explicit delta — no extraction in that path.
- **No Composio webhook has ever been delivered here.** Message text is supplied
  by hand. `COMPOSIO_WEBHOOK_SECRET` is empty, so no signed delivery can be
  verified.
- **Approvals are not authenticated locally.** `HEXCLAVE_TEAM_ID` is empty, so
  `dragback approve change` correctly fails with
  `APPROVAL_AUTHENTICATION_FAILED` and `fire.sh` falls back to the in-process
  seam, which needs `DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1`. Every authority
  check still runs; only the channel authentication is bypassed.
- **The CrustData payload is documentation-reconstructed, not captured.**

## Facts worth having on hand

- Store: `/tmp/dragback-stage/live-workspaces.json`
- Hooks: `hooks/dragback_session_start.py`, `dragback_pre_tool_use.py`, `dragback_session_end.py`
- Hook auth header: `X-Dragback-Hook-API-Key`, demo value `dragback-demo-hook-key`
  (export `DRAGBACK_HOOK_API_KEY` before seeding to override; a real key is then
  inherited from your shell rather than written into the settings files)
- Agent service `127.0.0.1:8002`, authority service `127.0.0.1:8001`
- Decision `DEC-018` supersedes `DEC-004`, scope `export.authorization`
- Provenance: `DEC-018 → DEC-004 → SPEC-009 → TICKET-100 → TASK-203 → PLAN-027`
- The seeder never writes to `~/.claude/settings.json`, and hook config is never
  committed to `.claude/settings.json`.
- `dragback` is **not on PATH** inside a stage session. An interrupted session that
  tries `dragback dev why` gets "command not found". Run it from the repo root as
  `.venv/bin/dragback dev why`, or `PYTHONPATH=backend .venv/bin/python -m
  dragback.cli dev why`. (`GET /supervisor/sessions` exists now, so `dev status`,
  `dev why` and `dev ack` all work against the merged service — the note in
  HANDOFF.md saying otherwise predates that route.)
- **Two rehearsals in a row without a re-seed is the single most likely way to make
  a working demo look broken.** Step 5 exists for that reason.
