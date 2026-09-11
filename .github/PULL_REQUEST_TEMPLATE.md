## What changed, and why

<!-- The reasoning matters more than the diff. If this closes a finding, cite it. -->

## Evidence

- [ ] `bash scripts/check.sh` exits 0 (the same script CI runs)
- [ ] New behaviour has a test, and I have **seen that test fail** without the change
- [ ] Any `file:line` I cited in a doc, I opened and read on this branch

## Known limits

<!-- Anything this leaves open, or any disclosure it makes stale. Write it down
     rather than letting a reviewer find it. outputs/OPEN-ITEMS-REGISTER.md is
     the register; README's "Known limits" and "Where the trust boundary is" are
     the operator-facing statements, and tests read both back. -->

---

**If the `Branch authorization is current` check is red:** it fails closed when
`WRITAI_AGENT_URL` (a repository variable) or `WRITAI_CI_API_KEY` (a secret) is unset,
which is the expected state on a fork. That is deliberate — a check that passes during an
outage is not a backstop. See `scripts/ci/README.md`.
