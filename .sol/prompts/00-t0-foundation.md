# TASK: T0 — Foundation. A green gate, a guarded secret, a frozen config, and CI.

You are **Fable**. You own git on this project. Work on the main tree at
`/work/writ.ai`, on branch `phase/t0-foundation` — no worktree, nothing else is
running. Activate `.venv` first (`source .venv/bin/activate`); without it
`python3 -m ruff` fails with `No module named ruff` even though `ruff` is on `PATH`.

**T0 is the only stage permitted to touch files other tracks own**, and only for the
three one-line guard calls in step T0.2.4. It runs on a quiesced tree for exactly that
reason. Everything else you might be tempted to fix belongs to Track A, B or C: leave
it, and name it in your report.

You own outright: `pyproject.toml`, `Makefile`, `scripts/check.sh`,
`scripts/ci/coverage_floors.py` (new), `.github/workflows/writai-check.yml` (new),
`.gitignore`, `.env.example`, `backend/writai/config.py`, `backend/writai/doctor.py`,
`backend/tests/test_runtime_config.py`, `backend/tests/test_doctor.py`,
`backend/tests/test_hooks.py`, `frontend/src/approvals/components.test.tsx`,
`.sol/**`.

You may add **one line** to each of `backend/writai/services/authority_api.py`,
`agent_api.py`, `executor_api.py` — the `require_production_secrets()` call in T0.2.4
— and nothing else in those three files.

**Read `.sol/prompts/_context.md` first.** It carries the verified ground truth, the
ownership table, the standing rules and the **Do not do** list, and it outranks your
instincts about this codebase.

**Standing rule 8 applies to every step: if a precondition below is false, stop that
step, leave the tree clean for it, skip the rest of the phase, append the skip to
`outputs/OPEN-ITEMS-REGISTER.md`, and report what this prompt said you would find
versus what you actually found at that `path:line`. Do not improvise a replacement.**

**The gate before you start is RED, deliberately.** `bash scripts/check.sh` fails with
8 backend failures and 1 frontend failure. T0.1 is the phase that turns it green.
Do not treat the baseline red as your own defect, and do not let it stop you starting.

**Note on step 0:** the orchestrator has already written `.sol/prompts/_context.md` and
all five track prompt files, because they were transcribed from the authoritative plan
text rather than re-derived. They exist; **do not rewrite them.** Your job for step 0 is
only to confirm they are present and include them in the T0.1 commit.

---

## Phase T0.1 — `make check` green on a clean Linux clone

**Why:** Three test failures stand between "clone and run" and a fact. Two are
dependency resolution (`pyproject.toml:17` pins `svix>=1.70` with no ceiling; a fresh
install resolves svix 2.x, which changes webhook verification and fails 7 tests in
`backend/tests/test_hexclave_webhooks.py`). One is a host dependency
(`backend/tests/test_hooks.py` line 850 needs macOS `osascript`). One is an env-var
dependency (`frontend/src/approvals/components.test.tsx` line 239 needs
`VITE_HEXCLAVE_PROJECT_ID`, which `vite.config.ts`'s `envDir: ".."` picks up from the
author's gitignored root `.env`). A committed 403 KB `uv.lock` already pins
`svix==1.99.1` — the version that passes — and is referenced by no Makefile target, no
script, no workflow, no document. The repo ships the answer and never reads it.

**Files:** `pyproject.toml`, `backend/tests/test_hooks.py`,
`frontend/src/approvals/components.test.tsx`. `README.md` is **not** yours — leave it,
Track C rewrites it.

**Do:**

1. In `pyproject.toml`, add an upper bound to every runtime dependency in
   `[project].dependencies` (lines 10-18), keeping the existing floors exactly:
   `fastapi>=0.115,<1`, `uvicorn[standard]>=0.30,<1`, `pydantic>=2.8,<3`,
   `httpx>=0.27,<1`, `python-dotenv>=1.0,<2`, `PyYAML>=6.0,<7`, **`svix>=1.70,<2`**.
2. On the line immediately above the `svix` pin, add exactly this comment:
   `# <2: svix 2.x changes webhook verification and fails backend/tests/test_hexclave_webhooks.py`
3. Cap `[project.optional-dependencies]` the same way, floors unchanged:
   `graph = ["neo4j>=5.20,<7"]`, `agent = ["langgraph>=0.2,<1"]`,
   `llm = ["anthropic>=0.40,<1"]`, `composio = ["composio>=0.18.0,<1"]`,
   `full = ["neo4j>=5.20,<7", "langgraph>=0.2,<1", "anthropic>=0.40,<1"]`,
   `dev = ["pytest>=8.2,<10", "pytest-asyncio>=0.23,<2", "ruff>=0.6,<1", "mypy>=1.11,<3", "types-PyYAML>=6.0,<7"]`.
4. Recreate the venv so the caps actually resolve:
   `deactivate 2>/dev/null; rm -rf .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`.
   Confirm `python3 -c "import svix; print(svix.__version__)"` prints a `1.x`.
5. In `backend/tests/test_hooks.py`, change `test_notify_denied_fires_once_per_deny`
   (the `def` is at line 850) to take a `monkeypatch: pytest.MonkeyPatch` parameter and,
   as its first statement, `monkeypatch.setattr(lib.shutil, "which", lambda _name: "/usr/bin/osascript")`.
   **Leave the three existing assertions byte-identical**: `... is True`,
   `seen[0][0] == "osascript"`, `"display notification" in seen[0][2]`.
6. Immediately after it, add
   `def test_notify_denied_is_silent_when_the_notifier_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:`
   which sets `monkeypatch.setattr(lib.shutil, "which", lambda _name: None)`, builds
   `config = lib.HookConfig(notifications_enabled=True)` and `seen: list[Any] = []`,
   and asserts **both**
   `lib.notify_denied(config, "blocked", runner=seen.append, system="darwin") is False`
   **and** `seen == []`. This pins the host-independent contract the old test was
   accidentally exercising on Linux.
7. In `frontend/src/approvals/components.test.tsx`, inside
   `it("keeps Hexclave sign-in reachable when the approval queue is empty")` (line 238),
   immediately after the existing `vi.stubEnv("VITE_WRITAI_HEXCLAVE_SIGN_IN", "1")` at
   line 239, add
   `vi.stubEnv("VITE_HEXCLAVE_PROJECT_ID", "54514e09-6629-4265-88cc-85fbb4ad119e");`
   — the same value `frontend/src/hexclave/client.test.ts` already uses. The existing
   `finally { vi.unstubAllEnvs(); }` already cleans up.
8. In the same test, add a third assertion after the existing two:
   `expect(markup).toContain('class="ap-header__signin"');` so it pins the button it
   claims to pin, not only its label. If that class name is not what the component
   renders, read `ApprovalsHeader`'s JSX and assert on the class it actually emits —
   and say in your report which class you used.

**Acceptance:** `bash scripts/check.sh` exits 0 on a Linux host with **no** root `.env`
and **no** `osascript` on `PATH`, from a freshly created venv.

**Verify:**
```bash
python3 -c "import svix; print(svix.__version__)"
```
→ a `1.x` version, never `2.x`.
```bash
env -u VITE_HEXCLAVE_PROJECT_ID bash scripts/check.sh; echo "EXIT=$?"
```
→ `EXIT=0`, with `770 passed, 3 skipped` from pytest (772 collected + 1 new test = 773,
minus 3 skips) and `Tests  181 passed (181)` from vitest.

**Positive control — prove the svix cap has teeth before you trust it:**
```bash
pip install --quiet "svix==2.3.0" \
  && PYTHONPATH=backend python3 -m pytest backend/tests/test_hexclave_webhooks.py -q --tb=no; echo "EXIT=$?"
pip install --quiet -e ".[dev]"   # restore the capped resolution
```
→ the first command must be **non-zero with 7 failures**. If it passes, the 7 failures
have another cause and this phase's premise is wrong — stop and report.

**If it fails:** If `monkeypatch.setattr(lib.shutil, ...)` raises `AttributeError`,
`writai_hook_lib` binds `which` directly rather than the module — find the real name
with `grep -n "^import shutil\|from shutil" hooks/writai_hook_lib.py` and patch that.
If the frontend test still fails, print `markup` and check whether
`hexclaveSignInEnabled()` (`frontend/src/hexclave/client.ts:57-62`) gained a third
condition; if it has, **stop and report — do not stub past a new production gate.**
If pip still resolves svix 2.x, an already-installed copy is shadowing the resolution:
recreate the venv, never loosen the pin.

---

## Phase T0.2 — Refuse to start on the default signing secret

**Why:** `config.py:66` — `grant_secret: str = os.getenv("WRITAI_GRANT_SECRET", "writai-local-demo-secret")`.
That one constant is the root of three independent trust chains: every `SignedGrant`
HMAC (`grants.py:13-18`, whose only validation is that the secret is non-empty); the
internal-service capability that gates every authority **write** route
(`services/support.py:34-41` `internal_service_token`, enforced by
`require_internal_service` at `:44-52`); and every per-workspace context signing secret
(`workspaces/authority_contexts.py:172-178` `_signing_secret`). Anyone who has read
this public repository can mint a valid grant and call an internal write route against
any deployment that did not set the variable. There is no startup guard, and
`doctor.py` runs six probes — `gemini`, `hexclave`, `composio`, `callwright`,
`crustdata`, `superset` — and not this one. The README walks the reader through
exposing the agent via ngrok.

**Files:** `backend/writai/config.py`, `backend/writai/doctor.py`,
`backend/tests/test_runtime_config.py`, `backend/tests/test_doctor.py`,
plus **one line each** in `backend/writai/services/authority_api.py`, `agent_api.py`,
`executor_api.py`.

**Do:**

1. In `config.py`, add `DEFAULT_DEMO_GRANT_SECRET = "writai-local-demo-secret"`
   alongside the other module constants at lines 63-69 (`DEFAULT_AUTHORITY_THRESHOLD`,
   `DEFAULT_GEMINI_BASE_URL`, …), and change line 66 to
   `grant_secret: str = os.getenv("WRITAI_GRANT_SECRET", DEFAULT_DEMO_GRANT_SECRET)`.
2. Add `def require_production_secrets(config: Settings | None = None) -> None:` to
   `config.py`, defaulting to the module-level `settings` when `config is None`.
   It raises `RuntimeError` when
   `config.env.strip().lower() not in {"development", "demo", "local", "test"}` **and**
   `config.grant_secret == DEFAULT_DEMO_GRANT_SECRET`. The message must contain the
   literal string `WRITAI_GRANT_SECRET` and the fix:
   `python3 -c "import secrets;print(secrets.token_urlsafe(48))"`.
   **Reuse the exact environment set already hard-coded in `_default_demo_reset_enabled`
   (`config.py:48-54`)** — extract it to a module constant `DEMO_ENVIRONMENTS` and use
   it in both places, so there is one definition of "this is a demo machine".
3. In the same function, also raise `RuntimeError` when
   `len(config.grant_secret) < 32` and the environment is not in `DEMO_ENVIRONMENTS`.
   A set-but-trivial secret is the failure mode an equality guard alone misses.
4. Call `require_production_secrets()` at **module scope** in all three service modules,
   on the line immediately after `install_api_support(app)` —
   `authority_api.py:179`, `agent_api.py:134`, `executor_api.py:56`. Module scope, not
   a startup event handler: the process must not bind a port. **This is the only line
   you may add to those three files.**
5. Add a seventh probe to `doctor.py`'s `PROBES` mapping, key `"signing"`, name
   `"Grant signing secret"`. `ProbeStatus.LIVE` when a non-default secret of >= 32 chars
   is set; `ProbeStatus.INVALID` when `settings.env` is outside `DEMO_ENVIRONMENTS`
   and the default (or a short secret) is in use; `ProbeStatus.ABSENT` on a demo machine
   using the default. Fill `degrades_to` and `fallback` in the same voice as the
   existing six. `doctor.main` returns 1 on any `INVALID` (see the `main` body at
   `doctor.py:788+`), so a production machine on the default secret now fails preflight.
6. Tests in `backend/tests/test_runtime_config.py`, using that file's existing
   `replace(Settings(), **overrides)` idiom:
   - `test_production_refuses_to_start_on_the_demo_signing_secret` —
     `pytest.raises(RuntimeError, match="WRITAI_GRANT_SECRET")` for
     `env="production", grant_secret=config.DEFAULT_DEMO_GRANT_SECRET`.
   - `test_production_refuses_a_short_signing_secret` — same, for
     `env="production", grant_secret="short"`.
   - `test_development_keeps_the_zero_config_demo_secret` —
     `require_production_secrets(...)` returns `None` for each of `"development"`,
     `"demo"`, `"local"`, `"test"` on the default secret. Assert all four; a loop with
     one assertion is fine, a single case is not.
   - `test_production_accepts_a_real_signing_secret` — returns `None` for
     `env="production"` and `secrets.token_urlsafe(48)`.
7. In `backend/tests/test_doctor.py`, add
   `test_signing_probe_is_invalid_on_the_default_secret_in_production` asserting
   `run_probes(only=["signing"])[0].status is ProbeStatus.INVALID` under a monkeypatched
   production-like `settings`, and that the rendered output contains
   `WRITAI_GRANT_SECRET`.

**Acceptance:** `WRITAI_ENV=production` with the default secret makes all three
services fail to **import** with a `RuntimeError` naming the variable, and
`writai doctor` exits 1. `make demo`, `make check` and `make stack` are unchanged with
zero environment variables.

**Verify:**
```bash
WRITAI_ENV=production PYTHONPATH=backend python3 -c "import writai.services.authority_api" 2>&1 | tail -3
```
→ a `RuntimeError` whose text contains `WRITAI_GRANT_SECRET`. Repeat for
`writai.services.agent_api` and `writai.services.executor_api` — all three.
```bash
WRITAI_ENV=production WRITAI_GRANT_SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))") \
  PYTHONPATH=backend python3 -c "import writai.services.authority_api; print('boots')"
```
→ `boots`.
```bash
PYTHONPATH=backend python3 -m writai.demo >/dev/null && echo "demo still zero-config"
```
→ `demo still zero-config`.
```bash
PYTHONPATH=backend python3 -m writai.doctor --json \
  | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"
```
→ `7`.

**If it fails:** If a service still imports under `WRITAI_ENV=production`, the guard is
behind a lazy import — put the call at module scope, not inside a factory or an
`@app.on_event`. If the existing suite breaks, a test is constructing `Settings()` with
a production-like `WRITAI_ENV` leaked from the environment; **fix the test's isolation,
never the guard's condition.** If `PROBES` is not a dict keyed by name, read the real
structure and match it — do not add a parallel registry.

---

## Phase T0.3 — Freeze the config surface every track builds against

**Why:** Three tracks are about to run in parallel and they need new `Settings`
fields: Track B needs `max_authority_contexts` and a SQLite `workspace_store` default,
Track C needs `log_level`, `rate_limit_enabled`,
`public_intake_rate_limit_per_minute` and `gemini_max_output_tokens`. If each track
adds its own, three agents append to one frozen dataclass in three worktrees and every
merge conflicts. **Adding all of them once, here, before the worktrees exist, is what
makes three-way parallelism safe.** After this phase `config.py` and `.env.example`
are closed for the rest of the plan.

**Files:** `backend/writai/config.py`, `.env.example`,
`backend/tests/test_runtime_config.py`

**Do:**

1. Add these fields to the `Settings` dataclass (`config.py:57+`), in the style of
   the fields already there — `os.getenv` with an explicit default, and `_env_flag`
   for booleans:
   - `log_level: str = os.getenv("WRITAI_LOG_LEVEL", "INFO")`
   - `rate_limit_enabled: bool = _env_flag("WRITAI_RATE_LIMIT_ENABLED", True)`
   - `public_intake_rate_limit_per_minute: int = int(os.getenv("WRITAI_PUBLIC_INTAKE_RATE_LIMIT_PER_MINUTE", "60"))`
   - `gemini_max_output_tokens: int = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "2048"))`
   - `max_authority_contexts: int = int(os.getenv("WRITAI_MAX_AUTHORITY_CONTEXTS", "256"))`
2. Change the `workspace_store` default at `config.py:82-85` from
   `".writai/live-workspaces.json"` to `".writai/live-workspaces.sqlite3"`. Add a
   two-line comment above it stating that a `.json` suffix selects the legacy
   `JsonFileLiveWorkspaceRepository` and that Track B's repository migrates an existing
   sibling `.json` store on first start.
   **This is the one change here that alters runtime behaviour before its implementation
   lands.** Track B's Phase B1 supplies the SQLite backend and the suffix routing. Until
   B1 merges, `agent_api.py:162` constructs `JsonFileLiveWorkspaceRepository` against a
   `.sqlite3` path, which works (it is just a filename to the JSON store) but is
   confusing. **Therefore: make this change and immediately confirm
   `bash scripts/check.sh` is still exit 0.** If any test asserts on the literal
   `.json` default, that test is Track B's file — **skip this step, leave the default at
   `.json`, and record it for Track B's B1 step 7 instead.**
3. Add each new variable to `.env.example` with one explanatory line, in the section
   matching its subject. For `WRITAI_GRANT_SECRET` (currently a placeholder near the
   top), replace the placeholder value with the generation command from T0.2:
   `WRITAI_GRANT_SECRET=  # required outside development: python3 -c "import secrets;print(secrets.token_urlsafe(48))"`.
   For `WRITAI_PUBLIC_INTAKE_RATE_LIMIT_PER_MINUTE`, state in the comment that the
   limiter is **per uvicorn worker**, so N workers means N x the configured value.
4. Add `test_every_documented_env_var_has_a_settings_field` to
   `backend/tests/test_runtime_config.py`: parse `.env.example` for lines matching
   `^([A-Z][A-Z0-9_]*)=`, drop a small explicit allow-list of variables that are read
   outside `Settings` (the hook scripts' `WRITAI_HOOK_*`, the frontend's `VITE_*`, the
   `NEO4J_*` set — enumerate them literally, do not pattern-match), and assert every
   remaining name appears in `pathlib.Path("backend/writai/config.py").read_text()`.
   **Assert `len(names) >= 30` first** so a regex that silently stops matching cannot
   make the test vacuous.

**Acceptance:** Every `Settings` field any later phase needs exists now, with its
documented default, and no later phase edits `config.py` or `.env.example`.

**Verify:**
```bash
PYTHONPATH=backend python3 -c "
from writai.config import settings
for f in ('log_level','rate_limit_enabled','public_intake_rate_limit_per_minute',
          'gemini_max_output_tokens','max_authority_contexts','workspace_store'):
    print(f, '=', getattr(settings, f))
"
```
→ six lines, `INFO / True / 60 / 2048 / 256 / .writai/live-workspaces.sqlite3`.
```bash
bash scripts/check.sh; echo "EXIT=$?"
```
→ `EXIT=0`.

**If it fails:** If step 2 turns a test red, that is the signal described in step 2 —
revert the default to `.json`, note it, move on. If `_env_flag` is not in scope where
you add `rate_limit_enabled`, it is defined at `config.py:71-75`; use it, do not write
a second parser.

---

## Phase T0.4 — Run the gate in CI, and put a floor under coverage

**Why:** `.github/workflows/` holds two workflows and
`grep -ln "pytest\|npm test" .github/workflows/*.yml` returns **nothing**. 772 backend
and 181 frontend tests have never been executed by a machine other than the author's.
This is the single highest-signal change available: it converts an assertion in the
README into a green badge a reviewer can click. And coverage today has no gate at all,
so it can fall by forty points without a red build.

**Files:** `.github/workflows/writai-check.yml` (new),
`scripts/ci/coverage_floors.py` (new), `scripts/check.sh`, `pyproject.toml`,
`.gitignore`

**Do:**

1. **Measure before you gate.** Install `pytest-cov` and run:
   ```bash
   pip install "pytest-cov>=5.0,<8"
   PYTHONPATH=backend python3 -m pytest -q --cov=writai --cov-report=term --cov-report=json
   ```
   Record the `TOTAL` percentage and the per-file percentages for
   `writai/grants.py`, `writai/authority/engine.py`, `writai/config.py`,
   `writai/workspaces/repository.py`, `writai/workspaces/session_enforcement.py`,
   `writai/services/support.py`, `writai/services/supervisor_api.py`.
   **Every floor below is derived from what you measured, not from a number in this
   prompt.** The source plan expects ~85% total and 97/95/99/92/92/—/— per file; if what
   you measure differs by more than 3 points on any of them, use **your** number and say
   so in your report.
2. Add `"pytest-cov>=5.0,<8"` to `[project.optional-dependencies].dev` in
   `pyproject.toml`.
3. Add to `pyproject.toml`:
   ```toml
   [tool.coverage.run]
   source = ["writai"]
   branch = false
   omit = ["*/tests/*"]

   [tool.coverage.report]
   fail_under = <your measured TOTAL, rounded down, minus 1>
   exclude_lines = ["pragma: no cover", "if TYPE_CHECKING:", "raise NotImplementedError", "\\.\\.\\."]
   ```
   One point below the measurement, so the gate is real and does not go red on the
   commit that introduces it.
4. Change `addopts` in `[tool.pytest.ini_options]` (currently `"-q"` at
   `pyproject.toml:47`) to
   `"-q --cov=writai --cov-report=term-missing:skip-covered --cov-report=json"`.
   Both `make test` and `make check` pick it up with no Makefile change.
5. Write `scripts/ci/coverage_floors.py`. It reads `coverage.json`, and for each of the
   seven modules in step 1 compares `files[<path>]["summary"]["percent_covered"]`
   against a floor set to **your measured value minus 2**, rounded down. On any breach
   it prints one line per breached module in the form
   `FLOOR: writai/grants.py 91.2% < 95%` and exits 1; otherwise it prints
   `coverage floors OK (7 modules)` and exits 0. Take the floors from a module-level
   `FLOORS: dict[str, int]` so a future change is one edit.
6. Add `python3 scripts/ci/coverage_floors.py` to `scripts/check.sh` on the line
   immediately after the `pytest` invocation, so local and CI enforce identically.
7. Add `coverage.json` and `coverage.xml` to `.gitignore` (`.coverage` and `htmlcov/`
   are already there at lines 10-11).
8. Create `.github/workflows/writai-check.yml`. Trigger on `push` (branches `[main]`)
   and `pull_request`. `permissions: contents: read`. **Two jobs:**
   - `check` — `runs-on: ubuntu-latest`, `timeout-minutes: 20`. Steps:
     `actions/checkout@v4`; `actions/setup-python@v5` with `python-version: "3.11"` and
     `cache: pip`; `actions/setup-node@v4` with `node-version: "22"`, `cache: npm`,
     `cache-dependency-path: frontend/package-lock.json`; `pip install -e ".[dev]"`;
     `npm --prefix frontend ci`; then **one** step `run: bash scripts/check.sh`.
     Invoke the script, never a copy of its lines — a CI gate that drifts from the local
     gate is not a gate.
   - `neo4j` — `runs-on: ubuntu-latest`, `timeout-minutes: 15`, with a `services:`
     container `image: neo4j:5-community`, `env: NEO4J_AUTH: neo4j/writai-demo`,
     `ports: ["7687:7687"]`, and
     `options: --health-cmd "cypher-shell -u neo4j -p writai-demo 'RETURN 1'" --health-interval 10s --health-retries 12`.
     Steps: checkout, setup-python, `pip install -e ".[dev,graph]"`, then
     `PYTHONPATH=backend python3 -m pytest -m neo4j -q` with `WRITAI_RUN_NEO4J_TESTS=1`,
     `NEO4J_URI=bolt://localhost:7687`, `NEO4J_USERNAME=neo4j`,
     `NEO4J_PASSWORD=writai-demo`, `NEO4J_DATABASE=neo4j`.
     A `services:` container needs no local Docker daemon.
     Today this job runs the two existing tests in `test_neo4j_integration.py`; Track A
     adds the contract suite that makes it meaningful. **Track A does not edit this
     file.**
9. Add a comment block at the top of the workflow stating, in these terms, why it is
   separate from `writai-pr-authorization.yml`: that workflow is the **enforcement
   backstop** and deliberately runs base-branch code against the PR head as inert data;
   this one deliberately runs the PR's own tests. **The two must never be merged**, and
   nothing in this workflow may be copied into that one.

**Acceptance:** `bash scripts/check.sh` fails when total coverage drops below the floor
or any of the seven modules drops below its own, and passes unchanged today. The
workflow file parses, invokes `scripts/check.sh`, and defines both jobs.

**Verify:**
```bash
python3 -c "
import yaml
d = yaml.safe_load(open('.github/workflows/writai-check.yml'))
jobs = d['jobs']
assert set(jobs) == {'check','neo4j'}, jobs.keys()
steps = jobs['check']['steps']
assert any('scripts/check.sh' in str(s.get('run','')) for s in steps), 'check.sh not invoked'
assert 'neo4j:5-community' in str(jobs['neo4j']), 'no neo4j service container'
print('workflow OK')"
```
→ `workflow OK`.
```bash
bash scripts/check.sh 2>&1 | grep -E "^TOTAL|coverage floors OK|Required test coverage"
```
→ a `TOTAL` line at or above your floor, and `coverage floors OK (7 modules)`, and no
`Required test coverage … not reached`.

**Positive control — prove both gates can fail:**
```bash
PYTHONPATH=backend python3 -m pytest -q --cov=writai --cov-fail-under=99 >/dev/null 2>&1; echo "TOTAL_GATE=$?"
python3 - <<'PY'
import json, subprocess, sys
d = json.load(open("coverage.json"))
k = "backend/writai/grants.py" if "backend/writai/grants.py" in d["files"] else next(
    p for p in d["files"] if p.endswith("writai/grants.py"))
d["files"][k]["summary"]["percent_covered"] = 1.0
json.dump(d, open("coverage.json","w"))
PY
python3 scripts/ci/coverage_floors.py; echo "FILE_GATE=$?"
PYTHONPATH=backend python3 -m pytest -q --cov=writai --cov-report=json >/dev/null  # restore
```
→ `TOTAL_GATE` non-zero and `FILE_GATE` non-zero. If either is 0, the gate is
decorative — fix it before committing.

**If it fails:** If `fail_under` trips on a clean tree, the `omit` list is wrong —
confirm `backend/tests/` is excluded and re-measure before lowering anything.
If the key shape in `coverage.json` is not `files/<path>/summary/percent_covered`, read
the real shape with `python3 -c "import json;print(list(json.load(open('coverage.json'))['files'])[:3])"`
and match it. Never lower a floor to make a build pass: lower one **only** with a
one-line entry in `outputs/OPEN-ITEMS-REGISTER.md` naming the module and the reason.

---

## When T0 is done

Report, in this order:

1. The **before** and **after** gate output: exit code, pytest tally, vitest tally.
2. The **measured** coverage TOTAL and the seven per-file numbers, and the floors you
   derived from them.
3. Every file you touched, with one line on why.
4. Every step you skipped under standing rule 8, with the premise and the reality.
5. **What you removed** — every test, assertion, branch or comment. If nothing, say
   "nothing was removed" in those words.
6. Every shim, stub or hardcoded value you introduced — including the UUID literal in
   the frontend test stub.
7. Whether step T0.3.2 (the `.sqlite3` default) landed or was deferred to Track B.

Do not create any worktree. Do not start Track A, B or C.
