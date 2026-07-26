# Tool-call-level scope enforcement — potential implementations

**Status:** design proposal, not built. Planning doc for the "let the hook read the
running session and decide per tool call" enhancement.

**Owner / reviewers:** _TBD_

---

## 1. What this is about

Today, Dragback enforcement is **session-level**. The moment a decision interrupts a
session, the `PreToolUse` hook denies **every** subsequent tool call in that session
until a human acknowledges — regardless of whether the specific call touches the thing
that actually changed.

This doc captures the options for making enforcement **tool-call-level**: block only the
calls that touch an invalidated scope, and let out-of-scope work in the same session
proceed.

### The plain-language "why"

> **Today** it's *"your session is stale, stop."*
> **This makes it** *"that specific action is stale, stop — the rest of your work is fine."*

Concretely, in the CSV-export demo: a decision invalidates the export code. Right now the
agent freezes entirely. With this change, the agent's **export edit** is blocked with the
exact reason and provenance — while, in the same session, it can keep writing tests,
editing docs, or touching anything that does **not** map to `export.authorization`.

### Why it matters for the product

The product's headline is **scope-sensitive invalidation** — *"out-of-scope siblings
survive."* Today that is only true **across tasks/agents** (`TASK-101` survives when
`TASK-102` is invalidated). Inside a single running session it is **not** true — everything
freezes. This change makes the promise true one level deeper: at the level of the individual
tool call the agent is about to make.

---

## 2. The constraint that shapes every option

`CLAUDE.md` makes the hook's wire payload non-negotiable, and a test enforces it:

> the hook sends only `session_id`, `tool_name`, and a timestamp. Never tool input, file
> contents, or transcripts.

So **"read the device" can never mean "send more to the server."** It can only mean:
**read more on the device, decide locally, transmit the same three fields.** Any option that
puts `tool_input`, file contents, or transcript data on the wire breaks
`test_pre_tool_hook_transmits_only_privacy_allowlist` and violates a stated product
invariant.

---

## 3. The gap in the current code

`ClaudeCodeSessionEnforcement.check()` (`backend/dragback/workspaces/session_enforcement.py:300`)
receives `tool_name` but **never branches on what the call does**. The logic is purely:

> *Is this session's assignment stale/interrupted on an intersecting scope? If yes → deny
> every tool call until acknowledged.*

The one field that would make it precise — the `tool_input` the hook already holds — is
deliberately ignored (and must stay off the wire). The opportunity is to **use it locally.**

---

## 4. The load-bearing insight

Scopes are **semantic**, not physical:

- A scope is `"export.authorization"`.
- A `SupervisorAssignment` carries `scopes: {...}`.
- Graph artifacts (`SPEC-009`, `TASK-102`) reference *attributes* (`{format: csv}`,
  `{audience: admin_only}`), **not files.**

A tool call is **physical**: `Edit {file_path: "src/export/csv.py"}`,
`Bash {command: "..."}`.

There is **no existing scope→file mapping anywhere in the repo.** So the entire feasibility
of "decide per tool call" reduces to one question:

> **Where does the bridge from `export.authorization` → concrete path-globs / command-patterns
> come from?**

Everything else is plumbing.

---

## 5. Potential implementations (ranked)

Three ways to "read the device," all respecting the privacy constraint (read locally,
transmit only the allowlist).

| Option | What it reads locally | Value | Risk |
|---|---|---|---|
| **A. Tool-call-level scope enforcement** | `tool_input` (file path / command) → match against invalidated scopes' path-globs | **High** — makes the hook match the scope-sensitive invariant; out-of-scope work flows during an interrupt | Med — more logic in a fail-open-critical path; matcher must be deterministic and fail closed on ambiguity |
| **B. Transcript-aware "deny once"** | `transcript_path` (JSONL) locally, to confirm the redirect instruction landed before re-allowing | Med — hardens the "deny once, then allow" guarantee against races | Low–Med |
| **C. Richer session binding** | `git status` / HEAD locally at session-start (branch already read) to detect when the session switches task | Low–Med — better assignment binding | Low |

**Recommendation: build A.** It is the honest completion of the product's core promise. B
and C are worthwhile but secondary; they can layer on later.

---

## 6. Deep dive: Option A

### 6.1 Architecture (respects the privacy invariant)

```
Decision lands → server derives, from a declared ScopeBinding,
                 a matcher for the invalidated scopes   ── graph-derived, deterministic
        │
        ▼  (server → hook, at session-start + on deny)    ← config flowing OUT is fine
   matcher = { "export.authorization": {globs:[...], cmd_patterns:[...]} }
        │
        ▼  PreToolUse fires on device
   hook reads tool_input LOCALLY, applies matcher LOCALLY
        │
        ├─ touches invalidated scope → DENY once (existing provenance/redirect)
        └─ doesn't touch it          → ALLOW  (out-of-scope siblings survive per-call)
        │
        ▼  wire still carries only session_id + tool_name + timestamp
```

The reading and matching happen **on-device, in the hook process.** The only thing crossing
server→hook is the matcher (config, not user data). `tool_input` never leaves the machine.

### 6.2 The pivotal decision: where the ScopeBinding comes from

| Option | How scope→path is authored | Determinism | Cost |
|---|---|---|---|
| **1. Declared config** (`scope-bindings` on the workspace, versioned with the graph) | A human writes `export.authorization: {globs: ["**/export/**", "**/csv*"], cmds: ["*flask*export*"]}` | Fully deterministic, auditable, matches "a human confirms" | Someone authors it per workspace |
| **2. Graph-derived** | Infer paths from artifact attributes | Only as good as the attributes — today they carry no paths, so ~empty | Low value now |
| **3. LLM-proposed matcher** | Model suggests globs from decision text | ❌ Violates the invariant — model output would gate a tool call | Don't |

**Recommendation: Option 1.** The only one that keeps *"deterministic code decides, a human
confirms."* The binding becomes a small, reviewable artifact shipped alongside the scope
definitions. For the demo you author exactly one (`export.authorization`). Option 2 can
layer on later **as a suggestion to the human author**, never as the gate.

### 6.3 Component changes (planning, not code)

1. **New concept — `ScopeBinding`** (`workspaces/`): `scope → {path_globs, command_patterns}`.
   Pure, typed, unit-testable off the hot path. Conservative by construction:
   **empty / ambiguous binding → treat as "touches" → deny.**
2. **Server — `ClaudeHookVerdict`** (`session_enforcement.py:88`): add one optional field
   (e.g. `scope_matcher`) carrying the invalidated scopes + their globs. Populated when the
   assignment is interrupted. Purely additive.
3. **Enforcement — `check()`** (`session_enforcement.py:300`): decision *logic* unchanged, but
   when it would deny, it also attaches the matcher. It still never sees `tool_input`.
4. **Hook** (`hooks/claude_code_hook.py`): the new local layer.
   - Fetch matcher at **session-start** (stable — cache it). Interrupt *state* still comes
     fresh per `PreToolUse`.
   - On a would-be deny: read `tool_input` locally, apply matcher; in-scope → deny once,
     out-of-scope → allow.
   - Wrap all new logic so **any exception → the existing `_fail_closed_deny`.** The current
     catch-error path stays as the backstop; it simply stops being the *only* logic.

### 6.4 Decision table (the whole behavior)

| Assignment state | Tool call touches invalidated scope? | Verdict |
|---|---|---|
| Bound / current | — | allow (unchanged) |
| Interrupted, not acknowledged | **yes** | **deny once** (+ provenance / redirect) |
| Interrupted, not acknowledged | **no** | **allow** ← the new capability |
| Interrupted, not acknowledged | matcher missing / errors / ambiguous | **deny** (fail closed) |
| Acknowledged | any | allow (unchanged) |

### 6.5 Two subtleties to decide up front

- **Caching.** The on-disk cache currently stores the final allow/deny. With local matching,
  cache the **server's** contribution (interrupt state + matcher); compute the final decision
  locally each call. The service-outage rule stays: last known "interrupted" + service down
  → deny. Be explicit so a stale **allow** is never cached.
- **"Deny once" semantics.** Keep the interrupt **assignment-level**; the matcher only filters
  *which* calls it applies to. Acknowledgement still clears at the assignment level. Do not let
  "once" drift into "once per file."

### 6.6 Test plan (maps to the Definition of Done)

- out-of-scope tool call in an interrupted session → **allowed** (the new proof)
- in-scope tool call → **denied once**, next allowed after acknowledge
- wire payload still contains no `tool_input` / file contents / transcript (extends the existing
  privacy test)
- matcher raises / missing / ambiguous → **deny** (fail-closed unit test)
- `FixtureSupervisorRuntime` still `SIMULATED`; all existing tests pass unchanged

### 6.7 Suggested sequencing (thin, safe slices)

- **M0** — author `ScopeBinding` type + one binding for the demo workspace. No behavior change.
- **M1** — server attaches the matcher to deny verdicts. Additive, still session-level. Green.
- **M2** — hook applies the matcher locally with the fail-closed wrapper. Behavior change lands here.
- **M3** — the tests above. Done.

### 6.8 Honest risks

- The hook is the **fail-open-critical** component; every added line is a liability.
  Mitigation: matcher is a pure function tested separately; all new hook code sits inside a
  try→deny wrapper.
- Command matching (`Bash`) is inherently fuzzier than path matching (`Edit` / `Write`).
  Consider shipping M2 for path-based tools only first, and treating `Bash` as "always
  in-scope during an interrupt" (deny) until the command-pattern matcher earns trust.
- You add an artifact humans must author. That's a **feature** (auditable) but also demo-day work.

---

## 7. Open decisions

1. **Authoring model** — Option 1 (declared `ScopeBinding`), or explore graph-derived-as-
   suggestion earlier?
2. **M2 scope** — path-based tools only first (`Edit` / `Write` / `Read` / `MultiEdit`), with
   `Bash` deny-by-default? Or attempt command matching in the first cut too?
3. **Demo framing** — lead with per-call precision, or keep the blunt session freeze for the
   demo and treat this as a post-demo hardening item?

---

## 8. The alternative: don't build it

Worth stating for balance. The blunt session-level freeze is **simpler, already tested, and
already fail-closed.** For a 3–5 minute demo, "one decision, five agents corrected" reads
cleanly without per-call precision. This enhancement adds real complexity to the most
safety-critical component in the system. If demo day is close, the rational move may be to
**ship this doc, not the code**, and build A afterward when there's time to harden it.
