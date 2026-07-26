# Recording script

The stage is already armed and `check.sh` is green. Do not reset. Do not re-arm.
Start at step 1.

---

## Copy-paste block — never type these on camera

**A — fire the change** (operator pane):

```bash
WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1 scripts/demo/fire.sh
```

**B — show the split** (operator pane):

```bash
.venv/bin/writai --agent-url http://127.0.0.1:8002 dev status
```

**C — show the provenance chain** (operator pane, run only after B):

```bash
.venv/bin/writai --agent-url http://127.0.0.1:8002 dev why $(.venv/bin/writai --agent-url http://127.0.0.1:8002 --json dev status | python3 -c 'import json,sys;print(next(s["session_id"] for s in json.load(sys.stdin)["sessions"] if s["assignment"]["task_id"]=="TASK-204"))')
```

**D — recovery, only if a step goes red** (repo terminal, costs ~40s):

```bash
export WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1 && scripts/demo/reset.sh && scripts/demo/up.sh && tmux send-keys -t writai-demo.1 Enter \; send-keys -t writai-demo.2 Enter \; send-keys -t writai-demo.3 Enter \; send-keys -t writai-demo.4 Enter \; send-keys -t writai-demo.5 Enter && scripts/demo/check.sh
```

---

## 1. Attach

**SAY:** "Five engineers, five Claude Code sessions, all running right now."

**RUN:** repo terminal

```bash
tmux attach -t writai-demo
```

**SEE:** Six panes. One operator pane, five agent panes, each showing a Claude
Code session working on its own task.

**IF NOT:** `tmux kill-server`, then paste **D**.

---

## 2. Show the before state

**SAY:** "All five are authorised. Same decision snapshot, graph-v17."

**RUN:** operator pane — paste **B**

**SEE:** Five sessions, every one `running`, every one `graph-v17`.

**IF NOT:** if any session is missing, paste **D**.

---

## 3. The trigger

**SAY:** "Compliance just changed one decision. Exports become admin-only."

**RUN:** operator pane — paste **A**

**SEE:** The blast radius, before anything is applied:

```
⏺ Blast radius: 3 of 5 active sessions will be interrupted
  Stopping (3)    Priya · Marcus · Dan
  Continuing (2)  Sara · Alex
```

Then `Approve the change now? [y/N]`

**IF NOT:** if it prints nothing and returns, the services are down — paste **D**.

---

## 4. Confirm — twice

**SAY:** "One person approves it."

**RUN:** press `y` then Enter. **It asks a second time.** Press `y` then Enter again.

**SEE:** `change DEC-018 approved as approve_compliance`, then
`FIRED — watch the sessions`.

**IF NOT:** if you see `EOFError`, you missed the second prompt — press `y` Enter.

---

## 5. The three-second silence

**SAY:** nothing. Count three seconds. Let the panes move.

**RUN:** nothing. Hands off the keyboard.

**SEE:** Priya, Marcus and Dan each stop on their next tool call with the
writ.ai block and the admin-only redirect. Sara and Alex keep working.

**IF NOT:** if a pane looks frozen rather than blocked, ignore it and go to
step 6 — the verdict is in the service, not the pane.

---

## 6. Show the split

**SAY:** "One person acknowledged the change. Five agents inherited it — and
only the three that needed to."

**RUN:** operator pane — paste **B**

**SEE:** Three sessions interrupted on TASK-203, TASK-204, TASK-205. Two
continuing on TASK-201, TASK-202.

**IF NOT:** if all five continue, the change did not apply — paste **A** again.

---

## 7. Show the provenance chain

**SAY:** "This is why. Not a red badge — the actual path, and what survived."

**RUN:** operator pane — paste **C**

**SEE:** The chain `DEC-018 → DEC-004 → SPEC-009 → TICKET-100 → TASK-204`, then
all five tasks with `still valid` or `invalidated` beside each, and `←` on
TASK-204. `Preserved` names the two CSV tasks. `Changed` gives the admin-only
instruction.

**IF NOT:** if it prints `Which became —`, the change has not applied yet — go
back to step 3.

---

## 8. Stop

**SAY:** "Deterministic invalidation, scope-aware, enforced in the agent's own
session."

**RUN:** nothing.

**Do not open `/approvals` in the browser.** Skip it. The screen labels itself a
rehearsal, which contradicts the claim you just made on camera.

---

## Two things not to say

- Do not say the redirect **wording** is deterministic. The scope verdict is;
  the requirement text is not.
- Do not claim the approval was authenticated. It ran on the labelled
  in-process seam.
