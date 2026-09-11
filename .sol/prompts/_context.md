# Shared context — inherited by every track prompt

Sections 2 (Ground truth), 3 (Track ownership), 5.0 (Standing rules) and the
**Do not do** appendix of `EXECUTE.md`, verbatim. This file outranks your instincts
about this codebase. `AGENTS.md` outranks this file.

---

## 2. Ground truth for this repo

Everything here was re-derived from the repository at `5103a44` (branch `main`,
clean tree). Where it corrects the brief or the source implementation plan, that is
marked **[correction]**. Line numbers below were read, not remembered.

### 2.1 Shape

```
backend/writai/    __init__.py cli.py(ROUTES at :37) cli_approve.py cli_dev.py
                   config.py demo.py doctor.py domain.py fixtures.py grants.py
                   hashing.py provenance.py runtime.py supervisor_contract.py
                   terminal.py crustdata_demo.py
  auth/            hexclave.py hexclave_webhooks.py
  authority/       __init__.py engine.py
  graph/           __init__.py base.py memory.py neo4j_store.py
  intake/          approval.py crustdata.py decisions.py gate.py replay.py slack.py
  integrations/    callwright.py
  llm/             anthropic_adapter.py extractor.py gemini_adapter.py provider.py
                   venice_adapter.py
  loop/            workflow.py
  notify/          email.py escalate.py escalation_source.py push.py slack.py
  scenarios/       authority_contexts.py catalog.py models.py repository.py
                   run_models.py runner.py transport.py validation.py
  services/        agent_api.py authority_api.py escalation_api.py events.py
                   executor_api.py supervisor_api.py support.py
  workspaces/      authority_contexts.py interrupt_port.py live_interrupt.py
                   models.py orchestrator.py repository.py runtimes/
                   session_binding.py session_enforcement.py supervisor.py transport.py
backend/tests/     63 test_*.py + conftest.py + fixtures/     -> 772 collected
frontend/src/      App.tsx api.ts types.ts main.tsx styles.css
                   approvals/ hexclave/ live-workspace/ scenario-lab/   -> 181 tests, 15 files
hooks/             three stdlib-only scripts + writai_hook_lib.py
scripts/           bootstrap.sh check.sh run_stack.sh run_demo.sh run_services.sh
                   ci/{writai_ci_check.py,test_writai_ci_check.py,verify.sh,README.md}
                   demo/{seed.py,up.sh,ack.sh,fire.sh,reset.sh,...}
.github/workflows/ writai-authorization.yml  writai-pr-authorization.yml
outputs/           OPEN-ITEMS-REGISTER.md   (96 lines - a real self-audit)
root               9 .md files; 47 .md in the whole repo
```

**[correction]** The source plan says 48 markdown files; the brief's 47 is right.

### 2.2 The gate

```bash
bash scripts/check.sh
```

Identical to `make check` (`Makefile:17-24`) except it uses `npm --prefix frontend`
instead of `cd frontend && npm`. Seven steps, in order: pytest, ruff, mypy,
compileall, `npm test`, `npm run typecheck`, `npm run build`. `set -euo pipefail`, so
it stops at the first red step.

**This is the gate for every phase in every track.** It must exit 0 before any
commit, from T0.1 onward. `make test` (`PYTHONPATH=backend python3 -m pytest`) is the
fast inner loop; it is not the gate.

**Every command in this file assumes `.venv` is active.** Without it,
`python3 -m ruff` / `-m mypy` fail with `No module named ruff` even when `ruff` is on
`PATH`.

### 2.3 What runs without credentials

| Command | Result | Env vars needed |
|---|---|---|
| `make demo` | **exit 0**, full six-stage proof, ends `writ.ai proof complete` | **zero** |
| `python3 -m ruff check backend` | "All checks passed!" | zero |
| `python3 -m mypy backend` | 140 source files | zero |
| `npm --prefix frontend run typecheck` | exit 0 | zero |
| `npm --prefix frontend run build` | exit 0 | zero |
| `python3 -m pytest` | red, see 2.4 | zero |
| `npm --prefix frontend test` | red, see 2.4 | zero |
| `writai doctor` | six probes, all "not configured" | zero |

**No phase requires** `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `HEXCLAVE_*`,
`COMPOSIO_*`, `SLACK_*`, `CRUSTDATA_*`, or `CALLWRIGHT_*`. Neo4j comes from
`docker-compose.yml` (`neo4j:5-community`, auth `neo4j/writai-demo`) or a GitHub
Actions `services:` container, both free.

### 2.4 The red baseline — measured on this exact tree, and it is expected

`bash scripts/check.sh` is RED on a clean clone before T0.1. Measured:

| Gate step | Result |
|---|---|
| `PYTHONPATH=backend python3 -m pytest` | **RED - 8 failed, 761 passed, 3 skipped** (772 collected) |
| `python3 -m ruff check backend` | green |
| `python3 -m mypy backend` | green |
| `python3 -m compileall -q backend` | green |
| `npm --prefix frontend test` | **RED - 1 failed, 180 passed (181)** |
| `npm --prefix frontend run typecheck` | green |
| `npm --prefix frontend run build` | green |

The 8 backend failures are **two** independent causes, both reproduced and isolated:

1. **7 failures in `backend/tests/test_hexclave_webhooks.py`** - `pyproject.toml:17`
   pins `svix>=1.70` with no upper bound and a fresh install resolves **svix 2.x**,
   which changes webhook verification. The committed, unreferenced `uv.lock` already
   pins `svix==1.99.1`, the version that passes.
2. **1 failure in `backend/tests/test_hooks.py`** -
   `test_notify_denied_fires_once_per_deny` (**def at line 850, failing assert at 853**)
   passes `system="darwin"` without stubbing `shutil.which`, so
   `hooks/writai_hook_lib.py` returns `False` on any non-macOS host.

The 1 frontend failure is **`frontend/src/approvals/components.test.tsx`** -
`describe("ApprovalsHeader")` at line 237, the test at 238,
`vi.stubEnv("VITE_WRITAI_HEXCLAVE_SIGN_IN", "1")` at 239. It stubs one of the two
variables `frontend/src/hexclave/client.ts:57-62` `hexclaveSignInEnabled()` requires;
`vite.config.ts` sets `envDir: ".."`, so the author's gitignored root `.env` supplies
`VITE_HEXCLAVE_PROJECT_ID` and the test passes there. Observed failure text:
`expected '<header class="ap-header">...' to contain 'Sign in with Hexclave'`.

**[correction to the brief]** The brief says the backend failure is "tied to a
gitignored root `.env`". It is not. The `.env`-dependent failure is the **frontend**
one. The backend failure is a macOS-only `osascript` dependency. Both are test
defects, neither is a product defect, and they are fixed in different files - do not
conflate them.

**A pre-existing failure is not a reviewer finding.** Until T0.1 merges, any reviewer
that reports these as `BLOCKER`s is reporting the baseline.

### 2.5 Findings carried forward - every one re-verified at `path:line`

| # | Finding |
|---|---|
| **G1** | **One constant signs everything.** `config.py:66` - `grant_secret: str = os.getenv("WRITAI_GRANT_SECRET", "writai-local-demo-secret")`. It is the HMAC key for every `SignedGrant` (`grants.py:13-18` `GrantSigner.__init__`, which rejects only the *empty* string), the derivation key for the internal-service capability that gates every authority write route (`services/support.py:34-41` `internal_service_token`, enforced at `:44-52` `require_internal_service`), and the seed for every per-workspace context secret (`workspaces/authority_contexts.py:172-178` `_signing_secret`). No startup guard. `doctor.py` runs six probes (`gemini`, `hexclave`, `composio`, `callwright`, `crustdata`, `superset`) and not this one. `README.md` walks the reader through exposing the agent via ngrok. |
| **G2** | **Documented CLI commands are deprecated.** `cli.py:1392-1402` raises `CliError(code="COMMAND_DEPRECATED")` for `workspace approve-baseline` and `workspace approve-change`. `README.md:108` prints the first; `docs/LIVE_WORKSPACE_CLI.md:122,129,158,161` print both. `writai approve change WORKSPACE_ID DECISION_ID` and `writai approve pending` are the live replacements (`cli_approve.py:96-107`); `approve-baseline` has **no** CLI replacement. **[correction]** the brief says line 130 - it is **129**. |
| **G3** | **The enforcement hot path reparses the whole store.** `workspaces/repository.py:89-94` `get()` calls `_read()` (`:46-55`), which `json.loads` + `model_validate`s the entire document, then linear-scans. `session_enforcement.py:298-300` calls it inside `RepositorySupervisorAssignmentGateway.get`, reached on **every** `PreToolUse` hook. `config.py:165` defaults `WRITAI_HOOK_TIMEOUT_SECONDS` to 3 with the comment *"Hooks fail OPEN on timeout, so a long timeout silently disables enforcement."* Growth degrades into **silent enforcement loss**, not an error. Source plan's measurement: 0.84 ms at 1 workspace, 19.6 ms at 50, 97.8 ms at 200, **658.6 ms at 1000** - **re-measured in Phase B1 step 1 rather than trusted**. |
| **G4** | **Signed grants pass through a world-readable window.** `repository.py:57-67` `_write()` writes a temp file at the process umask, `temporary.replace(self.path)` carries that mode onto the target, and `self.path.chmod(0o600)` runs **after**. The document serialises `AuthorizationResult.grant` - a `SignedGrant` with a plain-string token. Guarded only by an in-process `RLock` (`:44`), so read-modify-write loses updates across processes - which `outputs/OPEN-ITEMS-REGISTER.md` A1-4 already admits. |
| **G5** | **Neo4j and memory are not the same store, and the README says they are.** `neo4j_store.py:145-153` `add_edge` runs `MATCH ... CREATE` and `.consume()`s it - a missing endpoint silently no-ops, where `memory.py:51-52` raises `KeyError`. `neo4j_store.py:115-118` `add_artifact` is a bare `CREATE (a:Artifact $props)` - duplicate ids permitted, where `memory.py:32-33` raises `ValueError`. `neo4j_store.py:140-143` `list_artifacts` has no `ORDER BY`, where `memory.py:47-48` returns dict insertion order, and `engine.py:333-352` `current_requirements()` resolves same-`effective_at` ties by that order. The existing "parity" suite `test_neo4j_integration.py` **sorts both sides** in `_canonical_graph` (`:40-57`) before comparing - it structurally cannot see an ordering divergence. `README.md:388-402` calls it parity. `outgoing_edges` (`:165-179`) *does* have `ORDER BY target_id, kind` at `:175`. |
| **G6** | **`apply_decision_change` has no transaction.** `authority/engine.py:181-196`: `add_artifact` (181) -> `add_edge` (182-190) -> `increment_version` (191) -> `_propagate_invalidation` (192-196), which calls `_mark_artifact` (`:206-216`) -> `update_artifact` once per invalidated node. A failure between 182 and the traversal leaves a superseding decision **and** its `SUPERSEDES` edge in the graph while every downstream artifact still reads `VALID`. **[correction]** the source plan says "wrap 181-198"; 197 and 198 are `report.graph_version = graph_version` and `self.last_report = report`, which are engine-local state and must stay **outside**. Wrap **181-196**. |
| **G7** | **The authority context registry grows monotonically.** `workspaces/authority_contexts.py:170` - `self._contexts: dict[str, _DynamicAuthorityContext] = {}`. Its only `delete` (`:249-257`) has exactly one caller, `orchestrator.py:270`, reached only when `_ensure_context` (`:265`) finds a **drifted** context and rebuilds it. Nothing releases a context when a workspace finishes. Each holds a full `MemoryGraphStore` + `IntentAuthority`. `create` is at `:180` and inserts at `:227`; registry `state()` at `:245`; the `evaluate_plan` call is at `:417` inside `authorize` (`:404`). **[correction]** the source plan cites `__init__` at 161-171 (it is **160**-171) and `state` at 234 (that is inside `_access`; the public `state` is **245**). |
| **G8** | **`last_report` bleeds across unrelated `/authorize` responses.** `engine.py:74` declares it as instance state; `:198` writes it on every applied change; `:458-466` reads it inside `evaluate_plan` to populate `invalidated_artifact_ids`, `preserved_artifact_ids`, `evidence_refs` and `invalidation_path`. `authority_api.py:140` builds **one** module-level `runtime`, and `/authorize` (`:1694-1695`) calls `runtime.authority.evaluate_plan(...)`. One client's decision change decorates another client's authorization response. `:459` reads `.affected_artifact_ids`, not `.invalidated_artifact_ids`. **[correction]** the source plan cites `:458-467`; it is **458-466**. |
| **G9** | **No rate limiting anywhere, and three publicly reachable POST routes.** `authority_api.py:440` `POST /webhooks/hexclave`; `authority_api.py:1555` `POST /intake/slack/{workspace_id}`; `agent_api.py:892` `POST /intake/crustdata/person/capture`. All three fail **closed** when unconfigured (503 / 503 / 422-then-bearer) - so they are credential-gated, not open. **[correction to the brief]** they are not "unauthenticated"; they are **unrate-limited**. The cost exposure is real: `intake/slack.py:72` accepts `text` up to `max_length=40_000`, and `llm/gemini_adapter.py:136-146` builds a `generationConfig` with `responseMimeType` and `temperature` and **no `maxOutputTokens`**. |
| **G10** | **One shared hook key, and any holder can acknowledge another developer's interrupt.** `services/supervisor_api.py:36` - `return cls(expected_api_key=os.getenv("WRITAI_HOOK_API_KEY", ""))`. `:133-150` `acknowledge` takes only a `session_id` and calls `verifier.require(hook_api_key)` then `enforcement.acknowledge(session_id=session_id)` - no owner check. Acknowledgement is the redirect-delivery confirmation that lets a denied session resume (`session_enforcement.py:521`). `ClaudeCodeSessionBinding` (`session_binding.py:87-100`) carries no owner. `.env.example` already describes the variable as *"Per-developer token ... identifies the session owner to the service"* - the docs describe a design the code does not implement. |
| **G11** | **Two logger call sites in the entire backend.** `grep -rn "logger\.\(info\|warning\|error\|exception\|debug\)" backend/writai` returns exactly two lines: `services/support.py:223` and `services/escalation_api.py:167`, both `.exception`. `support.py:75-92` mints and validates a correlation id for every request and returns it in the `X-Correlation-ID` header and every response body - and never writes it to a log. |
| **G12** | **`make check` has never run in CI.** `.github/workflows/` holds two workflows; `grep -ln "pytest\|npm test" .github/workflows/*.yml` returns **nothing**. |
| **G13** | **Nothing is pinned; the lockfile is decorative.** `pyproject.toml:10-18` sets floors and no ceilings. A 403 KB `uv.lock` is committed and referenced by **no** Makefile target, script, workflow or document. It pins the svix version that passes. |
| **G14** | **Dead code, precisely delimited.** `workspaces/live_interrupt.py` (`LiveClaudeCodeInterruptPort`) is used **only** by `backend/tests/test_claude_code_enforcement.py:24,241,302`. `supervisor_contract.py:35` (`NullSupervisorInterruptPort`) is used **only** by `backend/tests/test_supervisor_contract.py:7,62`. `workspaces/interrupt_port.py` is **not** dead - `scripts/demo/seed.py:47,479` uses `WorkspaceSupervisorInterruptPort`, and so does `backend/tests/test_claude_code_runtime.py`. `supervisor_contract.py` itself is live. `AGENTS.md:34` invariant 8 forbids deleting any of it. |

### 2.6 Premises this file corrects

| Brief / source plan says | Actually |
|---|---|
| backend failure is the `.env` one | backend failure is macOS `osascript`; the `.env` one is the **frontend** failure |
| `test_hooks.py:849` | `def` at **850**, failing assert at **853** |
| `components.test.tsx:237` | `describe` at **237**, the `it(...)` at **238**, `vi.stubEnv` at **239** |
| "unauthenticated public intake" | credential-gated and **fail-closed**; the defect is the missing rate limit (G9) |
| `interrupt_port.py` is dead | it is **live** - `scripts/demo/seed.py:47,479` (G14) |
| 47 markdown files (brief) / 48 (plan) | **47** - the brief is right |
| B8's tests go in `test_scenario_authority_contexts.py` | that file tests `ScenarioAuthorityContextRegistry` from `scenarios/authority_contexts.py`, a **different class**. The LRU tests belong in a new `backend/tests/test_workspace_authority_contexts.py` |
| wrap `engine.py:181-198` in a transaction | wrap **181-196** (G6) |
| `engine.py:458-467` | **458-466** (G8) |
| `authority_contexts.py` `__init__` 161-171, `state` 234 | **160**-171 and **245** (G7) |
| `_canonical_graph` at `test_neo4j_integration.py:39-58`, skip logic `:60-78` | **40-57** and **59-78** |
| `Makefile` check target at 22-30 | **17-24** |

### 2.7 Unverified premises - `PREMISE UNVERIFIED - skip and report` if they do not match

| Premise | Status | Who is affected |
|---|---|---|
| Backend line coverage ~85%; `neo4j_store.py` 38%, `escalation_api.py` 60%, `doctor.py` 61%, `grants.py` 97%, `engine.py` 95%, `config.py` 99%, `repository.py` 92%, `session_enforcement.py` 92% | **UNVERIFIED.** T0.4 step 1 measures and derives every floor from what it measures. No number in this file is a floor. | T0.4 |
| `get()` timings 0.84 / 19.6 / 97.8 / 658.6 ms at N = 1 / 50 / 200 / 1000 | **UNVERIFIED** - code path confirmed, numbers are the source plan's. B1 step 1 re-measures; C5 cites Track B's numbers, never these. | B1, C5 |
| The JSON store's temp file is created mode `0644` | **PARTIALLY VERIFIED** - the chmod-after-replace ordering at `repository.py:63-64` is read and confirmed; the observed mode depends on the process umask. B1's test asserts what it observes. | B1 |
| `neo4j.SummaryCounters` exposes `relationships_created` | **UNVERIFIED.** A2 step 2 requires confirming it before use, and naming the driver version in the report. | A2 |
| `CREATE CONSTRAINT ... IF NOT EXISTS` and a variable-length path query run on `neo4j:5-community` | **UNVERIFIED.** A2 step 1 and A4 step 3 both require running the query before relying on it. APOC is **not** installed on the community image by default. | A2, A4 |
| `WRITAI_DEMO_UNAUTHENTICATED_APPROVAL` exists and gates unauthenticated demo approval | **UNVERIFIED** - C4 step 1 requires grepping for it before documenting it. | C4 |
| `frontend/src/approvals/components.test.tsx` renders `class="ap-header__signin"` | **UNVERIFIED** - T0.1 step 8 requires reading the component's JSX and asserting on the class it actually emits. | T0.1 |
| `orchestrator.py:488` is "the shipped supervisor-contract path" | **DOES NOT MATCH.** Line 489 is `def _apply_supervisor_invalidation`; `_enforce_supervisor_interrupts` is at `:548`. Nothing depends on it; recorded so nobody cites it. | nobody |
| The frontend test count after T0.1 is 182 | **DERIVED, not measured.** Today it is 181 (1 failed, 180 passed); T0.1 fixes one and adds none, so expect **181 passed**. | T0.1 |

---

## 3. Track ownership - authoritative

**This table is hard: edit nothing outside your row.** A path a track's prompt requires
but this table omits makes that task unexecutable - that is an escalation to the
orchestrator, not a licence to edit.

| Track | Builder | Reviewer | Owns outright | May extend (additive only) |
|---|---|---|---|---|
| **T0 - Foundation** | Fable | Sol | `pyproject.toml`, `Makefile`, `scripts/check.sh`, `scripts/ci/coverage_floors.py` *(new)*, `.github/workflows/writai-check.yml` *(new)*, `.gitignore`, `.env.example`, `backend/writai/config.py`, `backend/writai/doctor.py`, `backend/tests/test_runtime_config.py`, `backend/tests/test_doctor.py`, `backend/tests/test_hooks.py`, `frontend/src/approvals/components.test.tsx`, `.sol/**` *(new)* | **T0 only, on a quiesced tree:** one `require_production_secrets()` call line in each of `backend/writai/services/authority_api.py`, `agent_api.py`, `executor_api.py`. **That line and nothing else** in those three files. |
| **A - Authority & graph** | **Sol** | Fable | `backend/writai/authority/**`, `backend/writai/graph/**`, `backend/writai/loop/**`, `backend/writai/scenarios/authority_contexts.py`, `backend/tests/test_authority.py`, `backend/tests/test_authority_guards.py`, `backend/tests/test_selective_invalidation.py`, `backend/tests/test_neo4j_integration.py`, `backend/tests/test_neo4j_store.py`, `backend/tests/test_graph_store_contract.py` *(new)*, `backend/tests/review/**` *(new, Fable's review tests)* | `outputs/OPEN-ITEMS-REGISTER.md` - **append one block at the end only** |
| **B - Workspaces & enforcement** | **Fable** | Sol | `backend/writai/workspaces/**`, `backend/writai/services/authority_api.py`, `backend/writai/services/agent_api.py`, `backend/writai/services/supervisor_api.py`, `backend/tests/test_live_workspaces.py`, `backend/tests/test_service_concurrency.py`, `backend/tests/test_claude_code_enforcement.py`, `backend/tests/test_claude_code_runtime.py`, `backend/tests/test_supervisor_runtime.py`, `backend/tests/test_workspace_authority_contexts.py` *(new)*, `backend/tests/review/**` *(new, Sol's review tests)* | `outputs/OPEN-ITEMS-REGISTER.md` - append one block at the end only |
| **C - Service edge & docs** | **Sol** | Fable | `backend/writai/services/support.py`, `backend/writai/services/executor_api.py`, `backend/writai/llm/gemini_adapter.py`, `backend/tests/test_service_resilience.py`, `backend/tests/test_gemini_extraction.py`, `backend/tests/test_cli.py`, `README.md`, `docs/LIVE_WORKSPACE_CLI.md`, `backend/tests/review/**` *(new, Fable's review tests)* | `outputs/OPEN-ITEMS-REGISTER.md` - append one block at the end only |
| **T1 - Integration** | Fable | Sol | nothing new; runs on the merged tree | one line in `backend/writai/workspaces/authority_contexts.py:417`, one paragraph in `README.md`, one block in `outputs/OPEN-ITEMS-REGISTER.md` - **those three and nothing else** |

**Owns outright** - create, rewrite, delete freely.
**May extend (additive only)** - a shared file the track needs but does not own.

### 3.1 Shared files, and why each is safe

- **`backend/writai/config.py` is T0's, and it is frozen after T0.3.** Every new
  `Settings` field any track needs - `log_level`, `rate_limit_enabled`,
  `public_intake_rate_limit_per_minute`, `gemini_max_output_tokens`,
  `max_authority_contexts`, and the `workspace_store` default - is added **once, in
  T0.3, before any worktree exists**, and no track touches the file afterwards. Tracks
  read `settings.<field>`; none of them declares one.
- **`.env.example` is T0's**, documented in the same phase, for the same reason.
- **`.github/workflows/writai-check.yml` is T0's.** T0.4 creates **both** jobs -
  `check` and `neo4j`. Track A makes the second job go green by adding tests; it never
  edits the workflow. **No track may touch `writai-authorization.yml` or
  `writai-pr-authorization.yml`.**
- **`outputs/OPEN-ITEMS-REGISTER.md` is append-only for everyone.** Each track appends
  exactly one block at the **end**, under a heading naming itself
  (`## Plan execution - Track A`). **No track reorders, reflows, or edits an existing
  entry.** This file is a genuine self-audit and is the single cheapest credibility
  asset in the repo - Track C's job is to *surface* it from the README, not rewrite it.

### 3.2 The one cross-track line, and why it is T1's

Track A's phase A5 changes `IntentAuthority.evaluate_plan` (`engine.py:354`) to take an
optional `report` parameter. The per-workspace call site at
`workspaces/authority_contexts.py:417` **must** then pass
`report=context.authority.last_report` or workspace `/authorize` responses silently
lose their provenance fields. That file is Track B's, and Track B's worktree does not
contain Track A's signature change - adding the argument there would raise `TypeError`
and turn Track B's gate red.

So: **A5 gives the parameter a `None` default so every existing call site keeps
compiling, and T1.1 adds the one explicit `report=` line on the merged tree.**

### 3.3 Paths no track may touch

| Path | Why |
|---|---|
| `backend/writai/llm/extractor.py:179-189` | the post-model field overwrite - the product's entire prompt-injection boundary |
| `.github/workflows/writai-pr-authorization.yml` (esp. `:44-72`) | the base-sha checkout; the PR head is data only |
| `backend/tests/test_selective_invalidation.py:129-216` | the order-independence proof; A4's regression oracle |
| `backend/writai/provenance.py` | `authority_edge_sort_key` and `select_primary_invalidation_path` - what makes the above pass |
| `backend/writai/workspaces/live_interrupt.py`, `supervisor_contract.py:35` | test-only, but `AGENTS.md:34` forbids deleting pre-existing dead code |
| `backend/writai/cli.py` | the deprecation at `:1392-1402` is **correct**; C4 fixes the docs, not the CLI |
| `AGENTS.md`, `CLAUDE.md` | twins; no phase here needs either |
| `uv.lock` | committed and unreferenced; T0.1 reads it, nobody edits it |

**`AGENTS.md` is the highest-priority instruction in this repository and this file is
subordinate to it.** Invariant 8 (`AGENTS.md:34`): *"Surgical changes only. Touch what
the task requires. Do not refactor adjacent code. Do not delete pre-existing dead code
- mention it. Match existing style."*

### 3.4 Why there is no frontend track

The entire frontend workload in this plan is **one `vi.stubEnv` line and one added
assertion** in `frontend/src/approvals/components.test.tsx`, which is a baseline fix
and belongs in T0. `frontend/**` is **T0's for that one file and no track's
thereafter**.

---

## 5.0 Standing rules - inherited by every prompt

**1 - Evidence or it isn't a finding.** A reviewer finding is actionable only with one
of: a failing test committed to `backend/tests/review/` that passes once fixed, or a
specific `file:line` plus a concrete failure scenario (inputs or state -> the wrong
output or crash). "This could be cleaner", "consider extracting a helper", "might be a
perf concern" are **observations**, go in the `observations` array of
`findings.schema.json`, and nobody is obliged to act.

**2 - Reviewers do not edit.** The reviewer writes tests and findings. The builder
fixes their own code. A reviewer that edits implementation creates work nobody reviewed.

**3 - Ownership is hard.** Edit nothing outside your row in section 3. A needed path the
table omits is an escalation, not a licence.

**4 - Severity governs what blocks.** `BLOCKER` = data loss, credential exposure,
crash on a normal path, **silent wrong answer** -> blocks. `MAJOR` = wrong behaviour on
a plausible path, missing error handling on a fallible call, resource leak -> blocks.
`MINOR` = cosmetic, naming, non-load-bearing duplication -> logged only.

The canonical `BLOCKER` shape in **this** repo is *a graph that asserts stale work is
authorized*. G6 (no transaction around `apply_decision_change`) and G3 (fail-open
enforcement that degrades with store size) both have it. Anything with that shape is a
`BLOCKER` too.

**5 - Two rounds maximum.** A third round means the disagreement is about design, not
correctness. Escalate.

**6 - No mush merges.** When builder and reviewer propose different solutions, the
orchestrator picks **one**. Implementing both "to be safe" produces layered defensive
code nobody endorsed.

**7 - The four failure modes you are watched for.** *Sycophantic collapse* - did the
rebuttal contain evidence or confidence? *Scope creep by review* - does the finding
trace to a line the builder changed (`traces_to_diff`)? *Test theatre* - does the
reviewer's test still assert the original behaviour (`test_still_asserts_original`)?
*Confident fabrication* - does the cited symbol, config key or driver attribute
actually exist? Grep before you file.

**8 - Skip and report; never improvise.** Every phase states what it expects to find.
**If a stated precondition is false - a file is missing, a line does not contain what
this prompt quotes, a command errors for a reason not anticipated here - stop that
step immediately.** Then:

1. Leave the tree clean for that step (Fable: `git checkout -- <file>`. Sol: revert
   your own edit by hand and say so).
2. Skip the remainder of the phase.
3. Append one entry to `outputs/OPEN-ITEMS-REGISTER.md` under your track's block:
   the phase ID, **the premise as this prompt wrote it**, what is actually there
   **with a real `path:line`**, and one sentence on whether the phase's goal still matters.
4. Continue to the next phase only if it does not depend on the skipped one.
5. List every skip in your final report.

Do not invent an alternative fix. Do not widen scope to "make it work anyway". Do not
delete or weaken a test to make a gate pass. **A skipped phase with an accurate note is
a correct outcome; a phase that quietly became something else is not.**

**9 - Before trusting a check that passed, confirm it CAN fail.** A command that
returns empty for a reason unrelated to your claim is not evidence. Every Verify block
states its expected output. Where a phase gives a *positive control*, run it.

**10 - Name the test that goes red when you revert THAT LINE.** Not the subsystem -
the line. If the suite passes identically with and without your change, there is no
coverage of it.

**11 - Enumerate what you REMOVED.** Every test, assertion, branch, error handler or
invariant-bearing comment you deleted. **If nothing was removed, say so in those words.**

**12 - Surface every shim, stub, hardcoded value or synthetic id you introduce**,
unprompted, even when inconvenient.

**13 - One branch and one commit per phase.** Branch `phase/<id>-<slug>`, e.g.
`phase/a3-transactional-decision-change`. Commit subject <= 72 chars, imperative. Body:
2-5 bullets naming each file changed and why. End every commit message with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

Do not commit mid-phase. Do not push, open PRs, merge, squash or rewrite history unless
Ranjiv asks. **Fable does all of this. Sol does none of it.**

**14 - You do not certify your own work.** Hand over the diff and the reasoning.

**15 - `AGENTS.md` outranks this prompt.** Invariant 8 in particular: surgical changes
only; do not refactor adjacent code; do not delete pre-existing dead code - mention it.
`AGENTS.md` and `CLAUDE.md` must stay substantively identical; no phase here changes
either, and if you find yourself wanting to, that is an escalation.

**16 - The gate is `bash scripts/check.sh`, and it must exit 0 before every commit.**
Activate `.venv` first or `python3 -m ruff` fails with `No module named ruff`. Never
add `# noqa`, `pytest.skip` or a non-strict `xfail` to silence a real failure. The only
sanctioned `xfail`s in this plan are `strict=True` and are named explicitly where they
appear.

---

## Appendix - Do not do

Every prohibition here is load-bearing.

### Never weaken the post-model field overwrite at `llm/extractor.py:179-189`

Those eleven lines are the product's entire trust boundary. After an LLM proposes a
`DecisionMutation`, `source_ref` (180), `approval_status` (181), `authority_role` (182),
`confidence` (183), `effective_at` (184), `scopes` (185), `validity` (186),
`invalidated_scopes` (187), `supersedes_id` (188) and `affected_scopes` (189) are **all**
overwritten from the `TrustedDecisionContext` supplied by authenticated ingestion - and
only then does `authority.apply_decision_change` run.

Do not make any of those fields conditional. Do not "preserve the model's value when the
context is `None`". Do not add a fast path that skips the overwrite for a high-confidence
extraction. Do not move the overwrite behind a feature flag. Do not reorder it above the
`evidence_span_error` check at `:170-177` - that check is the second half of the same
defence and it must run first.

`AGENTS.md`'s product invariant - *the LLM may propose structure; deterministic code
decides and enforces; a human confirms* - is enforced **here and nowhere else**.

**No track in this plan owns this file.** If a phase appears to require editing it, the
phase is wrong.

### Never change the base-branch checkout in `.github/workflows/writai-pr-authorization.yml`

The checker is checked out from `github.event.pull_request.base.sha` into
`writai-checker/` with `persist-credentials: false` (lines 57-62), and the PR head goes
into `branch-under-review/` as inert data (lines 64-72). The file's own comment block
(lines 44-56) explains why, and says a cross-model review found the hole once already:
*"A required check that the candidate can rewrite is not a check."*

Do not simplify this to a single checkout. Do not run `scripts/ci/writai_ci_check.py`
from `$GITHUB_WORKSPACE`. Do not drop the `if: always()` self-test. Do not remove
`--require-binding`. **Do not merge this workflow with the new `writai-check.yml`** -
one deliberately runs base-branch code, the other deliberately runs the PR's, and that
difference is the whole point of each.

This is the best security engineering in the repository. Leave it alone.

### Never touch the order-independence proof at `test_selective_invalidation.py:129-216`

`test_equal_depth_primary_path_is_stable_when_edge_order_changes` builds the same graph
twice with the edge list reversed and asserts that `forward.paths ==
reversed_order.paths`, that `upstream_chain_artifact_ids` is exactly
`["DEC-018","DEC-004","SPEC-009","TICKET-100"]` both ways, and - at `:214-216` - that
`EVIDENCE-A` appears in **no** path, proving the kind filter holds.

It is Phase A4's regression oracle. **If a single character of it needs editing for the
batched traversal to pass, the batched traversal is wrong.** Revert and report.

Consequently: **do not touch `authority_edge_sort_key` or
`select_primary_invalidation_path` in `backend/writai/provenance.py`.** They are what
makes that test pass under reversed edge order. A4 batches the *reads*; the traversal
order is untouched.

### Do not delete pre-existing dead code

`AGENTS.md:34` invariant 8 is explicit: *do not delete pre-existing dead code - mention
it.* Specifically:

- `backend/writai/workspaces/live_interrupt.py` (`LiveClaudeCodeInterruptPort`) is used
  only by `backend/tests/test_claude_code_enforcement.py:24,241,302`. **Keep it.**
- `backend/writai/supervisor_contract.py:35` (`NullSupervisorInterruptPort`) is used only
  by `backend/tests/test_supervisor_contract.py:7,62`. **Keep it.** The module around it
  is live in eleven places.
- `backend/writai/workspaces/interrupt_port.py` is **not dead.**
  `scripts/demo/seed.py:47,479` uses `WorkspaceSupervisorInterruptPort`, and so does
  `backend/tests/test_claude_code_runtime.py`. Deleting it breaks the demo seeder.
- `uv.lock` is committed and consumed by nothing. T0.1 **reads** it to learn which svix
  version passes. Nobody edits or deletes it; moving `bootstrap.sh`/CI to
  `uv sync --locked` is the durable fix and it is out of scope - record it in the register.

### Do not make `make demo`, `make check` or `make stack` require configuration

They run today with zero environment variables - measured. T0.2's startup guard
must key off `WRITAI_ENV` using **the same environment set already hard-coded in
`_default_demo_reset_enabled` (`config.py:48-54`)**, so a fresh clone still runs.
Zero-config is the property that makes this repository reviewable in five minutes, and it
is worth more than any single hardening change in this plan.

### Do not remove the `xfail(strict=True)` markers on the JSON-repository parameters

The JSON store's O(n) `get()` and its world-readable write window are **documented,
intentional properties of a compatibility store**, not bugs to hide. A strict xfail is
how this plan proves the SQLite store fixed something, rather than that someone deleted
a test. A non-strict xfail proves nothing and is test theatre.

### Do not migrate the remaining `orchestrator.py` read-modify-write sites in B1

Three sites lose an **authorization decision** when a write is lost; the rest lose
display state. `AGENTS.md:34` says surgical. Enumerate the rest by line number in
`outputs/OPEN-ITEMS-REGISTER.md` and do them as their own change, with their own tests.

### Do not add a rate-limiting dependency, and do not add Redis

`slowapi` requires Redis to be more than per-worker - the **same** limitation as the
stdlib token bucket in C2, at additional cost and an additional supply-chain surface.
The per-worker caveat is documented honestly in the limiter's docstring. Add Redis only
when a deployment actually runs multiple workers **and** someone has measured that the
per-worker limit is insufficient.

### Do not raise `GEMINI_MAX_OUTPUT_TOKENS` above 2048 without measuring a real response

The extraction schema is small, and the cap is the only thing between a 40 000-character
Slack message (`intake/slack.py:72`) and an unbounded bill. C2's rate limit bounds the
request count; only C3 bounds the cost per request.

### Do not consolidate the 47 markdown files by deleting any of them

Phase C5 changes **navigation only**. `INTEGRATION_REPORT.md`, `HANDOFF.md`,
`ASSUMPTIONS.md`, `TASKS.md`, `writai.md`, `REPO_MANIFEST.md` and the lane docs are
build-history artefacts; a reviewer who wants them should find them, which is what the
`<details>` index is for. `AGENTS.md` and `CLAUDE.md` are twins that must be updated in
the same commit, and no phase in this plan updates either.

### Surface `outputs/OPEN-ITEMS-REGISTER.md`; do not bury it and do not rewrite it

96 lines of genuine self-audit: A1-1 through A1-5 with real severities and real reasons
for each being open, a "Fixed during A1 review" section naming four defects the author
caught and closed, and an "Environment" section admitting a trailing-slash bug that 502'd
every service-to-service call. **This is the most credible document in the repository and
it is in a directory nobody opens.** C5 links it from the first screen. Every track
appends to its **tail** under its own heading; nobody reorders, reflows or edits an
existing entry.
