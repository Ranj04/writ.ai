# Finish Dragback — integration and completion

You are finishing this project. Four parallel lanes plus a CI lane have already built most of it. Your job is to merge their work, close every gap that was cut for time, and hand back something coherent.

Run this from the **main repository**, not a worktree — you will be merging branches.

Read `CLAUDE.md` first. Its invariants, engine preconditions and definition of done govern everything below and are not restated here.

---

## How you work: two models, alternating

You are `fable`. `gpt-5.6-sol` is available through the Codex CLI. **Every work item is built by one model and adversarially reviewed by the other.** Neither model reviews its own work — that rule already paid for itself once tonight, when a cross-model review found four fail-open holes that the authoring model's own tests had missed.

You build most items. Hand these to sol, because they are cleanly isolated and benefit from a different set of blind spots:

- the CrustData observation path (Phase 2, item 6)
- the `.dragback/attach` server-side wiring (Phase 2, item 3)

To have sol build:

```bash
codex exec -m gpt-5.6-sol -s workspace-write -C "$PWD" \
  --json -o /tmp/sol-out.md "<scoped prompt>" < /dev/null
```

To have sol review your work:

```bash
git diff <base>...HEAD > /tmp/review.diff
codex exec -m gpt-5.6-sol -s read-only -C "$PWD" --json -o /tmp/review.md < /dev/null \
"Read /tmp/review.diff and CLAUDE.md. Find DEFECTS ONLY — no praise, no summary.
For each: file, line, what breaks, minimal fix. Check specifically:
 1. Any path where a model output becomes a verdict.
 2. Any fail-open hole: cached allows replayed, verdicts written after side effects,
    swallowed write errors, malformed responses treated as allow.
 3. Anything depending on a task returning to ValidityStatus.VALID — nothing ever
    clears invalidated_scopes, so that is a permanent loop.
 4. Anything that would break the canonical CSV proof or the 12 Examples.
Return a numbered defect list or the single word NONE."
```

`< /dev/null` is mandatory or `codex exec` blocks reading stdin. Note `workspace-write` has `network_access = false`; install anything sol needs beforehand.

Fix confirmed defects. Ignore ones you can disprove, and say why in your report.

---

## Phase 0 — Inventory and merge

**Before touching anything: `git tag pre-integration`.** Never force-push, never delete a branch, never discard another lane's commits.

Current state, verified:

- `origin/main` — Lane B: `intake/`, `auth/hexclave.py`, `notify/`, `services/supervisor_api.py`, `hooks/claude_code_hook.py`
- `lane-a` — 12 commits ahead of main, **already merged Lane B**. Survivor proven causally, hook hardened, fail-open holes closed, demo seeder, CLI, notifications, runbook
- `lane-c-demo` — demo launcher, cross-model hardened, handoff written
- `lane-d-approvals` — approval screen wired to Lane B's real endpoints, adversarial review applied, consistency audit written
- Lane E — `.github/workflows/` and `scripts/ci/`, 32 tests, isolated

Merge in this order, running the full suite after each and stopping if it goes red:

```
main <- lane-a          # largest, already contains Lane B
main <- lane-d-approvals # frontend only, cannot conflict
main <- lane-c-demo     # WILL conflict, see below
main <- lane-e branch
```

**Known collision:** `scripts/demo/` exists on both `lane-a` and `lane-c-demo` with the same fifteen filenames and different content. Resolution: **take Lane C's launcher wholesale** — it was purpose-built and cross-model hardened — and **keep Lane A's `scripts/demo/seed.py`**, which Lane C does not have. Lane A had already staged this resolution before handing off.

Read every lane's `HANDOFF.md` and `ASSUMPTIONS.md` before you start. They contain decisions you must not silently reverse.

---

## Phase 1 — Resolve the conflicts between lanes

**1. Two hook implementations.** After merging, `hooks/claude_code_hook.py` (Lane B) and `hooks/dragback_pre_tool_use.py` + `dragback_hook_lib.py` (Lane A) both exist. **Lane A's ships** — 53 tests, proven end to end against real sessions twice, and it survived the audit that found four fail-open holes: a cached *allow* replayed on transport failure, the desktop notification running before the verdict was written, `emit_json` swallowing write errors and still exiting 0, and malformed responses accepted as allows.

Port any behaviour Lane B's covers that Lane A's does not, **repoint `backend/tests/test_claude_code_hook.py` at Lane A's implementation** rather than deleting those tests, then delete the duplicate. Two hooks in one directory means someone eventually wires the unhardened one.

**2. `GET /supervisor/sessions`.** `supervisor_api.py` has `/check` and `/start` but no list route. `dragback status` and `dragback why` call it and are dead without it. Return assignment id, task id, binding state, snapshot and status per registered session.

---

## Phase 2 — Close every gap cut for time

**3. `.dragback/attach` is not wired.** `cli_dev.py` writes the marker file; `ClaudeCodeSessionRegistry.attach()` is only ever called from tests. So `dragback attach` is a no-op against the live service today — it writes a file the server never reads — and it is *first* in the documented binding resolution order. Wire it end to end. **Give this to sol.**

**4. CI check defaults.** Turn `--require-grant` on by default, scoped so it only fires when the branch actually resolves to a task: an unbound branch passes, a bound branch with no grant fails. Leave `--require-binding` off. Add a test for the redirected-then-re-authorized branch — it passed once, was invalidated, was redirected and re-authorized against the new snapshot, and must now pass again. A naive implementation fails that case forever.

Also add to `scripts/ci/README.md`, prominently: the workflow is **advisory** until *Branch authorization is current* is added to the protected branch's required status checks in repository settings, and advisory closes nothing. Include the click path.

**5. `check.sh` must hard-fail** on any unbound session, any session whose snapshot does not match the current graph version, and any assignment that has already consumed its deny. These are the two silent demo-killers: an unbound session is allowed everything and looks identical to a working demo, and deny-once is per assignment, so a skipped re-seed makes a healthy system look broken. Make these the loudest output in the script.

**6. The CrustData observation path** — the last unbuilt feature, and it is on-thesis rather than a bolt-on. An approver changing role or leaving makes every decision they approved rest on a fact that is no longer true. Subscribe a CrustData person watcher; on a role change or departure, flag the decisions that person approved for review. Reuse `intake/replay.py`'s pattern. The watcher has a documented one-hour minimum interval and **cannot fire live** — capture a real payload and replay it on demand, labelled *real payload, replayed*, never as live. **Give this to sol.**

**7. Lane D's remaining items:** ship `/approvals/why` — the developer-facing view, with *work preserved* given equal visual weight to *work invalidated* — and then **act on the consistency audit Lane D produced**, which it was told to report but not execute. Work through it one divergence at a time, running `npm run typecheck && npm run test` after each, and stop immediately if anything in `/` or `/scenario-lab` changes behaviour. Those two routes are the fallback demo.

**8. Lane C's remaining items:** `--record`, the tmux layout if `tmux` is present, and wiring the launcher to use **Superset worktrees** for the five sessions when `superset` is on PATH, falling back cleanly when it is not.

**9. Gate the seeder's auth bypass** behind an env var the demo sets. It bypasses no authority check — role, scope, confidence, requirement match and proposal binding all still run — but it must not be reachable by accident, and it belongs on the real-vs-simulated panel.

**10. Update the real-vs-simulated panel** to match what is now true, including that a crashed hook fails open and the PR check is the backstop.

---

## Phase 3 — Verify what has been claimed but never confirmed

State each of these explicitly in your report, with evidence:

- Has a **Gemini extraction** actually succeeded end to end with the live key? Nobody has confirmed it, and it sits at the front of the chain. The `--scope/--was/--now` bypass must still exist as the fallback.
- Is **Composio** delivering real webhooks, or is message text still pasted by hand?
- Is **Hexclave** doing real permission checks, or is a hardcoded allowlist still in place?
- Does **every approval channel** — Slack reaction, ntfy, email, CLI, web — funnel through one shared `approve()` path, with a test per channel proving it?
- Does the **five-session demo run from a cold machine** using the runbook, twice in a row, with a re-seed between?
- Do the **canonical CSV proof and all 12 Examples** still pass?

---

## Out of scope — do not build

Microsoft Teams, Discord or email intake. Agent runtimes other than Claude Code. A desktop or mobile app — the CLI serves the interrupted developer and the web screen plus Slack serve the approver, so a desktop client adds no user today. A durable multi-machine event bus; `EventBroker` stays process-local with its 100-event history, recorded as a known limit rather than fixed. Superset's undocumented `terminals_send` as a runtime dependency.

---

## Report

A single `INTEGRATION_REPORT.md`: what merged and in what order, every conflict and how you resolved it, each Phase 2 item with its state, every Phase 3 answer with evidence, which model built and which reviewed each item, defects the reviews found and which you fixed or disproved, and the exact command sequence to run the demo from a cold machine.
