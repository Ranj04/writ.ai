# writ.ai task queue

## P0 — preserve the core proof

- [x] Seed `graph-v17` with a decision, spec, ticket, two sibling tasks, and an active plan.
- [x] Authorize the initial plan and issue a snapshot-bound grant.
- [x] Ingest an approved `graph-v18` decision scoped to `export.authorization`.
- [x] Traverse the multi-hop lineage.
- [x] Invalidate `TASK-102` while preserving `TASK-101`.
- [x] Reject the `graph-v17` grant.
- [x] Replan to `admin_only` and authorize the corrected plan.
- [x] Cover the flow with deterministic tests.

## P1 — service integration

- [x] Expose the authority API.
- [x] Expose the agent-loop API.
- [x] Expose the independent executor API.
- [x] Add robust service-to-service timeout and error handling.
- [x] Add a shared correlation ID to every response and event.
- [x] Add SSE streams for graph and loop-state updates.

## P2 — frontend

- [x] Create a thin React/Vite shell and API clients.
- [x] Make the user-owned Workspace the main product entry.
- [x] Guide users through import, approval, authorization, change, and verification.
- [x] Provide seeded Examples with a plain-language story and exact Impact map.
- [x] Highlight preserved work separately from invalidated work.
- [x] Preserve the retired Guided Proof on `archive/guided-proof`.

## P3 — Neo4j

- [x] Provide a Neo4j store implementation and Docker Compose service.
- [x] Add Neo4j integration tests behind an opt-in marker.
- [x] Confirm the Cypher traversal returns the same report as the memory store (opt-in Aura run).
- [x] Add a seed/reset endpoint for AuraDB.

## P4 — optional LLM extraction

- [x] Define an extraction interface and fixture implementation.
- [x] Add an Anthropic adapter skeleton with structured JSON output.
- [x] Add evidence-span validation before accepting a proposed edge.
- [x] Add a review state when extraction confidence is below threshold.
- [x] Ensure LLM extraction is never required for the deterministic demo.

## P6 — the product: Lane A (enforcement)

See `docs/BUILD_LANE_A.md` and the frozen `docs/LANE_A_INTERNAL_CONTRACT.md`.

- [x] Freeze the Lane A/B seam: `supervisor_contract.py`, `SupervisorExecutionMode.LIVE`, both CLI groups, every new config key.
- [x] A1 — `ClaudeCodeSupervisorRuntime` and the real `SupervisorInterruptPort`.
- [x] A2 — deterministic session-to-assignment binding.
- [x] A3 — hook verdict endpoint `POST /supervisor/sessions/{session_id}/check`, plus `GET /supervisor/sessions` for the dev CLI.
- [x] A4 — `SessionStart` / `PreToolUse` / `SessionEnd` hooks and the managed-settings example.
- [x] A5 — dev CLI: `attach`, `status`, `why`, `ack`, `watch`, rendered per `docs/TERMINAL_OUTPUT_SPEC.md`.
- [x] A6 — local desktop notification on deny, fired after the verdict is written.
- [x] Five-session demo fixture, proven by `backend/tests/test_five_session_demo.py`: one approved change stops exactly three sessions and leaves exactly two running.
- [x] Blast-radius count checked against the set that actually gets denied (`test_the_blast_radius_equals_the_set_that_actually_gets_denied`).
- [ ] **Swap `NullSupervisorInterruptPort` for `live_interrupt_port` in `services/agent_api.py`.** One line, deliberately left for a human: it changes what the running service does for every lane.
- [x] Real-vs-simulated panel updated for the live runtime, including that a crashed hook fails open and the PR check is the backstop.
- [x] Five-session demo verified twice from a cold machine against the runbook.

Known-open defects live in `outputs/OPEN-ITEMS-REGISTER.md`, not in this list.

## P7 — integration (all lanes merged)

See `INTEGRATION_REPORT.md` for evidence against every item below.

- [x] Merge `lane-a`, `lane-d-approvals`, `lane-c-demo` and Lane E's PR workflow.
- [x] One hook implementation ships; the managed settings name it.
- [x] `GET /supervisor/sessions` carries the assignment state each session is judged on.
- [x] `.writai/attach` wired end to end (built by sol).
- [x] CI `--require-grant` on by default, scoped so an unbound branch still passes.
- [x] `check.sh` hard-fails, loudly, on the three silent demo-killers.
- [x] CrustData observation path — captured deliveries are replay-gated and
      reconstructed fallback data is never labelled live (built by sol).
- [x] `/approvals` reachable; Lane D's last audit divergence closed.
- [x] Lane C's launcher arms and fires again after Lane B disabled the legacy approvals.
- [x] The seeder's channel-auth bypass is opt-in and on the honesty panel.
- [x] Close INT-3: deny-until-acknowledged, so a lost deny re-delivers instead of allowing.
- [x] Close INT-5: an unconfirmed approval reads as unconfirmed, never as "nothing applied".
- [x] Close INT-4: approval credentials bind to the change object, so fixture data cannot post.
- [x] Close INT-1: a malformed service response fails the PR check instead of passing as unbound.
- [ ] **INT-2 is deferred, not forgotten.** Grant verification belongs behind a server
      endpoint, not reimplemented in the stdlib 3.9 check. Invariant 5 holds meanwhile:
      the executor verifies run_id, task_id and plan_hash. See the register.
- [x] **Provision a Hexclave team, then configure `HEXCLAVE_TEAM_ID`.** The
      current secret key and team resolve successfully through `writai doctor
      hexclave`.
- [x] Make *Branch authorization is current* a required status check on protected
      `main` (configured and verified 2026-07-25).
- [ ] **Operationalize the required check.** It currently fails closed until a
      GitHub runner can reach a narrowly exposed authenticated agent service and
      the PR has an honest task binding plus a current unexpired grant.
- [ ] **Capture a real CrustData payload** to replace the documentation-reconstructed
      fixture. The existing API key has passed a read-only watcher-list request,
      but there are zero watchers and no genuine callback. Select the exact
      LinkedIn target, provision the watcher, and set the human-confirmed
      CrustData person-id → Hexclave user-id binding; never infer it.

## P5 — presentation hardening

- [ ] Rehearse the 3–5 minute flow from `docs/DEMO_SCRIPT.md`.
- [ ] Record a backup screen capture.
- [x] Keep competitor comparisons in Q&A, not the opening.
- [x] Add a visible “real vs simulated” panel.
- [x] Freeze fixture IDs and demo data before presentation day.
