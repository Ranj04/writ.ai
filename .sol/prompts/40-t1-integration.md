# TASK: T1 — Integration. The one line that spans two tracks, and the numbers.

You are **Fable**. Work on the **main tree** at `/work/writ.ai`, on branch
`phase/t1-integration`, after Tracks A, B and C have all merged. Nothing else is
running. Activate `.venv`.

This stage exists for exactly three things. Do those three and stop. Anything else you
notice goes in your report, not in the diff.

**Standing rule 8 applies. Standing rule 16 applies: the gate must be exit 0.**

---

## Phase T1.1 — Land the cross-track line, backfill the numbers, prove the whole

**Why:** Track A changed `IntentAuthority.evaluate_plan` (`engine.py:354`) to take an
optional `report` parameter with a `None` default, and passed it explicitly at every
call site it owned. The **per-workspace** call site at
`backend/writai/workspaces/authority_contexts.py:417` is Track B's file, so Track A
could not touch it, and Track B's worktree did not contain the new signature, so Track B
could not add the argument without a `TypeError`. **On the merged tree, both halves
exist and the line can finally be written.** Until it is, workspace `/authorize`
responses silently return empty `invalidated_artifact_ids`, `preserved_artifact_ids`,
`evidence_refs` and `invalidation_path` — a real regression introduced by A5 and closed
here. Separately, Track C may have left `TODO(T1):` markers in `README.md` for numbers
only Track B could measure.

**Files:** `backend/writai/workspaces/authority_contexts.py`, `README.md`,
`outputs/OPEN-ITEMS-REGISTER.md`

**Do:**

1. Confirm the premise before editing. Run:
   ```bash
   grep -n "def evaluate_plan" backend/writai/authority/engine.py
   grep -n -A6 "evaluate_plan(" backend/writai/workspaces/authority_contexts.py
   ```
   The first must show a `report: InvalidationReport | None = None` parameter. The second
   must show a call at or near line 417 that does **not** pass `report=`. **If either is
   not true, stop: Track A's A5 did not land, or Track B already added the line. Report
   and do nothing else in this step.**
2. Add `report=context.authority.last_report,` to that call, matching the surrounding
   keyword-argument style exactly. Add a one-line comment: per-workspace contexts each
   own their own `IntentAuthority`, so correlating this engine's last report with this
   plan is real, unlike the shared runtime at `authority_api.py:1694`.
3. Add `backend/tests/test_live_workspaces.py::test_a_workspace_authorization_still_reports_its_own_invalidation_path`:
   drive a workspace through import → baseline approval → a decision change → authorize,
   and assert `invalidation_path` is **non-empty** and equals the path that workspace's
   own change produced. Without this test, step 2 is unverified and a future refactor
   deletes it silently.
4. Backfill every `TODO(T1):` marker Track C left. `grep -rn "TODO(T1)" README.md docs/`
   → for each, insert the real number from Track B's report (the `get()` timings at
   N ∈ {1, 50, 200}, the ratio, and the observed pre-fix file mode). **Do not invent a
   number; if Track B's report does not contain it, re-run Track B's measurement
   harness and use what you get.**
5. Re-run the coverage measurement from T0.4 step 1 on the integrated tree and confirm
   the total floor and all seven per-file floors still hold. **If a floor now fails, that
   is a finding, not a licence to lower it** — report it to the orchestrator, who decides
   between "the track owes a test" and "the floor was wrong". If the orchestrator says
   lower it, lower it **with a one-line entry in `outputs/OPEN-ITEMS-REGISTER.md`** naming
   the module and the reason.
6. Append one final block to `outputs/OPEN-ITEMS-REGISTER.md` under
   `## Plan execution — integration`: every phase skipped by any track, and the three
   dead-code items that `AGENTS.md:34` required be mentioned rather than deleted —
   `workspaces/live_interrupt.py`, `supervisor_contract.py:35`
   `NullSupervisorInterruptPort`, and the note that `workspaces/interrupt_port.py` is
   **not** dead (`scripts/demo/seed.py:47,479`).

**Acceptance:** Workspace authorization carries its own provenance again; the README
contains no `TODO(T1):` marker and no invented number; the gate is green on the fully
integrated tree.

**Verify:**
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_live_workspaces.py -q \
  -k "still_reports_its_own_invalidation_path"
```
→ `1 passed`.
```bash
grep -rn "TODO(T1)" README.md docs/ ; echo "MARKERS=$?"
```
→ `MARKERS=1` (grep found nothing).
```bash
bash scripts/check.sh; echo "EXIT=$?"
```
→ `EXIT=0`.
```bash
PYTHONPATH=backend python3 -m writai.demo | tail -3
```
→ ends `writ.ai proof complete`, exit 0, with zero environment variables.
```bash
WRITAI_ENV=production PYTHONPATH=backend python3 -c "import writai.services.agent_api" 2>&1 | tail -2
```
→ still a `RuntimeError` naming `WRITAI_GRANT_SECRET` — T0's guard survived three merges.

**If it fails:** If step 3's test finds an empty `invalidation_path` even after step 2,
the workspace context's `last_report` is `None` because the change was applied on a
**different** engine instance — read `workspaces/authority_contexts.py:132-153` and
report which instance holds the report. Do not paper over it by reinstating
`self.last_report` inside `evaluate_plan`; that is the defect A5 removed.

## When T1 is done

Report the integrated gate output, the numbers you backfilled and their source, the
coverage total and seven per-file percentages on the merged tree, and every unresolved
item now sitting in `outputs/OPEN-ITEMS-REGISTER.md`. Remove no worktree — the
orchestrator does that.
