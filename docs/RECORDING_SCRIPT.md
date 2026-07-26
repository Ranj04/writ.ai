# Recording script

The stage is already armed and `check.sh` is green. **Do not reset. Do not re-arm.** Start at step 1.

---

## Copy-paste block — never type these on camera

**A — fire the change** *(operator pane)*

```bash
WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1 scripts/demo/fire.sh
```

**B — show the split** *(operator pane)*

```bash
.venv/bin/writai --agent-url http://127.0.0.1:8002 dev status
```

**C — show the provenance chain** *(operator pane, only after B)*

```bash
.venv/bin/writai --agent-url http://127.0.0.1:8002 dev why $(.venv/bin/writai --agent-url http://127.0.0.1:8002 --json dev status | python3 -c 'import json,sys;print(next(s["session_id"] for s in json.load(sys.stdin)["sessions"] if s["assignment"]["task_id"]=="TASK-204"))')
```

**D — recovery, only if a step goes red** *(repo terminal, ~40s)*

```bash
export WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1 && scripts/demo/reset.sh && scripts/demo/up.sh && tmux send-keys -t writai-demo.1 Enter \; send-keys -t writai-demo.2 Enter \; send-keys -t writai-demo.3 Enter \; send-keys -t writai-demo.4 Enter \; send-keys -t writai-demo.5 Enter && scripts/demo/check.sh
```

---

## 1. Attach

**SAY:** "Five engineers, five Claude Code sessions, running right now. Same repo, same feature — two building the CSV export, three building who's allowed to download it."

**RUN:** *repo terminal*

```bash
tmux attach -t writai-demo
```

**SEE:** Six panes. One operator pane, five agent panes, each with a Claude Code session working its own task.

**IF NOT:** `tmux kill-server`, then paste **D**.

---

## 2. Show the before state

**SAY:** "All five are authorised against the same decision snapshot. Each one holds a grant bound to that exact version of company intent."

**RUN:** *operator pane* — paste **B**

**SEE:** Five sessions, every one `running`, every one `graph-v17`.

**IF NOT:** if any session is missing, paste **D**.

---

## 3. The trigger

**SAY (before you paste):** "Compliance changes one decision: exports become admin-only. In production this arrives from Slack — we're connected through Composio — tonight I'm triggering it directly so you can see the timing."

**RUN:** *operator pane* — paste **A**

**SEE:**

```
⏺ Blast radius: 3 of 5 active sessions will be interrupted
  Stopping (3)    Priya · Marcus · Dan
  Continuing (2)  Sara · Alex
```

then `Approve the change now? [y/N]`

**SAY (pointing at it):** "Before it applies anything, it tells you exactly who you're about to interrupt. Three of five. By name."

**IF NOT:** if it prints nothing and returns, the services are down — paste **D**.

---

## 4. Confirm — twice. This is the Hexclave beat.

**SAY (before you press anything):** "Now — who is allowed to make this call? Because if the answer is 'anyone in the Slack channel', this product is dangerous rather than useful."

**SAY:** "Identity runs through Hexclave. It answers one question: does this person hold `approve_compliance` on this team? If they don't, the reaction is ignored, silently, and nothing moves."

**RUN:** press `y` then Enter. **It asks a second time.** Press `y` then Enter again.

**SEE:** `change DEC-018 approved as approve_compliance`, then `FIRED — watch the sessions`.

**SAY (as it applies):** "And there are three ways to approve — this terminal, the web screen, or a reaction in Slack. All three land on the same permission check. None of them can approve by claiming it already checked — the request envelope refuses those fields outright, so a caller physically cannot assert its own authority."

**IF NOT:** an `EOFError` means you missed the second prompt — press `y` Enter.

---

## 5. The three-second silence

**SAY:** nothing. Count three. Let the panes move.

**RUN:** nothing. Hands off the keyboard.

**SEE:** Priya, Marcus and Dan each stop on their next tool call with the writ.ai block and the admin-only redirect. Sara and Alex keep working.

**IF NOT:** if a pane looks frozen rather than blocked, ignore it and go to step 6 — the verdict lives in the service, not the pane.

---

## 6. Show the split — deliver this slowly

**SAY:** "Three stopped. Two never noticed. A kill switch stops everything, which is why teams switch them off — this stopped exactly the three whose task scope intersected the change."

**RUN:** *operator pane* — paste **B**

**SEE:** Three sessions interrupted on TASK-203, TASK-204, TASK-205. Two continuing on TASK-201, TASK-202.

**SAY (after it prints):** "And they didn't start over. Each one read the correction and proposed the admin-gated version of what it was already building."

**IF NOT:** if the labels look wrong, say the numbers from the blast radius instead and move on — do not debug on camera.

---

## 7. Show the chain

**SAY:** "Any of them can ask why, and get the whole path back — the decision, what it superseded, and the evidence."

**RUN:** *operator pane* — paste **C**

**SEE:** The provenance chain, TASK-203/204/205 invalidated, TASK-201/202 still valid.

**IF NOT:** skip it. Step 6 already made the point.

---

## 8. The CrustData beat — say this, no command

This is the idea that makes the product bigger than one demo. Deliver it as a question.

**SAY:** "One more thing. That approval rested on Dana being the compliance lead. What happens when she changes role — or leaves?"

**SAY:** "Then every decision she approved is resting on a fact that stopped being true, and nobody notices, because approvals don't expire when people move."

**SAY:** "CrustData watches for exactly that. When an approver changes role or leaves the company, writ.ai flags every decision they approved for review — the same mechanic you just watched, pointed at people instead of tickets."

**SAY:** "Because decisions don't only rest on other decisions. They rest on facts about the world. And facts change."

---

## Closing — say these in order

**1 — enforcement**
> "Two things you're not seeing. This runs through a Claude Code hook installed in organisation-managed settings, so a developer can't disable it locally. And hooks fail open by design, so there's a pull-request check behind it that fails closed."

**2 — engineering credibility**
> "760 tests. And every change was reviewed by a second model — that found four fail-open holes our own tests missed, including a cached *allow* being replayed when the service was unreachable."

**3 — the honest one. Do not skip it.** Volunteering the limits is what makes everything above believable.
> "What we haven't proven end to end: the Slack delivery, and tonight's approval falls back to an in-process seam because the approver's Hexclave user key isn't provisioned yet — the identity layer is wired and tested, the credential isn't. And the CrustData signal is a replayed capture, because their watcher runs hourly and can't fire on a stage."

**Then close:**
> "Tests prove the code works. writ.ai proves the work is still wanted — for everyone at once."

---

## Two claims to avoid

- Do **not** claim the redirect wording is deterministic. The scope verdict is stable; the requirement text is not.
- Do **not** run `scripts/demo/ack.sh`. The hook self-acknowledges and nothing on the staged path needs it.
