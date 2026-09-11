# Contributing

## The gate

```bash
bash scripts/check.sh
```

That is the whole gate: pytest with coverage floors, ruff, mypy, `compileall`, vitest,
`tsc`, and the Vite build. CI runs **this same script** rather than a copy of its steps,
and `make check` delegates to it, so there is exactly one definition of "green" and a
step added here reaches everyone. It must exit 0 before you commit.

`make demo` must keep running with **zero configuration**. That property is why this
repository is reviewable in five minutes and it is worth more than any single change.

## What is enforced

- **ruff and mypy on `backend/`.** No `# noqa` or `# type: ignore` to silence a real
  finding.
- **Coverage floors**, total and per-module, in `scripts/ci/coverage_floors.py`.
  Lowering one requires an entry in `outputs/OPEN-ITEMS-REGISTER.md` naming the module
  and the reason. This is already stated in `pyproject.toml`.
- **`backend/writai/config.py` is the only place environment variables are read.** A new
  variable in `.env.example` needs either a `Settings` field or an entry in
  `_READ_OUTSIDE_SETTINGS` in `backend/tests/test_runtime_config.py`, and a test asserts
  it — documenting a variable nothing reads fails the build.
- **Route tiers.** `backend/tests/test_route_authentication.py` freezes every mutating
  route to an authentication tier and reads the README's counts back. Adding a route
  means choosing a tier deliberately and updating the section.

## Documentation is tested

`README.md`'s "Where the trust boundary is" and "Known limits", and `hooks/README.md`'s
timeout ceiling, are read back by tests. This is on purpose: a stale `file:line` in the
section a reviewer trusts most is worse than no section. **If you cite a line, open it
first.** If you change behaviour these sections describe, change them in the same commit.

## Tests

Add or update a test with every behaviour change, and **watch the new test fail without
your change** before you trust it. A regression test nobody has seen go red is not a
regression test. If you are fixing something a reviewer found, their test stays as they
wrote it — weakening it to pass is the failure mode the review exists to catch.

## Scope

`AGENTS.md` carries the product invariant and the engineering invariants; it outranks
this file. Invariant 8 in particular: surgical changes, no refactoring of adjacent code,
and pre-existing dead code is mentioned rather than deleted.

Dependencies are range-pinned in `pyproject.toml`; the frontend is lockfile-pinned and
installed with `npm ci`. GitHub Actions are pinned to commit SHAs, and Dependabot
proposes the upgrades monthly, grouped.

## Reporting a vulnerability

Privately — see [`SECURITY.md`](SECURITY.md), which also lists the weaknesses that are
known, deliberate, and already documented.
