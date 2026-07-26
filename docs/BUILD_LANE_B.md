# Lane B — Intake & approval (teammate, Codex primary)

**You own the cloud, human-facing half:** Slack intake, extraction into an untrusted draft, Hexclave authority, the approval channels, and the approver-side CLI.

**Ranjiv owns Lane A** — the live supervisor runtime, the Claude Code hook, session binding, the verdict endpoint. You meet at one frozen interface. Neither of you touches the other's files.

---

## Before you start

**Wait for the seam commit.** Ranjiv pushes it in the first ~20 minutes. It contains `supervisor_contract.py`, the `SupervisorExecutionMode.LIVE` change, both CLI group registrations, and every new `config.py` key. Pull it before writing a line. After that neither lane touches `cli.py` or `config.py` again — that removes the biggest merge-conflict surface in the repo (`cli.py` is 51K).

**Install dependencies first, outside Codex.** `codex exec` runs with `sandbox: workspace-write`, which has **`network_access = false`** — `pip install composio` will fail inside the sandbox and you will lose ten minutes not understanding why.

```bash
pip install composio httpx        # or whatever the intake needs — do this now
```

If you genuinely need network inside a run: `-c sandbox_workspace_write.network_access=true`.

**Model identifier, exactly.** `-m gpt-5.6-sol`. **`gpt-5.6` and `gpt-5.6-codex` do not exist** — `-m` accepts any string without validating it and silently falls back to degraded metadata. It will look like it worked.

**Always redirect stdin in scripts.** Bare `codex exec "prompt"` with non-TTY stdin prints *"Reading additional input from stdin…"* and blocks forever. Every command below ends `< /dev/null`.

---

## Your work items

| # | Item | Files you own | Cut line |
|---|---|---|---|
| B1 | Composio Slack trigger → signed webhook → untrusted draft, reusing the Workspace import path | `intake/slack.py`, `services/authority_api.py` | **Must** |
| B2 | Satisfy `apply_decision_change` preconditions + `effective_at` | `intake/decisions.py` | **Must** |
| B3 | Deterministic gate + Hexclave permission check + `authority_policy` injection at `runtime.py` | `intake/gate.py`, `auth/hexclave.py`, `runtime.py` | **Must** |
| B4 | Approver CLI: `pending`, `approve` — the fallback approval path | `cli_approve.py` | **Must** |
| B5 | Slack reaction approval (`SLACK_MESSAGE_REACTION_ADDED`) + blast-radius message | `notify/slack.py` | Should |
| B6 | ntfy/Pushover phone push; email magic link | `notify/push.py`, `notify/email.py` | Nice |
| B7 | Callwright escalation on unacknowledged interrupt — reuses `integrations/callwright.py` as-is | `notify/escalate.py` | Nice |

### B2 is the one that will derail you — do it first if you can

`apply_decision_change` will reject a naive Slack-derived decision. It requires (`engine.py:122-179`):

- a `supersedes_id` naming an **existing** `Decision`,
- `affected_scopes ⊆ superseded.scopes`,
- and a **three-way exact match** between `decision.scopes`, `mutation.affected_scopes`, and the keys of `decision.attributes["requirements"]`.

So a Slack message introducing a constraint on a scope no prior decision covers **cannot be applied at all**. Read how the Workspace import path satisfies this today and do the same thing — most likely each workspace needs a seeded baseline policy decision covering its scopes. Report what you find before building on top of it.

Also: **always set `effective_at`.** `current_requirements()` orders candidates by it and defaults `None` to `datetime.min` (`engine.py:344-350`), so an "effective immediately" message ingested without a timestamp sorts to the *bottom* of precedence and silently loses.

### B1 — reuse, don't rebuild

Workspace document import already does exactly the trust model we want: untrusted draft → extraction confidence in evidence metadata → **explicit human confirmation of the extracted fields** → recorded separately → then server validation. Slack is a new *source* for that path, not a second trust model. Find it, reuse it.

Call `triggers.get_type("SLACK_CHANNEL_MESSAGE_RECEIVED")` before parsing anything — Composio publishes no example payload, so the field paths must come from ground truth, not a guess. Verify the webhook signature with `triggers.parse(body, headers, verify_secret)`.

### B3 — Hexclave

`authority_policy` compares a **single** `decision.authority_role` string against a set (`engine.py:136-140`). So map scope → sets of Hexclave **permission id strings**, and seed `authority_role="approve_compliance"` — a permission id, not a role name like `compliance_lead`, which will simply be rejected.

`runtime.py:23-34` constructs `IntentAuthority` with no `authority_policy` at all, so add the injection point there. Do **not** use `ScenarioDefinition.authority_policy` — its model requires a `mutation: DecisionMutation`, which forces exactly the faked supersession we are avoiding.

Permissions are **not** in the Hexclave JWT, so every check is a network round-trip to `GET /team-permissions?...&recursive=true`. Cache with a short TTL.

### B4/B5 — the approval

**The notification is not the approval.** Every channel — Slack reaction, ntfy button, email link, CLI — must land on **one shared `approve()` path** that resolves a Hexclave identity, checks the permission for the affected scopes, then applies. No channel may approve by asserting it already checked. Write a test per channel proving they all go through it.

Slack reaction approval is the good demo path and needs no Slack app: post a threaded message with the extracted fields and the blast radius, approver reacts ✅. But a reaction is a weaker signal than a signed button click and reactions can be removed — so resolve the **reacting** user, check their permission **at reaction time** not at post time, treat the first qualifying reaction as binding, record it as evidence, and make sure a later un-react does not un-approve. Reactions from users without the permission are ignored silently, never escalated.

Do not build Block Kit buttons in v1 — the click POSTs to *your* endpoint, which means standing up your own Slack app with an interactivity request URL.

### Calling Lane A

```python
from writai.supervisor_contract import InterruptRequest, SupervisorInterruptPort

preview = port.preview(request)     # blast radius for the confirmation screen — no mutation
result  = port.interrupt(request)   # on approval
```

`NullSupervisorInterruptPort` returns empty tuples until Ranjiv binds the real one at the first integration point (~T+120). Build against the Protocol, not the stub. `interrupt()` is idempotent per `decision_id`, so retry safely.

---

## How to run it

### Parallel within your own lane

B1/B2 are the intake chain; B3 (Hexclave) and B5 (Slack notify) share nothing with them. Run them concurrently in worktrees with isolated Codex state:

```bash
git worktree add ../db-b3 -b lane-b-hexclave
git worktree add ../db-b5 -b lane-b-notify

for wt in ../db-b3 ../db-b5; do
  ( export CODEX_HOME="$HOME/.codex-$(basename "$wt")"
    mkdir -p "$CODEX_HOME"                     # NOT auto-created; codex hard-fails otherwise
    codex exec -m gpt-5.6-sol -s workspace-write -C "$wt" \
      --json -o "$wt/.codex-last.md" "$(cat ~/writai-prompts/$(basename "$wt").md)" \
      < /dev/null > "/tmp/$(basename "$wt").ndjson" 2>&1 ) &
done
wait
```

Two things that bite: `CODEX_HOME` must already exist, and each isolated home needs its own auth — pass `OPENAI_API_KEY` / `CODEX_API_KEY` via env rather than logging in N times.

### Cross-model review — after every work item, no exceptions

Author with Codex, review with Claude. Different model, different failure modes; that's the whole point.

```bash
git diff main...HEAD > /tmp/b1.diff

claude --model fable -p --permission-mode plan \
"Read /tmp/b1.diff and AGENTS.md in this repo.

Find DEFECTS ONLY. No praise, no summary. For each: file, line, what breaks, minimal fix.
Check specifically:
 1. Does any code path let a model output become a verdict, or approve without a
    Hexclave permission check?
 2. Do ALL approval channels funnel through one shared approve() path?
 3. Is the permission checked for the REACTING user at reaction time, not the posting
    user at post time? Does un-reacting fail to un-approve?
 4. Does every Slack-derived decision set effective_at?
 5. Does it satisfy apply_decision_change's supersedes_id requirement, the
    affected_scopes subset rule, and the three-way scope/requirement match?
 6. Is the Composio webhook signature actually verified before the payload is trusted?
 7. Would the canonical CSV proof or any of the 12 Examples break?
Return a numbered defect list or the single word NONE."
```

`--permission-mode plan` keeps the reviewer read-only.

### Reviewing Ranjiv's work

Same shape, pointed at his branch diff, using the Lane A checks from his prompt. Do it at each integration point, not at the end.

---

## Order of operations

1. **Pull the seam commit.** Install dependencies outside the sandbox. *Blocking.*
2. **B2 first** — report how the Workspace path satisfies the supersession preconditions before building intake on an assumption.
3. **B1, then B3 and B5 in parallel worktrees.** Claude review after each.
4. **B4 early** — the CLI approve path is the fallback that keeps the demo alive if Slack approval isn't ready. It is cheap; do not leave it to the end.
5. **T+120 — first integration with Lane A.** Real `SupervisorInterruptPort` replaces the stub. Verify the blast-radius count equals the set that actually gets denied.
6. **B6, B7 only if there's room.**

**Cut from the bottom.** B6 and B7 are droppable — B7 is nearly free since `integrations/callwright.py` is already a complete grant-gated caller with an idempotent attempt store, so it may be worth ten minutes for the sponsor prize even under time pressure.

## Things that will waste your time if you don't know them

- Composio terminology is versioned: `entity_id` is legacy, use `user_id`; `apps`→`toolkits`, `actions`→`tools`, `integration`→`auth config`. Never use `default` as a user id.
- **Teams has zero Composio triggers.** Slack only. Do not start on Teams.
- Gmail is polling with a ~15-minute floor — not a demo path.
- `codex exec` has **no `-a/--ask-for-approval`** (that's the TUI only) and `--full-auto` is deprecated. Use `-s workspace-write`.
- `codex debug models` lists the real catalog if you ever doubt an identifier.
- Nothing clears `invalidated_scopes` — `_mark_artifact` only downgrades. Never write a gate that depends on a task returning to `VALID`.

---

## Phase 4 — Approval screen in the existing web app (whichever lane clears first)

**Do not start this until your lane's Phase 3 is done and reported.** Whoever clears first takes it — say so in chat so you don't both start. It is one page on the frontend that already exists (`frontend/src/App.tsx`, `frontend/src/api.ts`, `frontend/src/components/`), not a new app.

**What it shows.** A pending-changes queue, and for the selected change: the Slack message, its author, the extracted scopes, the requirement delta, the evidence span — and the **blast radius**, *"3 of 5 active sessions will be interrupted — Priya, Marcus, Dan"*, with the sessions that will survive listed separately. Approve and reject buttons. After approval, which sessions were actually interrupted versus preserved.

**The one hard rule.** The browser decides nothing. The README already states the discipline — *the browser does not traverse the graph, decide verdicts, sign grants, calculate pass results, or invent loop state* — and it holds here. The blast radius comes from the server's `SupervisorInterruptPort.preview()`, never computed client-side; approve posts to the same shared `approve()` path every other channel uses, and the Hexclave permission is checked server-side. A browser that can approve by asserting it already checked is not a permission system.

**Reuse, don't rebuild.** The SSE `EventBroker` already streams state changes; wire the queue to it rather than polling. Match the existing Workspace visual language — the preserved-work list matters as much as the invalidated one, and that distinction is already a pattern in the Examples UI.

**Why it is worth doing at all**, given the CLI already works: it is the single most photogenic artifact in the product, it is what makes the system read as safe rather than invasive, and every other team on that stage will be showing a terminal.
