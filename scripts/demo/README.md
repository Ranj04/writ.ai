# Demo launcher

Stage machinery for the five-session Dragback demo. Bash plus the Python
standard library; `tmux`, `jq` and `screencapture` are used when present and
never required.

> One person acknowledged the change. Five agents inherited it.

## On stage, in order

```bash
scripts/demo/reset.sh          # ~1s, safe to run twice, run it between rehearsals
scripts/demo/up.sh             # arms 5 sessions, then stops. Fires nothing.
scripts/demo/check.sh          # green/red readiness. Exits non-zero only on red.
#   ... talk over the armed sessions ...
scripts/demo/fire.sh           # the only script that changes the graph
dragback dev status            # 3 interrupted, 2 continuing
dragback dev why <session-id>  # the path from the decision to that person's task
scripts/demo/ack.sh            # the human beat: releases the blocked sessions
#   ... the three agents rewrite their own work to admin-only ...
```

If the live run dies:

```bash
scripts/demo/fallback.sh       # newest recording, full screen, one keystroke
```

## What each script does

| Script | Job |
|---|---|
| `reset.sh` | Stops sessions, recorder and services, removes the demo directories, clears `.dragback/` state. Idempotent. `--recordings` also deletes the backups. |
| `up.sh` | Starts the three services, seeds the workspace, creates one directory per session with its `.dragback/task` and hook config, launches a Claude Code session in each, and **stops**. `--sessions N`, `--record`, `--no-agents`. |
| `check.sh` | The readiness checklist. One short line per item. Hard-fails red on an unbound session, an assignment whose snapshot does not match the graph, and an assignment whose deny is already spent — the three states that make a rehearsal silently do nothing. |
| `fire.sh` | Proposes and approves `DEC-018`. `--yes` skips the confirmation. |
| `ack.sh` | Acknowledges the sessions the service reports as blocked, so they can correct their own work. Never called by `up.sh` or `fire.sh` — it is a person's decision. |
| `fallback.sh` | Plays the newest file in `recordings/`. Optional explicit path. |
| `demo_api.py` | Read-only helper: health, workspace facts, assignments, sessions, session plan, binding match. Never writes to a service. |
| `prompts/session-N.txt` | Canned starter prompts, identical every run. |

## Session isolation

If the **Superset** CLI is on `PATH` and `DRAGBACK_DEMO_SUPERSET_PROJECT` names a
project, each session gets its own Superset workspace — an isolated git worktree
on its own branch (`dragback-demo/session-N-TASK-2xx`), which is what makes five
parallel agents manageable on one machine. `reset.sh` removes them with
`superset workspaces delete`, never `rm -rf`: Superset owns those paths.

Requires the host server (`superset start`) and an authenticated CLI. If any of
that is missing — CLI absent, project unset, host down, create fails — the
session falls back to a plain directory, once, with the reason printed. The
fallback is the default and the well-tested path.

`--agent`/`--prompt` are deliberately not passed to Superset: it would launch the
agent itself and bypass this launcher's hook config and canned prompt.

## Where things live

- Session directories: a Superset worktree, or `../dragback-demo/session-N`,
  **outside the repository**
  so a demo agent does not inherit Dragback's own `CLAUDE.md`. Override with
  `DRAGBACK_DEMO_ROOT`. `reset.sh` only deletes a tree carrying the
  `.dragback-demo-root` marker `up.sh` wrote.
- Service logs and session transcripts: `scripts/demo/logs/`.
- Run state and the session manifest: `scripts/demo/state/`.
- Backups: `scripts/demo/recordings/` (git-ignored).

Nothing generated at run time is committed.

## Arming and firing are separate on purpose

`up.sh` never approves anything. It leaves five sessions running against
`graph-v17` and prints the fire command. Arm during the previous team's demo;
fire on cue. When `tmux` is present the fire command is *typed but not executed*
in the operator pane.

## What the prompts are for

Each prompt keeps its session making **frequent, small tool calls** — read a
file, write a file, append to `progress.log` — because the interruption only
lands at the next tool call. An agent that thinks silently for forty seconds
looks broken on stage. They use file tools only, never the shell.

**The work has to be the real work.** A model that receives a redirect which
does not match what it is actually doing will call it stale or misrouted and
decline to act — correctly. So the two `export.generation` sessions genuinely
write a CSV serialiser and a streaming writer, and the three
`export.authorization` sessions genuinely decide the audience: every file they
produce carries `AUDIENCE = "all_users"` and the visibility or authorization
function that implements it. When `DEC-018` lands, the correction is
unmistakably about the file in front of them.

The objective and the current requirement are quoted into `NOTES.md` and the
prompt **from the seeded graph**, in the graph's own words (`Expose the export
control to all users`, `audience: all_users`), because those are the words the
deny payload uses.

Every prompt ends with the same instruction: check that the denial names your
scope and your task; apply it if it does, refuse it if it does not. Refusing a
misrouted redirect is the behaviour we want to keep.

Prompts are matched to tasks by the task's position in the seeded graph, so
`--sessions 3` still gives `TASK-203` the prompt written for `TASK-203`.

## Honest limits

- **The tmux layout is executed and works**: one operator pane plus five agent
  panes, each in its own session directory. Without tmux, sessions run in
  `claude -p` print mode in the background and log to
  `scripts/demo/logs/session-N.log`; a print-mode log only fills in when the
  session finishes, so during a run the live evidence is `progress.log` inside
  each session directory plus `dragback dev status`.
- **Every rehearsal re-triggers Claude Code's folder-trust prompt** in each agent
  pane, because `reset.sh` deletes the session directories and `up.sh` recreates
  them. A person has to answer it — `up.sh` prints a ready-made
  `tmux send-keys` one-liner that accepts all panes at once.
- **`--record` needs one-time macOS permission.** `screencapture -v` silently
  produces nothing until the terminal running it is granted Screen Recording in
  System Settings › Privacy & Security › Screen Recording. `up.sh` detects the
  immediate exit, says so, and arms everything else. Grant it once, then
  `up.sh --record` works.
- **`fallback.sh` never blocks.** The full-screen AppleScript is bounded and
  detached: an unplayable file used to leave QuickTime showing a modal error with
  AppleScript waiting behind it forever.
- **Hooks are all-or-nothing.** If `hooks/` is incomplete on the checkout, the
  generated session settings omit the hooks block entirely and `up.sh` says
  ARMED WITHOUT ENFORCEMENT. Writing a settings file that names a missing script
  makes Claude Code report a hook error on *every* tool call, which bricks the
  session rather than degrading it.
- **A print-mode session that gives up releases its binding** through the
  `SessionEnd` hook, so after firing, `dragback dev status` shows only the
  survivors and `dragback dev why <id>` can no longer find the interrupted
  session. The assignment states and each session's
  `.dragback/hook-verdict-cache.json` still carry the whole explanation. With
  tmux, interactive sessions stay open and `dev why` keeps working.
- With the shipped five-session fixture the three interrupted assignments have
  no corrected plan, so the verdict endpoint denies them **every** time (rule 5)
  rather than once (rule 4). Without an acknowledgement an agent cannot even
  write the correction it has worked out — the hook that stopped the wrong work
  also stops the fix. `ack.sh` is that beat, and it is a person pressing a key.
- The deny payload quotes the decision **text** and the task titles, but not the
  new requirement value. `audience: admin_only` appears nowhere in it, so agents
  paraphrase — they write `administrators`. Correct in substance, and not what
  the graph says. Putting the requirement delta in the payload's `now_required`
  would close it.
- If your Claude Code user settings inject a `SessionStart` handoff document, it
  lands in **every** demo session too. Clear it before rehearsing, or five
  agents open with a paragraph about unrelated work.
