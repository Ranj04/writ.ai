# Lane C — assumptions made unattended

Every entry is a question I would have asked a human, the answer I assumed, and
why that answer is the conservative one. Branch `lane-c-demo`, worktree
`../db-demo`, based on `main` (`e8df117`).

---

## 1. Where the work lives

**Question.** Lane C was built earlier in the shared `DragBack` working tree,
before the overnight rule to stay on my own branch. Move it, copy it, or leave it?

**Assumed.** Copied `scripts/demo/` into this worktree on `lane-c-demo` and
**left the originals in place** in the shared tree.

**Why conservative.** Deleting files out of a working tree another agent is
actively using could destroy context mid-task. Duplication is recoverable in the
morning; a deleted file another lane was reading is not. The branch is the
authoritative copy — integrate from here.

## 2. Another lane wrote into `scripts/demo/`

**Question.** `scripts/demo/seed.py` (460 lines) and `scripts/demo/demo-proof/`
appeared in the directory `docs/BUILD_LANE_C.md` assigns to Lane C. Whose stage
machinery wins?

**Assumed.** Neither copied nor modified. They are not on this branch. My scripts
are complete and independently verified; `seed.py` is a parallel implementation
of the same stage (same `csv-exports` workspace, same ports 8001/8002, different
store at `/tmp/dragback-stage`, and it applies `DEC-018` as part of seeding
rather than keeping arming and firing separate).

**Why conservative.** "Touch only the files your lane owns" outranks the
directory claim. A human picks one in the morning. **Running both at once will
collide on ports and bind sessions to a different store than the operator
seeded** — flagged again in HANDOFF.md.

## 3. `dragback approve --text "<message>"` does not exist

**Question.** `docs/BUILD_LANE_C.md` says to assume that signature and "if it
differs, read the real one and adapt".

**Assumed.** Adapted. The real surface is `dragback approve pending` /
`approve change WS DEC --role R`, and it is a Lane B placeholder that exits 2
with `NOT_IMPLEMENTED`. `fire.sh` therefore uses `dragback workspace
propose-change` + `workspace approve-change`, which work today. `check.sh`
reports the placeholder as a warning, not a failure, so it goes green when
Lane B lands.

## 4. Demo session directories live outside the repository

**Question.** Where do the five session directories go?

**Assumed.** `<repo>/../dragback-demo/session-N`, overridable with
`DRAGBACK_DEMO_ROOT`.

**Why conservative.** A session started inside the DragBack checkout inherits the
repo's `CLAUDE.md` and starts reading the implementation contract instead of
doing its canned work. `reset.sh` will only delete a tree carrying the
`.dragback-demo-root` marker `up.sh` wrote, so a mistyped override cannot take
somebody's work with it.

## 5. Per-session hook configuration

**Question.** The hook config rule says it never goes in `~/.claude/settings.json`
and is never committed to `.claude/settings.json`.

**Assumed.** `up.sh` generates `.claude/settings.json` **inside each generated
session directory**, which is outside the repository entirely (see 4). Nothing is
written to `~/.claude/settings.json`, and no `.claude/` directory is created in
any checkout. Verified: this branch adds no `.claude/` path.

## 6. `--record` was not executed

**Question.** Deliverable 6 starts a macOS screen recording. Test it overnight?

**Assumed.** No. The code path ships and degrades with a message if
`screencapture` exits immediately, but I did not start a recording.

**Why conservative.** It records the machine's screen, and macOS may raise a
Screen Recording permission prompt with nobody awake to accept it. Starting an
unattended capture of someone's desktop is not a decision to make for them.
**Untested — run `up.sh --record` once with a human present.**

## 7. tmux layout was not executed

**Question.** Deliverable 7 wants five agent panes plus an operator pane.

**Assumed.** `tmux` is not installed on this machine. The code detects it and
degrades to background `claude -p` sessions logging to
`scripts/demo/logs/session-N.log`, which is the path I tested. I did **not**
install tmux — that is a change to the machine, not to my lane.
**The tmux branch is written but unexecuted.**

## 8. Service ports are now overridable

**Question.** Other agents run on this machine all night. `reset.sh` stops
Dragback services on 8001–8003, which could be another lane's.

**Assumed.** Added `DRAGBACK_DEMO_AUTHORITY_PORT` / `_AGENT_PORT` /
`_EXECUTOR_PORT`. **Defaults are unchanged** (8001/8002/8003), because the
contract fixes them and the hook's default endpoint points at 8002.

**Why conservative.** Six lines that let a second checkout rehearse without
stopping the first one's services. `reset.sh` already refused to kill a process
whose command line is not a Dragback service; this bounds the blast radius
further. I checked the ports were free before running anything tonight.

## 9. This worktree is based on `main`, which lacks Lane A's hooks

**Question.** `main` (`e8df117`) has the fixtures, the CLI and the A1 runtime, but
not `hooks/` or `services/supervisor_check.py` — those are on `lane-a`.

**Assumed.** Verified in this worktree at the **assignment** level (the layer the
overnight correction is about: deny-once is per assignment). The **hook** level —
five real Claude Code sessions registering, three denied with the provenance
path, two allowed, an agent rewriting `AUDIENCE` after acknowledgement — was
proven earlier tonight in the integrated tree, before this branch existed.
Evidence is quoted in HANDOFF.md. I did not merge or rebase `lane-a` in.

## 10. Service interpreter

**Question.** This worktree has no `.venv`, and system `python3` is 3.9 (below the
3.11 floor).

**Assumed.** Ran with `DRAGBACK_DEMO_PYTHON=/Users/ranjivj/DragBack/.venv/bin/python`.
`PYTHONPATH=backend` resolves to **this** worktree's backend — verified. An
integrator working in the main checkout needs neither variable.

## 11. An already-fired workspace is now fatal, not a warning

**Question.** Overnight correction: deny-once is per assignment, so a partial
reset makes the second rehearsal silently allow.

**Assumed.** Escalated from warning to hard failure in two places: `up.sh` now
**exits 1** rather than arming on a fired workspace, and `check.sh` marks it
**red**. `reset.sh` now resolves the workspace store from
`DRAGBACK_WORKSPACE_STORE` / `.env` / the default, deletes it, names anything
left in `.dragback/` that it does not recognise, and prints a verified line
confirming the store is gone.

**Why conservative.** A demo that silently allows is indistinguishable from the
product not working. Refusing costs one second; the failure mode costs the demo.

## 12. Not fixed — another lane's failing test

`backend/tests/test_hooks.py::test_worst_case_context_length_is_recorded` fails in
the **shared** tree: it asserts an exact context length (`8235 == 9496`) against
Lane A's in-flight hook wording. Not on this branch, not mine, **not fixed**.
This branch's suite is green: **282 passed, 2 skipped**.

## 13. Not fixed — deny payload omits the new requirement value

The deny payload quotes the decision text and task titles but never the new
requirement value: `audience: admin_only` appears nowhere in it, so a corrected
agent paraphrases (`administrators`). Correct in substance, not the graph's word.
Lane A's `now_required` is where that belongs. **Written down, not fixed.**
