# Stage runbook — five-session deny

Cold machine to the deny landing. The canonical flow is:

```text
reset → up → check → fire → status/why → ack
```

Run every command from the repository root. Do not use the old direct
`seed.py --serve` flow; the launcher owns service startup, fixture import,
session bindings, and reset safety.

---

## 0. Prerequisites

**There is exactly one stage tree. It is `/Users/ranjivj/writai-verify`.**

```bash
cd /Users/ranjivj/writai-verify &&
  .venv/bin/python --version &&
  .venv/bin/writai --help >/dev/null &&
  test -f .env &&
  git log --oneline -1
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

  ```bash
  rm .venv
  python3.12 -m venv .venv
  .venv/bin/pip install -e ".[dev]"
  ```
- **`.env`** — read from the **working directory**. Run `writai doctor` from a
  tree without one and every integration reports `[ ---- ] not configured` and
  the command **exits 0** — a clean-looking preflight that proves nothing. If
  the doctor says everything is absent, suspect the tree before the keys.
- **the commit** — more than one checkout of this repo exists on this machine.
  Confirm you are on the one you mean to record.

The launcher needs `curl` and a Python 3.11+ interpreter that can import
`fastapi` and `uvicorn`. It selects, in order:

1. `WRITAI_DEMO_PYTHON`, if set;
2. an executable repo `.venv/bin/python`, if present;
3. `python3`.

The canonical stage tree should use its repo-local `.venv`. To select a
different interpreter explicitly:

```bash
export WRITAI_DEMO_PYTHON=/absolute/path/to/python3.12
"$WRITAI_DEMO_PYTHON" -c 'import fastapi, uvicorn'
```

`claude` starts the agent sessions when available. `tmux`, `jq`,
`screencapture`, and Superset are optional; the launcher reports each
fallback.

If Hexclave approval identity is not configured, opt into the clearly labelled
local channel-authentication bypass before arming:

```bash
export WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1
```

This bypasses only the approval channel authentication. Role, permission,
scope, confidence, proposal binding, graph traversal, and snapshot enforcement
still run. Omit it when the Hexclave approval path is genuinely configured.

Record the sponsor-integration state before rehearsing:

```bash
writai doctor
writai --json doctor
```

The JSON flag is global and therefore precedes `doctor`. A non-live sponsor
path changes what may be claimed on stage; it does not block the fixture-driven
five-session proof.

---

## 1. Reset

```bash
scripts/demo/reset.sh
```

Reset stops launcher-owned sessions, recording, and services; clears generated
demo state; and removes only a demo root bearing the launcher's safety marker.
It preserves backup recordings unless passed `--recordings`.

Always reset before another rehearsal. Reusing fired assignments is the most
likely way to make a working demo appear inert.

---

## 2. Arm

```bash
scripts/demo/up.sh
```

`up.sh` starts the authority, agent, and executor services; imports and
authorizes the `graph-v17` workspace; writes the five task bindings and hook
settings; and launches the sessions when Claude Code is available. It does
**not** propose or approve `DEC-018`.

Expected shape:

- two `export.generation` sessions that survive;
- three `export.authorization` sessions that will be interrupted;
- agent service on `127.0.0.1:8002`;
- all assignments pinned to `graph-v17`.

For a dry preflight without starting Claude Code:

```bash
scripts/demo/up.sh --no-agents
```

If tmux panes show Claude Code's folder-trust prompt, accept it in every agent
pane before firing. Reset recreates those directories, so the prompt can return
on every rehearsal.

---

## 3. Check

```bash
scripts/demo/check.sh
```

Do not fire on red. The checklist catches the three silent demo-killers:

- a session with no task binding;
- an assignment snapshot that does not match the armed graph;
- an assignment whose interrupt has already been spent.

Warnings are disclosed fallbacks, not passes. In particular, a missing backup
recording or optional Superset workspace does not stop the deterministic proof.

Use this checklist instead of ad hoc session curls: it validates the launcher
contract without registering a stray rehearsal session.

---

## 4. Fire

```bash
scripts/demo/fire.sh
```

This is the only launcher script that mutates the graph. It proposes `DEC-018`,
shows the blast radius, and waits for the human confirmation before applying
the change.

Expected result:

```text
graph-v17 → graph-v18
TASK-201, TASK-202                 continue
TASK-203, TASK-204, TASK-205      interrupted
```

The next tool call in each affected session receives the denial. The exact tool
may be `Read`, `Edit`, or another call; the hook runs before every tool use.

---

## 5. Show status and one explanation

```bash
writai --agent-url http://127.0.0.1:8002 dev status
writai --agent-url http://127.0.0.1:8002 dev why <SESSION_ID>
```

`status` is the wide shot: three interrupted, two continuing. `why` is the
proof for one person: affected scope, provenance path, invalidated work,
preserved work, decision snapshot, and redirect reason.

If the `writai` console script is not installed, use the same interpreter that
passed preflight:

```bash
PYTHONPATH=backend "${WRITAI_DEMO_PYTHON:-python3}" -m writai.cli \
  --agent-url http://127.0.0.1:8002 dev status
PYTHONPATH=backend "${WRITAI_DEMO_PYTHON:-python3}" -m writai.cli \
  --agent-url http://127.0.0.1:8002 dev why <SESSION_ID>
```

---

## 6. Human acknowledgement

```bash
scripts/demo/ack.sh
```

The script reads the blocked sessions from the service, shows which decision
blocks each one, and asks a person to confirm. Only then are those sessions
released to correct their own work. Neither `up.sh` nor `fire.sh` acknowledges
on the user's behalf.

Finish by showing the agents change `all_users` to an admin-only audience while
the two generation tasks remain intact.

---

## 7. Between rehearsals

Run the canonical sequence again from the top:

```bash
scripts/demo/reset.sh
scripts/demo/up.sh
scripts/demo/check.sh
```

Close any old interactive Claude Code sessions. A prior session that already
saw a redirect is not a fresh rehearsal.

If the live run fails and a clean backup exists:

```bash
scripts/demo/fallback.sh
```

---

## Troubleshooting

- **Agent service is down:** `curl --fail
  http://127.0.0.1:8002/health`. Run `reset.sh`, then `up.sh`; do not kill an
  unknown process merely because it owns the port.
- **No interrupt:** run `check.sh`. Confirm the session launched from its
  generated directory and that the directory contains `.writai/task`.
- **All five stop:** confirm each task binding and scope. The two
  `export.generation` siblings must survive.
- **All five continue:** the workspace was probably already fired or the hooks
  were not installed in the generated session settings. Reset, re-arm, and
  check.
- **A blocked agent cannot write the fix:** that is the intended
  deny-until-human-acknowledgement state. Run `ack.sh` after showing `why`.
- **No live agent panes:** Claude Code or tmux may be absent. `up.sh` reports the
  fallback and still supports `--no-agents` for a deterministic service
  rehearsal.

---

## What is not live — do not claim otherwise

- The staged change comes from the checked-in explicit delta. It does not
  depend on LLM extraction.
- No genuine Composio webhook delivery or Hexclave-authenticated approval has
  exercised the local fallback flow when
  `WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1` is set.
- CrustData credentials have passed a real, read-only watcher-list request
  using `Authorization: Bearer …` and `x-api-version: 2025-11-01`. That
  validates the key only. There are zero watchers, no genuine callback has been
  captured, and the exact LinkedIn target and CrustData-person-to-Hexclave-user
  binding are unset.
- `make demo-crustdata-replay` is a deterministic,
  documentation-reconstructed fallback. It makes no CrustData API call,
  receives no callback, and is not evidence of live sponsor usage.
- Fixture-generated correction wording is a demo template. Deterministic code,
  not the model, decides authority, traversal, invalidation, and grant
  usability.

## Facts worth having on hand

- Store: `.writai/live-workspaces.json` unless
  `WRITAI_WORKSPACE_STORE` overrides it.
- Generated session directories: a Superset worktree, or
  `../writai-demo/session-N`.
- Logs and manifests: `scripts/demo/logs/` and `scripts/demo/state/`.
- Hooks: `hooks/writai_session_start.py`,
  `hooks/writai_pre_tool_use.py`, and `hooks/writai_session_end.py`.
- Agent, authority, and executor ports: `8002`, `8001`, and `8003`.
- Decision: `DEC-018` supersedes `DEC-004` for
  `export.authorization`.
- Example provenance:
  `DEC-018 → DEC-004 → SPEC-009 → TICKET-100 → TASK-203 → PLAN-027`.
- The launcher never writes `~/.claude/settings.json`.
