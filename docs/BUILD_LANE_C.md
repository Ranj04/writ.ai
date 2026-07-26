# Lane C — Demo launcher (third agent, runs in parallel, touches nothing the other lanes own)

You are building the stage machinery for a live demo. You are **not** building product code, and two other people are editing this repository right now.

---

## Hard isolation rules — read twice

**You own exactly one directory: `scripts/demo/`.** Every file you create lives there.

**Do not open, edit, or create files in any of these.** Another agent is actively writing them and your edit will collide:

```
backend/          frontend/         Makefile
cli.py            config.py         pyproject.toml
AGENTS.md         CLAUDE.md         .env  .env.example
docs/  (except this file, read-only)
```

**Work in your own worktree with isolated agent state:**

```bash
git worktree add ../db-demo -b lane-c-demo
# Codex only:
export CODEX_HOME="$HOME/.codex-demo" && mkdir -p "$CODEX_HOME"
```

**Do not add dependencies.** Bash plus `python3` standard library only. Do not assume `jq`, `tmux`, `yq`, or anything else is installed — if you use `tmux`, detect it and degrade to plain background processes when it's absent.

**Do not add Makefile targets.** The Makefile is a collision risk. Ship plain scripts. If everything else is finished and the other lanes have stopped editing, you may propose the targets in your final report as a diff for a human to apply — do not apply it yourself.

---

## The critical constraint: what you depend on does not exist yet

The hook, the `/check` endpoint, the session binding, and `writai approve` are being written **right now, in parallel with you**. You must build against the contract below, not against working code.

Therefore: **every script must degrade gracefully and report, never crash.** If a component is missing, say so in the readiness output and continue doing what you can. A script that exits 1 because the hook isn't merged yet is useless to us at 2am.

**Contract you may rely on** (do not invent beyond it):

- Three services on `localhost:8001` (authority), `8002` (agent), `8003` (executor). Each answers `GET /health` with `{"status": "ok"}`. Start them with `make authority`, `make agent`, `make executor`.
- A session binds to a task by a file at `<repo>/.writai/task` containing a single task id, e.g. `TASK-102`.
- A Claude Code session registers itself on start via a `SessionStart` hook and is checked before each tool call via a `PreToolUse` hook. Hook configuration lives in `.claude/settings.json` inside each session's directory.
- `writai approve --text "<message>"` triggers a decision change. **Assume this signature; if it differs, read the real one and adapt.**
- Existing state to clear on reset: `.writai/` in the repo root and in each demo directory.

---

## Deliverables, in priority order. Stop wherever time runs out.

### 1. `scripts/demo/reset.sh` — build this first

Ten seconds, idempotent, safe to run twice. Removes demo worktrees and directories, clears `.writai/` state, kills stray service processes and agent sessions, reseeds the graph.

This is first because rehearsal count is what makes a demo good, and reset time is what caps rehearsal count.

### 2. `scripts/demo/up.sh` — arm everything, then wait

Starts the three services and waits for `/health`. Creates five demo directories, each with its `.writai/task` file and its `.claude/settings.json` hook config. Launches a Claude Code session in each with a canned starter prompt. Then **stops and waits** — it must never trigger the change itself. Arming and firing stay separate so the operator can arm during the previous team's demo.

Default to five sessions, `--sessions N` to override. Three bound to a task carrying the `export.authorization` scope, two to `export.generation`. Read the actual task ids from the seeded graph rather than hardcoding them if you can; hardcode with a loud comment if you cannot.

### 3. `scripts/demo/prompts/session-1.txt` … `session-5.txt`

Canned starter prompts, identical every run — reproducibility matters far more than realism here. Each must generate **frequent tool calls** (reading files, writing small test files, listing directories) rather than long silent reasoning, because the interruption only lands at the next tool call. An agent thinking for forty seconds looks broken on stage.

### 4. `scripts/demo/check.sh` — the readiness checklist

Prints a green/red line per item and exits non-zero only if something is red:

- each service healthy
- graph seeded, and at which version
- N sessions registered
- each session bound to the expected task
- hook config present in each session directory
- `writai approve` present and runnable

This exists so failures are discovered backstage instead of on stage. Make the output large and scannable — this gets read at a glance under pressure.

### 5. `scripts/demo/fallback.sh`

Opens the most recent recording from `scripts/demo/recordings/` full-screen. One keystroke, no fumbling in front of judges.

### 6. `scripts/demo/up.sh --record`

Starts a screen recording into `scripts/demo/recordings/` alongside the launch, so "record the first clean run" happens automatically instead of being remembered. macOS only is fine.

### 7. tmux layout

If `tmux` exists, lay out five agent panes plus one clearly distinct operator pane. If it doesn't, background the sessions and log each to a file. Detect, never require.

---

## Rules for the scripts themselves

`set -euo pipefail`. Idempotent — running twice must not corrupt anything. Never `rm -rf` a path you did not construct in the same script. Every wait has a timeout and a clear message on expiry. All output legible from fifteen feet: short lines, no dense logs to stdout.

---

## Report when done

Which deliverables shipped, which did not, anything in the contract above that turned out to be wrong, and the exact command sequence an operator should run on stage. If you want Makefile targets, include them as a proposed diff — do not apply it.
