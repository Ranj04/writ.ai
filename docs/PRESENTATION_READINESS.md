# Presentation readiness

## Automated preflight

Run this before rehearsing:

```bash
make check
make stack
```

The UI header should show all three services connected. Run the user-owned Workspace flow once, or
use the deterministic CSV backup at `/scenario-lab?demo=1`, and confirm:

- the old `graph-v17` grant is rejected;
- `TASK-101` remains preserved while `TASK-102` is invalidated;
- the loop enters `REPLAN`;
- the corrected `graph-v18` grant is accepted.

Fixture IDs and the claim that `DEC-018` never names `TICKET-100` are frozen by
`backend/tests/test_fixture_contract.py`.

## Human rehearsal

Use `docs/DEMO_SCRIPT.md`, time one uninterrupted run, and keep it between three and five minutes.
Do not add named competitor comparisons to the opening.

## Backup capture

After the timed rehearsal, record one clean full-demo run at the presentation laptop's native
resolution. Keep the file local and verify it plays without network access. Capture the initial
`graph-v17` state, selective invalidation, old-grant rejection, `REPLAN`, and final acceptance.

The recording is a presentation artifact and intentionally is not committed to the repository.
