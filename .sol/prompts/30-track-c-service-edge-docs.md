# TASK: Track C — The service edge, and what a reviewer sees in the first ninety
# seconds.

You are **Sol** (Codex `gpt-5.6-sol`). Work in the worktree at
`/work/writai-track-c`, branch `track/c-service-edge-docs`. Activate its venv
(`source .venv/bin/activate`).

**You never run a git command.** Not `add`, not `commit`, not `status`, not `diff`.
Leave your files on disk and describe what you changed; Fable commits.

Read `.sol/prompts/_context.md` first — verified ground truth, the ownership table, the
standing rules, and the **Do not do** list.

**You own outright:** `backend/writai/services/support.py`,
`backend/writai/services/executor_api.py`, `backend/writai/llm/gemini_adapter.py`,
`backend/tests/test_service_resilience.py`, `backend/tests/test_gemini_extraction.py`,
`backend/tests/test_cli.py`, `README.md`, `docs/LIVE_WORKSPACE_CLI.md`.
**You may append one block at the end of** `outputs/OPEN-ITEMS-REGISTER.md`.

You do **not** touch `backend/writai/config.py`, `.env.example`,
`.github/workflows/**`, `backend/writai/cli.py`, `backend/writai/services/authority_api.py`,
`backend/writai/services/agent_api.py`, or anything under
`backend/writai/workspaces/**`, `authority/**` or `graph/**`. T0 already added every
`Settings` field you need — read them off `settings`, never declare one.

**Frozen, and you will be tempted by two of them:**
- `backend/writai/llm/extractor.py:179-189` — the post-model field overwrite. **Not
  yours. Never opened.** See *Do not do* in `_context.md`.
- `backend/writai/cli.py:1392-1402` — the `COMMAND_DEPRECATED` raise is **correct**.
  Phase C4 fixes the documentation, not the CLI. Do not un-deprecate anything.
- `.github/workflows/writai-pr-authorization.yml` — not yours, and specifically not to
  be merged with `writai-check.yml`.
- The 47 markdown files. **Delete none of them.** C5 changes navigation only.

**Standing rule 8 applies to every step.** If a line does not contain what this prompt
quotes: stop that step, revert your own edit by hand, skip the rest of the phase, append
the skip to `outputs/OPEN-ITEMS-REGISTER.md`, and report premise versus reality with a
real `path:line`.

**Your gate is `bash scripts/check.sh` and it must exit 0 before you declare any phase
done.**

---

## Phase C1 — One structured log line per request, carrying its correlation id

**Why:** `grep -rn "logger\.\(info\|warning\|error\|exception\|debug\)" backend/writai`
returns **exactly two lines** in the entire backend: `services/support.py:223` and
`services/escalation_api.py:167`, both `.exception`. Meanwhile `support.py:75-92` mints
and validates a correlation id for **every** request, returns it in the
`X-Correlation-ID` header and in every response body — and never writes it to a log.
There is no way to answer "what happened at 14:02" on a running deployment. This is the
cheapest operability change in the repo and it is a prerequisite for C2: a rate limiter
you cannot observe is a rate limiter you cannot tune.

**Files:** `backend/writai/services/support.py`,
`backend/tests/test_service_resilience.py`

**Do:**

1. Add `def configure_logging() -> None:` to `support.py` and call it from
   `install_api_support` (`:239-244`), **before** the middleware is added. Use
   `logging.basicConfig` with a formatter emitting `timestamp`, `level`, `logger`,
   `message` and `correlation_id`. Take the level from `settings.log_level`
   (T0.3 added it; `WRITAI_LOG_LEVEL`, default `INFO`). Make it idempotent — three
   services import this module in one process during tests, and three
   `basicConfig` calls must not produce three handlers.
2. In `CorrelationIdMiddleware.__call__` (`:198-235`), record `time.perf_counter()` on
   entry, capture `message["status"]` inside the existing `send_with_correlation_id`
   closure (`:210-216`), and on completion emit exactly **one**
   `logger.info("request", extra={...})` carrying `correlation_id`, `method`, `path`,
   `status`, `duration_ms`.
3. **Log the route template, never the raw path.** Use `scope["route"].path` when
   present so a workspace id never lands in a log line; when it is absent (a 404 never
   matched a route) log the literal `"<unmatched>"`. **Never fall back to the raw path.**
   Log no headers, no body, no query string. Put that rule in a comment.
4. Keep the existing `logger.exception` at `:223` exactly as it is. Your `info` line is
   in addition to it, not instead of it.
5. Tests in `backend/tests/test_service_resilience.py`:
   - `test_every_request_writes_one_log_line_carrying_its_correlation_id` — with
     `caplog.at_level(logging.INFO)`, issue one `GET /health`, then assert **exactly
     one** record whose `correlation_id` equals `response.json()["correlation_id"]`
     (or the `X-Correlation-ID` header, whichever the contract uses — read
     `correlated_payload` at `:95-107` and match it).
   - `test_a_request_log_line_never_contains_a_workspace_id` — issue a
     `GET /live-workspaces/{id}` with a distinctive id such as
     `ws-secret-do-not-log`, then assert **no** emitted record's message or `extra`
     contains that string. This is the test that makes step 3 load-bearing.
   - `test_an_unmatched_route_logs_the_placeholder_not_the_path` — `GET /nope/ws-secret`
     → the record's `path` is `"<unmatched>"` and the string `ws-secret` appears nowhere.

**Acceptance:** Every request produces exactly one structured `INFO` line carrying its
correlation id, the route template, the status and the duration — and no user data.

**Verify:**
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_service_resilience.py -q
```
→ all passed.
```bash
PYTHONPATH=backend python3 - <<'PY'
import logging; logging.basicConfig(level=logging.INFO)
from fastapi.testclient import TestClient
from writai.services.authority_api import app
with TestClient(app) as c:
    r = c.get("/health")
print("status", r.status_code, "corr", r.headers.get("X-Correlation-ID"))
PY
```
→ one `INFO` line printed to stderr, and the correlation id in it matches the header.

**If it fails:** If `scope["route"]` raises `KeyError`, the request never matched — that
is exactly the `"<unmatched>"` case, handle it with `scope.get("route")`. If three
handlers appear in tests, `configure_logging` is not idempotent — guard on
`logging.getLogger().handlers` or use `force=False` deliberately and say which.

---

## Phase C2 — Rate-limit the three public intakes

**Why:** There is no rate limiting anywhere in this codebase, and three POST routes are
publicly reachable: `authority_api.py:440` `/webhooks/hexclave`,
`authority_api.py:1555` `/intake/slack/{workspace_id}`, `agent_api.py:892`
`/intake/crustdata/person/capture`.

**[correction to the brief — this matters for your tests]** All three fail **closed**
when unconfigured — 503 `SLACK_INTAKE_NOT_CONFIGURED`, 503
`HEXCLAVE_WEBHOOK_NOT_CONFIGURED`, and 422-then-bearer respectively. They are
**credential-gated, not unauthenticated.** The defect is that they are **unbounded**:
verification work, and on the Slack path model spend, is performed for every request an
anonymous caller sends. That fail-closed behaviour is what lets your tests run with
**zero credentials** — you assert on the 503 path.

**Files:** `backend/writai/services/support.py`,
`backend/tests/test_service_resilience.py`

**Do:**

1. Add `TokenBucketLimiter` to `support.py`: pure stdlib, keyed by
   `(route_template, client_host)`, backed by `dict[tuple[str, str], tuple[float, float]]`
   (tokens, last-refill) under an `RLock`, with **lazy eviction** of buckets idle beyond
   one refill window so the limiter cannot itself become the unbounded registry it
   exists to prevent. **No new dependency.** `slowapi` needs Redis to be more than
   per-worker, which is the same limitation at additional cost — say so in the docstring.
2. Add a module constant `PUBLIC_INTAKE_ROUTES: frozenset[str]` in `support.py` holding
   the three **route templates** literally: `"/webhooks/hexclave"`,
   `"/intake/slack/{workspace_id}"`, `"/intake/crustdata/person/capture"`.
   **Verify each against the decorators before you write it** —
   `grep -n '@app.post("/webhooks/hexclave")\|@app.post("/intake/' backend/writai/services/*.py`.
   Naming a route that does not exist is the fabrication failure mode.
3. Install the limiter as ASGI middleware from `install_api_support`, **before** route
   handling, so it precedes signature verification and bounds the verification work
   itself. On exhaustion raise
   `ApiError(status_code=429, code="RATE_LIMITED", message=..., retryable=True)` so it
   flows through the existing error contract (`error_payload` at `:127-139`) and carries
   a correlation id like everything else.
4. Read the limit from `settings.public_intake_rate_limit_per_minute` and the on/off
   switch from `settings.rate_limit_enabled` (T0.3 added both). Apply the limit **only**
   to `PUBLIC_INTAKE_ROUTES`; everything else is unlimited.
5. Document the per-worker caveat in the limiter's docstring, in the voice
   `config.py:9-24` uses: N uvicorn workers means N buckets, so the effective limit is
   N x the configured value; a shared limiter needs Redis and is out of scope. **An
   honest comment beats a silent overclaim.**
6. Expose a `reset()` on the limiter and call it from a test fixture, so ordering cannot
   leak between tests.
7. Tests in `backend/tests/test_service_resilience.py`:
   - `test_public_slack_intake_is_rate_limited` — with `TestClient(authority_api.app)`,
     POST `/intake/slack/ws-1` 61 times with body `b"{}"`. Assert the first 60 return
     `503` (the fail-closed, credential-free path) and the 61st returns `429` with
     `error.code == "RATE_LIMITED"`.
   - `test_the_rate_limit_precedes_signature_verification` — assert the 61st response is
     `429` and **not** `503`, proving the limiter runs before the verifier. This is the
     test that makes step 3's ordering load-bearing.
   - `test_authorize_is_not_rate_limited` — 100 POSTs to `/authorize` never return `429`.
   - `test_the_limiter_evicts_idle_buckets` — fill one bucket, advance the clock past a
     refill window with a monkeypatched time source, add a second key, and assert the
     first key's bucket is gone. Without this, step 1's eviction is unverified.

**Acceptance:** The three public intakes return 429 past the configured rate; no other
route is limited; the limiter's own memory is bounded.

**Verify:**
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_service_resilience.py -q
```
→ all passed.
```bash
PYTHONPATH=backend python3 - <<'PY'
from fastapi.testclient import TestClient
from writai.services.authority_api import app
with TestClient(app) as c:
    for _ in range(61):
        r = c.post("/intake/slack/ws-1", content=b"{}")
print("last status:", r.status_code, r.json()["error"]["code"])
PY
```
→ `last status: 429 RATE_LIMITED`.

**If it fails:** If the 61st request returns `503` instead of `429`, the middleware is
installed after routing — `install_api_support` adds middleware in reverse order of
execution, so check your ordering against `CorrelationIdMiddleware`. If it returns `429`
on the **first** request, the bucket starts empty rather than full — initialise it at
capacity. **Do not add Redis, and do not add a rate-limiting dependency** — see
*Do not do*.

---

## Phase C3 — Cap the Gemini output

**Why:** `intake/slack.py:72` accepts `text: str = Field(min_length=1, max_length=40_000)`,
and `llm/gemini_adapter.py:136-146` builds a `generationConfig` carrying
`responseMimeType` and `temperature` and **no `maxOutputTokens`**. A single
credential-holding caller can drive unbounded model spend, and C2's rate limit bounds
the request count but not the cost per request. The extraction schema is small; the cap
is the only thing between a 40 000-character Slack message and an unbounded bill.

**Files:** `backend/writai/llm/gemini_adapter.py`,
`backend/tests/test_gemini_extraction.py`

**Do:**

1. In `_generate`'s payload (`gemini_adapter.py:136-146`), add
   `"maxOutputTokens": self._max_output_tokens` to the `generationConfig` dict, beside
   the existing `responseMimeType` and `temperature`. Leave the existing comment about
   `responseSchema` exactly where it is — it explains a real constraint.
2. Set `self._max_output_tokens` in `__init__` from a keyword parameter defaulting to
   `settings.gemini_max_output_tokens` (T0.3 added it; `GEMINI_MAX_OUTPUT_TOKENS`,
   default `2048`). Match the way the adapter already takes `base_url` and `model`.
3. Add a one-line comment above it: the default is 2048 because the extraction schema is
   small, and **raising it requires measuring a real response first** — see *Do not do*.
4. Test in `backend/tests/test_gemini_extraction.py`:
   `test_the_gemini_request_caps_its_output_tokens` — capture the posted payload with
   the file's existing transport-stubbing idiom and assert
   `payload["generationConfig"]["maxOutputTokens"] == 2048`. Add a second assertion that
   `responseMimeType` and `temperature` are **still** present and unchanged, so the cap
   cannot be added by replacing the config.

**Acceptance:** No Gemini call leaves this codebase without an output cap.

**Verify:**
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_gemini_extraction.py -q
```
→ all passed.
```bash
grep -n "maxOutputTokens" backend/writai/llm/gemini_adapter.py
```
→ exactly one line, inside `generationConfig`.

**If it fails:** If the adapter has no `__init__` parameter list you can extend, read
how it is constructed (`grep -rn "GeminiAdapter(" backend/writai`) and match that call
shape. Do not read `os.getenv` directly in the adapter — every other setting in this
codebase comes through `Settings`, and a second convention is its own defect.

---

## Phase C4 — Make every documented CLI command executable, and keep it that way

**Why:** `README.md:108` tells the reader to run
`writai workspace approve-baseline refund-operations --role finance-admin`, and
`docs/LIVE_WORKSPACE_CLI.md:122,129,158,161` tell them to run that **and**
`writai workspace approve-change`. Both raise
`CliError(code="COMMAND_DEPRECATED")` at `cli.py:1392-1402` and exit 2 — verified by
running it. **The very first thing a reviewer following the README does is hit a wall.**
`approve-change` has a working replacement — `writai approve change WORKSPACE_ID
DECISION_ID`, registered at `cli_approve.py:96-107` alongside `writai approve pending`.
`approve-baseline` has **none**: approval now requires a Hexclave-resolvable
`approval_token`, and no local CLI has one.

**The CLI is right and the documentation is wrong.** Do not un-deprecate anything.

**Files:** `README.md`, `docs/LIVE_WORKSPACE_CLI.md`, `backend/tests/test_cli.py`

**Do:**

1. In `README.md:104-112`, replace the `approve-baseline` line with the two paths that
   actually work: the authenticated Workspace UI at `http://127.0.0.1:5173/` after
   `make stack`, and — for a machine without Hexclave — `scripts/demo/up.sh` with
   `export WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1`. **Confirm that variable exists**
   (`grep -rn "WRITAI_DEMO_UNAUTHENTICATED_APPROVAL" scripts/ backend/`) before you
   document it; if it does not, that is a rule-8 stop. Keep the `import` and `authorize`
   lines — both work.
2. In `docs/LIVE_WORKSPACE_CLI.md` do the same for the `approve-baseline` at `:122`, and
   replace the `approve-change` block at `:129-130` with
   `writai approve change refund-operations DEC-REFUND-002`.
3. In the `## Commands` block at `:155-166`, remove the two deprecated lines (`:158`,
   `:161`) and add the two that `cli_approve.py` actually registers:
   `writai approve pending [--workspace WORKSPACE_ID]` and
   `writai approve change WORKSPACE_ID DECISION_ID`.
4. Add a short subsection to `docs/LIVE_WORKSPACE_CLI.md` titled
   **"Why baseline approval has no CLI"**: approval requires a Hexclave-resolvable
   `approval_token` carried in an `ApprovalAttemptEnvelope`, and no local CLI can mint
   one. **This is the honest version of a gap, and it reads better than a command that
   cannot run.** Cite the real `path:line` for the envelope — find it with
   `grep -rn "ApprovalAttemptEnvelope" backend/writai/services/agent_api.py`.
5. Add `backend/tests/test_cli.py::test_documented_workspace_commands_are_not_deprecated`.
   It reads `README.md` and `docs/LIVE_WORKSPACE_CLI.md`, extracts every line matching
   `^\s*(writai [^\\\n]*?)\s*\\?$`, takes the token after `workspace` where present, and
   asserts none is in `{"approve-baseline", "approve-change"}`.
   **Assert on a non-empty extraction first — `assert len(commands) >= 20` — so a regex
   that silently stops matching cannot make the test vacuous.**
6. Strengthen it: also assert that every extracted `writai <group> <command>` pair
   resolves against `cli.ROUTES` (`cli.py:37`) **or** against the `approve` subparser's
   registered names, so a future doc that invents a command fails the test rather than
   only a re-introduced deprecated one. Import `ROUTES` read-only; **do not edit
   `cli.py`.**

**Acceptance:** Every `writai …` command printed in `README.md` or
`docs/LIVE_WORKSPACE_CLI.md` reaches a live code path, and the test fails if anyone
reintroduces a deprecated or invented one.

**Verify:**
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_cli.py -q -k documented
```
→ `1 passed`.
```bash
PYTHONPATH=backend python3 -m writai.cli workspace approve-baseline x --role y >/dev/null 2>&1; echo $?
```
→ `2` — the command is **still correctly deprecated**; only the docs changed.
```bash
grep -rn "approve-baseline\|approve-change" README.md docs/LIVE_WORKSPACE_CLI.md
```
→ matches only inside the new "Why baseline approval has no CLI" prose, if at all.

**Positive control:** add `writai workspace approve-baseline foo --role bar` to a
scratch copy of `README.md`, run the test, confirm it goes **red**, remove it.

**If it fails:** If the extraction finds zero commands, the fenced blocks use a form
your regex misses — widen it and keep the `>= 20` floor. If it finds a deprecated
command in a file this prompt did not name, **fix that file too if you own it, and
record it if you do not** — do not narrow the test to two files.

---

## Phase C5 — README truth pass: what a reviewer sees in ninety seconds

**Why:** 47 markdown files, 9 at the repository root. A reviewer opens `README.md`,
is told at line 11 to read six documents **in order**, and leaves. `## Fastest start`
— the thing they should run — is at line **116**, two screens down. Meanwhile
`outputs/OPEN-ITEMS-REGISTER.md` is a **genuine, specific, self-critical audit** (96
lines, real severities, real trade-offs, a "Fixed during A1 review" section that names
four real defects) and it is buried in a directory nobody opens. **Surfacing it is the
single cheapest credibility gain available in this repository**, and it is far more
impressive than a README that claims everything works.

**Files:** `README.md`

**Do:**

1. Move `## Fastest start` (line 116) to **immediately after the tagline block**
   (lines 3-5), before `## Read first`. A reviewer must reach a runnable command within
   one screen.
2. In that block, keep the venv + `pip install -e ".[dev]"` lines and state the honest
   ordering with **measured** runtimes: `make demo` first (zero config, prints the
   six-stage proof), then `make check` (the full gate — pytest, ruff, mypy, compileall,
   vitest, tsc, vite build). **Time both yourself** and write the real numbers, so
   nobody's five minutes is consumed by an `npm run build` they did not expect.
3. Rewrite `## Read first` (lines 11-20). Replace the six-document reading order with
   **three** lines: what to run first (`make demo`), where the design lives
   (`docs/ARCHITECTURE.md`), and **where the known defects live**
   (`outputs/OPEN-ITEMS-REGISTER.md`). Move the existing six-item list into a
   `<details>` block titled "Full document index" and **add the remaining root files to
   it** — `ASSUMPTIONS.md`, `HANDOFF.md`, `INTEGRATION_REPORT.md`, `REPO_MANIFEST.md` —
   so nothing becomes unreachable. **Delete no file.**
4. Add a `## Known limits` section immediately before `## Repository layout` (line 403).
   Four bullets, **each with a real `path:line` and, where there is one, a number**:
   - **The default signing secret.** `config.py:66` defaults `grant_secret` to
     `"writai-local-demo-secret"`; that one constant signs every grant
     (`grants.py:13-18`), derives the internal-service capability
     (`services/support.py:34-41`) and seeds every per-workspace context secret
     (`workspaces/authority_contexts.py:172-178`). State plainly that `writai doctor`
     validated six integrations and not this one, and that a startup guard now refuses
     to boot outside development.
   - **The enforcement hot path.** `workspaces/repository.py:89-94` reparsed the whole
     JSON store on every `get()`, on `session_enforcement.py:298-300`, behind a
     3-second **fail-open** hook (`config.py:165`). Quote **the timings Track B
     measured** — ask the orchestrator for them; if Track B has not merged yet, write
     the sentence without numbers and leave a `TODO(T1): timings` marker for T1 to fill.
     **Do not invent numbers.**
   - **The world-readable write window.** `repository.py:57-67` — `chmod(0o600)` runs
     after `replace()`, over a file containing signed grant tokens.
   - **Store and graph parity.** `neo4j_store.py:115-118,140-143,145-153` diverge from
     `graph/memory.py` on duplicate ids, ordering and missing endpoints.
   End the section with a link to `outputs/OPEN-ITEMS-REGISTER.md` as the full register.
5. Correct `README.md:388-402` (`### Neo4j parity tests`). Narrow the claim, do not
   delete the section: the existing suite compares **content** after sorting both sides
   (`test_neo4j_integration.py:40-57`); **ordering and error** parity is covered by
   `backend/tests/test_graph_store_contract.py`. If Track A has merged, say parity is
   achieved and runs on every CI push; if it has not, say it is **not** currently
   achieved by the Neo4j backend. **Write whichever is true when you write it, and say
   in your report which you wrote.**
6. Add the CI badge for `.github/workflows/writai-check.yml` immediately under the
   tagline at line 5. Derive the URL from `git remote -v` — **ask Fable to run it for
   you**; you run no git command. If the remote is unknown, leave the badge out and say so.
7. **Delete none of the 47 markdown files.** `INTEGRATION_REPORT.md`, `HANDOFF.md`,
   `ASSUMPTIONS.md` and the lane docs are build-history artefacts; a reviewer who wants
   them should be able to find them. `AGENTS.md` and `CLAUDE.md` are twins and are not
   yours.

**Acceptance:** A reader who opens `README.md` reaches a runnable command in the first
screen and finds four known limits with `path:line` before they find them by reading
code.

**Verify:**
```bash
python3 - <<'PY'
import pathlib
r = pathlib.Path("README.md").read_text()
assert r.index("## Fastest start") < r.index("## Read first"), "Fastest start must come first"
assert r.index("## Fastest start") < r.index("## What already works")
assert "## Known limits" in r, "Known limits section missing"
for ref in ["config.py:66", "grants.py:13-18", "workspaces/authority_contexts.py:172-178",
            "workspaces/repository.py:89-94", "repository.py:57-67",
            "outputs/OPEN-ITEMS-REGISTER.md", "test_graph_store_contract.py"]:
    assert ref in r, f"missing reference: {ref}"
print("README OK")
PY
```
→ `README OK`.
```bash
bash scripts/check.sh; echo "EXIT=$?"
```
→ `EXIT=0`.

**If it fails:** If a cited line number no longer matches because an earlier phase moved
code, **re-derive it with `grep -n` and cite the true one. Never cite a line you have
not just read** — a README full of stale citations is worse than one with none.

---

## When Track C is done

Report, in this order:

1. The gate: `bash scripts/check.sh` exit code and the pytest/vitest tallies.
2. The exact `PUBLIC_INTAKE_ROUTES` strings you used and the `grep` output that
   confirmed each against its decorator.
3. The **measured** `make demo` and `make check` runtimes you wrote into the README.
4. Which version of the Neo4j paragraph you wrote (C5 step 5), and whether the CI badge
   went in.
5. Every `TODO(T1):` marker you left, and where.
6. Every file you touched, with one line on why.
7. Every step skipped under standing rule 8, with premise versus reality at a real
   `path:line`.
8. **What you removed.** If nothing, say "nothing was removed" in those words.
9. Every shim, stub, hardcoded value or synthetic id you introduced.
10. A one-line confirmation that `llm/extractor.py`, `cli.py`,
    `.github/workflows/writai-pr-authorization.yml` and all 47 markdown files are
    **unmodified / undeleted**, and that you touched no `config.py` or `.env.example`.

You have run no git command. Say so.
