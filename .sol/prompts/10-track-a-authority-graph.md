# TASK: Track A — Authority & graph. Make the two stores one contract, and make a
# decision change atomic.

You are **Sol** (Codex `gpt-5.6-sol`). Work in the worktree at
`/work/writai-track-a`, branch `track/a-authority-graph`. Activate its venv
(`source .venv/bin/activate`) — each worktree has its own.

**You never run a git command.** Not `add`, not `commit`, not `status`, not `diff`,
not `checkout`. Leave your files on disk and describe what you changed; Fable commits.
If you need to know what changed, read the files.

Read `.sol/prompts/_context.md` first — it carries the verified ground truth, the
ownership table, the standing rules and the **Do not do** list, and it outranks your
instincts about this codebase.

**You own outright:** `backend/writai/authority/**`, `backend/writai/graph/**`,
`backend/writai/loop/**`, `backend/writai/scenarios/authority_contexts.py`,
`backend/tests/test_authority.py`, `backend/tests/test_authority_guards.py`,
`backend/tests/test_selective_invalidation.py`,
`backend/tests/test_neo4j_integration.py`, `backend/tests/test_neo4j_store.py`,
`backend/tests/test_graph_store_contract.py` (new).
**You may append one block at the end of** `outputs/OPEN-ITEMS-REGISTER.md`.
**Everything else in the repository is another track's.** In particular you do **not**
touch `backend/writai/config.py`, `.env.example`, `.github/workflows/**`,
`backend/writai/services/**`, or `backend/writai/workspaces/**`.

**Three things in your own territory are frozen. Read the *Do not do* section of
`_context.md` before your first edit:**
- `backend/tests/test_selective_invalidation.py:129-216`
  (`test_equal_depth_primary_path_is_stable_when_edge_order_changes`) — the
  order-independence proof. It is Phase A4's regression oracle. **If a single character
  of it needs editing for your change to pass, your change is wrong.**
- `backend/writai/provenance.py` — `authority_edge_sort_key` and
  `select_primary_invalidation_path`. Not yours. Never edited.
- `backend/writai/llm/extractor.py:179-189` — not yours, never opened.

**Standing rule 8 applies to every step: if a precondition below is false — a line does
not contain what this prompt quotes, a symbol is absent — stop that step, revert your
own edit by hand, skip the rest of the phase, append the skip to
`outputs/OPEN-ITEMS-REGISTER.md`, and report the premise versus the reality with a real
`path:line`. Do not improvise a replacement.**

**Your gate is `bash scripts/check.sh` and it must exit 0 before you declare any phase
done.** It was made green by T0 before your worktree existed; if it is red when you
start, stop and report — you have inherited a broken base.

**Neo4j is available in this environment** at `bolt://neo4j:7687` (user `neo4j`,
password `writai-demo`, database `neo4j`) unless the orchestrator tells you otherwise.
Steps marked `NEEDS NEO4J` should be run. If the connection fails, do the offline half,
record the unrun half in the register, and move on. **Never weaken an assertion to make
an unrunnable suite look green.**

---

## Phase A1 — One graph-store contract suite, shared by both backends

**Why:** `README.md:388-402` calls `backend/tests/test_neo4j_integration.py` the
"parity tests". It is two tests, and `_canonical_graph` (`:40-57`) **sorts both sides**
before comparing — it structurally cannot detect an ordering divergence, and it never
exercises the three real ones:
`neo4j_store.py:145-153` `add_edge` runs `MATCH … CREATE` and `.consume()`s it, so a
missing endpoint **silently no-ops**, where `memory.py:51-52` raises
`KeyError(f"Both edge endpoints must exist: {edge.source_id} -> {edge.target_id}")`;
`neo4j_store.py:115-118` `add_artifact` is a bare `CREATE (a:Artifact $props)`, so
duplicate ids are **permitted**, where `memory.py:32-33` raises
`ValueError(f"Artifact already exists: {artifact.id}")`;
`neo4j_store.py:140-143` `list_artifacts` has **no `ORDER BY`**, where `memory.py:47-48`
returns dict insertion order, and `engine.py:333-352` `current_requirements()` resolves
same-`effective_at` ties by that order. A shared contract suite turns a paragraph into
a red test. **A1 writes the suite and leaves it red against Neo4j. A2 makes it green.
Do not do both in one phase — the red run is the evidence.**

**Files:** `backend/tests/test_graph_store_contract.py` (new),
`backend/tests/test_neo4j_integration.py`

**Do:**

1. Create `backend/tests/test_graph_store_contract.py` with a module-level
   `graph_store` fixture parameterised over backends: `MemoryGraphStore()` always, and
   `Neo4jGraphStore(...)` only when `WRITAI_RUN_NEO4J_TESTS=1` and all four `NEO4J_*`
   variables are set. **Reuse the exact skip/fail logic already at
   `test_neo4j_integration.py:59-78`** — `pytest.skip` when the enable variable is
   unset, `pytest.fail` listing the missing variables when it is set but the connection
   variables are not. Do not invent a second convention.
2. Mark the Neo4j parameter with `pytest.mark.neo4j` (already registered in
   `pyproject.toml`'s `markers`) so `-m "not neo4j"` excludes it and `scripts/check.sh`
   stays credential-free.
3. Write these five tests against the fixture. **In each, the memory store's behaviour
   is the contract** — assert what `memory.py` does, not what `neo4j_store.py` does:
   - `test_add_artifact_rejects_a_duplicate_id` — seed one artifact, then
     `with pytest.raises((ValueError, KeyError)):` on a second `add_artifact` with the
     same `id`.
   - `test_add_edge_rejects_a_missing_endpoint` — `pytest.raises(KeyError)` for a
     `source_id` that does not exist, and **again**, separately, for a `target_id` that
     does not exist. Two assertions, because `memory.py:51` checks both.
   - `test_list_artifacts_is_ordered_by_id` — insert three artifacts in the order
     `["C-3","A-1","B-2"]` and assert
     `[a.id for a in store.list_artifacts()] == ["A-1","B-2","C-3"]`. Choosing
     *sorted by id* as the contract makes **both** backends deterministic and removes
     `current_requirements()`'s dependence on insertion order.
   - `test_outgoing_edges_is_ordered_by_target_then_kind` — matching the
     `ORDER BY target_id, kind` already present at `neo4j_store.py:175`.
   - `test_increment_version_returns_the_new_label` — after seeding at 17, assert
     `store.increment_version() == "graph-v18"` and `store.version_label == "graph-v18"`.
4. In `test_neo4j_integration.py`, add exactly one comment line above `_canonical_graph`
   (line 40): that it sorts deliberately to test **content** parity, and that
   **ordering and error** parity is covered by `test_graph_store_contract.py`.
   **Change nothing else in that file.**

**Acceptance:** All five pass against `MemoryGraphStore` inside `scripts/check.sh`.
Against Neo4j, at least three fail — that is the parity gap, now demonstrable.
`test_list_artifacts_is_ordered_by_id` **may also fail against memory** if the memory
store returns insertion order and your fixture inserts out of order — that is expected
and A2 step 3 fixes it. If it does, mark **only that one** parameter
`xfail(strict=True, reason="memory list_artifacts is insertion-ordered until A2")` and
say so in your report.

**Verify (offline, always runnable):**
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_graph_store_contract.py -q -m "not neo4j"
```
→ `5 passed` (or `4 passed, 1 xfailed` per the note above).

**Verify (NEEDS NEO4J):**
```bash
WRITAI_RUN_NEO4J_TESTS=1 NEO4J_URI=bolt://neo4j:7687 NEO4J_USERNAME=neo4j \
NEO4J_PASSWORD=writai-demo NEO4J_DATABASE=neo4j \
PYTHONPATH=backend python3 -m pytest backend/tests/test_graph_store_contract.py -q -m neo4j
```
→ **expected RED**: `add_artifact_rejects_a_duplicate_id`,
`add_edge_rejects_a_missing_endpoint` and `list_artifacts_is_ordered_by_id` fail.
**Paste the exact failure output into your report** — it is the evidence A2 closes.

**If it fails:** If the *memory* half fails on a test other than the ordering one, the
memory store is not the contract you assumed — read `graph/memory.py` again, make the
test match the code, and report the discrepancy against this prompt. If Neo4j is
unavailable, ship the memory half, record the unrun Neo4j half in the register, move on.

---

## Phase A2 — Close the Neo4j parity gap

**Why:** A1 turned the gap into failing tests. Close them at the source, and make both
backends meet the *same* contract rather than one chasing the other.
`neo4j_store.py` has never been executed by a machine other than the author's.

**Files:** `backend/writai/graph/neo4j_store.py`, `backend/writai/graph/memory.py`,
`backend/tests/test_graph_store_contract.py`

**Do:**

1. **Duplicate ids.** In `Neo4jGraphStore.reset()`'s seed transaction, add
   `CREATE CONSTRAINT artifact_id_unique IF NOT EXISTS FOR (a:Artifact) REQUIRE a.id IS UNIQUE`
   before the seeding writes. Then in `add_artifact` (`:115-118`), catch
   `neo4j.exceptions.ConstraintError` and re-raise as
   `ValueError(f"Artifact already exists: {artifact.id}")` — **the exact type and
   message `memory.py:33` raises**, character for character.
2. **Missing endpoints.** In `add_edge` (`:145-153`), keep the result of `session.run(...)`,
   call `.consume()` once into a variable, and read
   `summary.counters.relationships_created`. When it is `0`, raise
   `KeyError(f"Both edge endpoints must exist: {edge.source_id} -> {edge.target_id}")`
   — again the exact type and message from `memory.py:52`.
   **Confirm the attribute exists before you rely on it:**
   `python3 -c "import neo4j, inspect; print([a for a in dir(neo4j.SummaryCounters) if 'relation' in a])"`.
   If `relationships_created` is not there, read the installed driver's
   `SummaryCounters` and use the real attribute — and **record the driver version in
   your report**.
3. **Ordering.** Add `ORDER BY a.id` to the Cypher in `list_artifacts` (`:140-143`).
   Then change `MemoryGraphStore.list_artifacts` (`memory.py:47-48`) from
   `[deepcopy(item) for item in self._artifacts.values()]` to
   `[deepcopy(item) for item in sorted(self._artifacts.values(), key=lambda a: a.id)]`.
   **Both sides change.** This also removes `engine.py:333-352` `current_requirements()`'s
   dependence on dict insertion order for same-`effective_at` ties, which is a
   correctness improvement independent of the backend.
4. Remove the `xfail` marker A1 may have added to `test_list_artifacts_is_ordered_by_id`.

**Acceptance:** All five contract tests pass against **both** backends. The two existing
tests in `test_neo4j_integration.py` still pass, unmodified.

**Verify (offline):**
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_graph_store_contract.py -q -m "not neo4j" \
  && bash scripts/check.sh; echo "EXIT=$?"
```
→ `5 passed`, then `EXIT=0`.

**Verify (NEEDS NEO4J):**
```bash
WRITAI_RUN_NEO4J_TESTS=1 NEO4J_URI=bolt://neo4j:7687 NEO4J_USERNAME=neo4j \
NEO4J_PASSWORD=writai-demo NEO4J_DATABASE=neo4j \
PYTHONPATH=backend python3 -m pytest -m neo4j -q
```
→ `0 failed`.

**Positive control for step 3 — prove the ordering change is load-bearing:**
```bash
PYTHONPATH=backend python3 -c "
from writai.graph.memory import MemoryGraphStore
from writai.domain import Artifact, ArtifactKind
s = MemoryGraphStore()
for i in ('C-3','A-1','B-2'):
    s.add_artifact(Artifact(id=i, kind=ArtifactKind.DECISION, title=i, scopes=set()))
print([a.id for a in s.list_artifacts()])"
```
→ `['A-1', 'B-2', 'C-3']`. Before step 3 it printed `['C-3', 'A-1', 'B-2']`.

**If it fails:** If step 3's memory-store change turns an **existing** test red, that
test was depending on insertion order. Read it: if it asserts a *displayed* order, fix
the test and say so; if it asserts a *behavioural* order that the product needs, **stop
and report — the contract choice is wrong and that is an escalation, not a test edit.**
If the `CREATE CONSTRAINT` syntax is rejected, the server is Neo4j 4.x: record the
version and skip step 1 rather than falling back to a `MERGE`, which would silently
change `add_artifact` from create to upsert.

---

## Phase A3 — Make `apply_decision_change` transactional

**Why:** `authority/engine.py:181-196` performs `add_artifact` (181) →
`add_edge` (182-190) → `increment_version` (191) → `_propagate_invalidation` (192-196),
which calls `_mark_artifact` (`:206-216`) → `update_artifact` once per invalidated node.
There is no transaction. A failure between 182 and the end of the traversal leaves a
superseding decision **and** its `SUPERSEDES` edge in the graph while every downstream
artifact still reads `VALID`. That is not a lost update — it is **a graph that actively
asserts stale work is authorized**, which is the precise failure this product exists to
prevent. It is a `BLOCKER` by the rule in `_context.md`.

**Files:** `backend/writai/graph/base.py`, `backend/writai/graph/memory.py`,
`backend/writai/graph/neo4j_store.py`, `backend/writai/authority/engine.py`,
`backend/tests/test_authority.py`, `backend/tests/test_graph_store_contract.py`

**Do:**

1. Add one method to the `GraphStore` Protocol in `graph/base.py`:
   `def transaction(self) -> AbstractContextManager[None]: ...`
   with a docstring saying: everything inside commits together or not at all.
   Import `AbstractContextManager` from `contextlib`.
2. `MemoryGraphStore`: implement as a `@contextmanager` that `deepcopy`s `_version`,
   `_artifacts` and `_edges` on entry and restores all three on exception, re-raising.
   **Snapshot whatever `__init__` actually sets** — read the constructor and list them;
   if it sets a fourth attribute, snapshot that too and say so in your report.
3. `Neo4jGraphStore`: add `self._tx: Any = None` in `__init__`. Implement `transaction()`
   as a `@contextmanager` that opens **one** session, calls `session.begin_transaction()`,
   assigns it to `self._tx`, yields, then `commit()` on clean exit / `rollback()` on
   exception, and clears `self._tx` in a `finally`. Keep the session open for the whole
   context manager's lifetime.
4. Add a private `def _run(self, query: str, **params: Any)` to `Neo4jGraphStore` that
   uses `self._tx.run(...)` when a transaction is open and opens a short
   `self._driver.session(database=self._database)` otherwise. Route **every** method
   that currently does `with self._driver.session(...) as session: session.run(...)`
   through it — that is `increment_version`, `add_artifact`, `update_artifact`,
   `get_artifact`, `list_artifacts`, `add_edge`, `list_edges`, `outgoing_edges`
   (`neo4j_store.py:95-179`). This is a mechanical change; **make it and change nothing
   else in the file.** `reset()` keeps its own explicit transaction.
5. Wrap `engine.py:181-196` in `with self.graph.transaction():` — `add_artifact`,
   `add_edge`, `increment_version`, and the whole `_propagate_invalidation(...)` call.
   **Lines 197 and 198 — `report.graph_version = graph_version` and
   `self.last_report = report` — stay OUTSIDE the block.** They are engine-local state,
   not graph state. (The source implementation plan says "wrap 181-198"; it is wrong,
   and this correction is deliberate.)
6. Tests in `backend/tests/test_authority.py`:
   - `test_a_failed_invalidation_leaves_the_graph_untouched`: seed the fixture graph at
     `graph-v17`; wrap the store in a subclass whose `update_artifact` raises
     `RuntimeError("injected")` on its **third** call; then
     `with pytest.raises(RuntimeError, match="injected"):` around
     `apply_decision_change(...)` with the v18 decision the existing tests already load.
     Afterwards assert **three** things: `graph.version_label == "graph-v17"`;
     `with pytest.raises(KeyError): graph.get_artifact("DEC-018")`; and
     `graph.get_artifact("TASK-102").validity is ValidityStatus.VALID`.
   - `test_a_successful_change_still_commits_every_artifact`: the unmodified path still
     produces `graph-v18`, `TASK-102` invalidated and `TASK-101` valid — proving the
     wrapper changed nothing on the happy path.
   Use whatever fixture loader `test_authority.py` already uses for the v17 graph and
   the v18 decision; **do not introduce a second fixture path.**
7. Add `test_transaction_rolls_back_on_exception` to
   `backend/tests/test_graph_store_contract.py` so both backends are held to the same
   contract: inside `with pytest.raises(RuntimeError):`, open `store.transaction()`,
   `add_artifact`, `increment_version`, then `raise RuntimeError("boom")`; afterwards
   assert the artifact is absent and the version label is unchanged.

**Acceptance:** A failure anywhere inside `apply_decision_change` leaves the graph
identical to its pre-call state, on both backends, and the happy path is byte-identical
to before.

**Verify:**
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_authority.py -q \
  -k "leaves_the_graph_untouched or still_commits_every_artifact"
```
→ `2 passed`.
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_graph_store_contract.py -q -m "not neo4j" \
  && bash scripts/check.sh; echo "EXIT=$?"
```
→ `6 passed`, then `EXIT=0`.
```bash
PYTHONPATH=backend python3 -m writai.demo | grep "Invalidation path"
```
→ unchanged from before this phase — record the exact line in your report.

**If it fails:** If the memory rollback misses a field, `MemoryGraphStore` holds state
beyond the three attributes — read `__init__` and snapshot what it really sets. If the
Neo4j `self._tx` threading produces `SessionExpired`, the transaction is outliving its
session: keep one session open for the whole context manager. If wrapping the block
changes the demo's invalidation path **at all**, you wrapped too much — check that 197
and 198 are outside.

---

## Phase A4 — Batch the invalidation BFS into one graph read

**Why:** `engine.py:246-274` walks the graph one node at a time: per dequeued node one
`self.graph.outgoing_edges(current_id, DOWNSTREAM_EDGES)` call (`:253`), then one
`self.graph.get_artifact(edge.target_id)` **per child** (`:258`). Against
`MemoryGraphStore` that is free. Against Neo4j each is a network round trip
(`neo4j_store.py:165-179` and `:131-138`), so a single decision change costs
O(nodes + edges) round trips and the traversal becomes latency-bound. The property this
must not break is the order-independence proven by
`test_selective_invalidation.py:129-216`.

**Files:** `backend/writai/graph/base.py`, `backend/writai/graph/memory.py`,
`backend/writai/graph/neo4j_store.py`, `backend/writai/authority/engine.py`,
`backend/tests/test_selective_invalidation.py`,
`backend/tests/test_graph_store_contract.py`

**Do:**

1. Add one method to the `GraphStore` Protocol:
   `def downstream_subgraph(self, root_id: str, kinds: set[EdgeKind]) -> tuple[list[Artifact], list[Edge]]:`
   — every artifact and edge reachable from `root_id` along edges whose kind is in
   `kinds`, **artifacts sorted by `id`**, **edges sorted by
   `(source_id, kind, target_id)`**. Say both orderings in the docstring.
2. `MemoryGraphStore`: implement with the same BFS it can already express over
   `self._edges`, then sort both lists. A pure in-memory refactor.
3. `Neo4jGraphStore`: implement as **one** query routed through `self._run` (A3 step 4),
   collecting distinct artifacts and relationships and ordering in the Cypher.
   **Do not use APOC unless you have confirmed it is installed** — and on
   `neo4j:5-community` it is not by default. Write the collection in plain Cypher with
   `UNWIND` over `relationships(path)`, or do the DISTINCT-ing in Python after one
   `RETURN path`. **One round trip is the requirement; the exact Cypher is yours.**
   Verify the query runs before you rely on it, and paste it into your report.
4. In `engine.py:246-274`, call `downstream_subgraph(superseded.id, DOWNSTREAM_EDGES)`
   **once, before the loop**, and build two dicts from the result: `artifacts_by_id` and
   `edges_by_source`. Replace the `self.graph.outgoing_edges(...)` call at `:253` with a
   lookup into `edges_by_source` and the `self.graph.get_artifact(...)` at `:258` with a
   lookup into `artifacts_by_id`.
   **Keep the BFS itself exactly as it is** — the `deque`, `visited`, `current_path`,
   and the `sorted(..., key=authority_edge_sort_key)` at `:252-255`. The traversal order
   is the load-bearing part; only the data *source* changes.
5. Keep `_mark_artifact` (`:206-216`) writing through `self.graph.update_artifact`.
   Only reads are batched; the invalidation must still be persisted.
6. Line `:275` — `affected_artifacts = [self.graph.get_artifact(id) for id in affected]`
   — may also read from `artifacts_by_id`, **but only if the dict is refreshed after
   `_mark_artifact` ran**, since those artifacts have been mutated. If refreshing is not
   trivially correct, **leave line 275 calling the store** and say so; a wrong
   `InvalidationReport` is far worse than N extra reads.
7. Tests:
   - `backend/tests/test_selective_invalidation.py:129-216`
     `test_equal_depth_primary_path_is_stable_when_edge_order_changes` must pass
     **byte-identical**. Do not touch it.
   - Add `test_the_invalidation_traversal_reads_the_graph_once` to the same file: wrap
     the store in a call-counting subclass and assert, across one
     `apply_decision_change`, that `downstream_subgraph` was called **exactly once** and
     that `outgoing_edges` was called **zero** times. Assert `get_artifact`'s count
     against whatever step 6 left — state the number in the test's docstring so a future
     reader knows it is deliberate.
   - Add `test_downstream_subgraph_is_ordered_and_kind_filtered` to
     `test_graph_store_contract.py`: assert both sort orders, and that an edge kind
     **not** in the authority downstream set is excluded — the same property
     `test_selective_invalidation.py:214-216` asserts about `EVIDENCE-A`.

**Acceptance:** One `downstream_subgraph` call per decision change on both backends;
`test_equal_depth_primary_path_is_stable_when_edge_order_changes` unmodified and
passing; every field of the returned `InvalidationReport` unchanged.

**Verify:**
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_selective_invalidation.py -q
```
→ all passed.
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_authority.py \
  backend/tests/test_scenario_catalog.py -q \
  && PYTHONPATH=backend python3 -m writai.demo | grep -A1 "Invalidation path"
```
→ tests pass and the demo still prints
`DEC-018 -> DEC-004 -> SPEC-009 -> TICKET-100 -> TASK-102`.

**Ask Fable to run this for you (you may not run git):**
`git diff track/a-authority-graph~1 -- backend/tests/test_selective_invalidation.py`
must show **no** modification inside
`test_equal_depth_primary_path_is_stable_when_edge_order_changes`.

**If it fails:** If the invalidation path changes at all, the batched read returned a
different edge order than `outgoing_edges` did — **sort in Python with
`authority_edge_sort_key` rather than trusting the Cypher's `ORDER BY`.** Do not "fix"
it by editing `provenance.py` or the frozen test: both are forbidden, and if the batched
version needs either changed, the batched version is wrong — revert it and report.

---

## Phase A5 — Make `last_report` request-scoped

**Why:** `engine.py:74` declares `self.last_report` as instance state; `:198` writes it
on every applied decision change; `:458-466` reads it inside `evaluate_plan` to populate
`invalidated_artifact_ids`, `preserved_artifact_ids`, `evidence_refs` and
`invalidation_path` on the returned `AuthorizationResult`. `authority_api.py:140`
constructs **one** module-level `runtime`, and `/authorize` (`:1694-1695`) calls
`runtime.authority.evaluate_plan(...)`. So one client's decision change decorates a
completely unrelated client's `/authorize` response with that client's invalidated
artifact ids and provenance path. Per-workspace contexts each own their own
`IntentAuthority`, so the bleed is confined to the shared runtime — **but that is the
demo path a reviewer exercises first**, and shared mutable state on an HTTP handler is
the wrong shape regardless.

**Files:** `backend/writai/authority/engine.py`, `backend/writai/loop/workflow.py`,
`backend/writai/scenarios/authority_contexts.py`,
`backend/tests/test_authority_guards.py`

**Do:**

1. Change the signature at `engine.py:354` from
   `def evaluate_plan(self, *, run_id: str, task_id: str, plan: AgentPlan) -> AuthorizationResult:`
   to
   `def evaluate_plan(self, *, run_id: str, task_id: str, plan: AgentPlan, report: InvalidationReport | None = None) -> AuthorizationResult:`.
   **The `None` default is mandatory and is not a convenience** — Track B's worktree
   contains call sites that do not yet pass the argument, and a required parameter would
   turn its gate red. Add a comment on the parameter saying exactly that.
2. At `:458`, read the **`report` parameter** instead of `self.last_report`. Leave
   `self.last_report` itself in place and still written at `:198` —
   `runtime.reset()`, the `/graph` snapshot, and both context registries legitimately
   read it as engine state.
3. Pass it explicitly at the call sites where correlating a report with a plan is real,
   because they are a single in-process sequence:
   - every `evaluate_plan` call in `backend/writai/loop/workflow.py` →
     `report=self.authority.last_report`. Find them all with
     `grep -n "evaluate_plan" backend/writai/loop/workflow.py`; there should be four.
     If the count differs, use what you find and say so.
   - `backend/writai/scenarios/authority_contexts.py` (the call inside its `authorize`
     path) → `report=context.authority.last_report`.
4. `engine.py`'s internal re-evaluation inside `verify_grant` (`:506+`) also calls
   `evaluate_plan` — pass `report=self.last_report` there, preserving today's behaviour
   exactly, since it is an in-process re-check of the same engine.
5. **Leave `authority_api.py:1694-1695` alone. Passing nothing IS the fix** — and that
   file is Track B's, so you could not edit it anyway.
6. **Do not touch `backend/writai/workspaces/authority_contexts.py:417`.** It is Track
   B's file and needs the same `report=` line; T1 adds it on the merged tree. Note this
   explicitly in your report so the orchestrator can confirm T1 ran.
7. Tests in `backend/tests/test_authority_guards.py`:
   - `test_authorize_does_not_inherit_another_requests_invalidation_report`: with
     `TestClient(authority_api.app)`, POST `/decisions/change` with the v18 decision,
     then POST `/authorize` with an unrelated valid plan; assert the response's
     `invalidated_artifact_ids == []`, `preserved_artifact_ids == []` and
     `invalidation_path == []`.
   - `test_the_in_process_loop_still_reports_its_own_invalidation_path`: drive
     `loop/workflow.py`'s controller through the same change and assert
     `invalidation_path == ["DEC-018","DEC-004","SPEC-009","TICKET-100","TASK-102"]`.
   - `test_evaluate_plan_without_a_report_returns_empty_provenance_fields`: a direct
     engine call with `report=None` **after** an applied change; all four fields empty.

**Acceptance:** No `/authorize` response carries data derived from a different request;
the in-process demo and loop output are byte-identical to before.

**Verify:**
```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_authority_guards.py \
  backend/tests/test_demo_flow.py backend/tests/test_grants.py -q
```
→ all passed.
```bash
PYTHONPATH=backend python3 -m writai.demo | grep -A1 "Invalidation path"
```
→ `DEC-018 -> DEC-004 -> SPEC-009 -> TICKET-100 -> TASK-102`, unchanged.
```bash
bash scripts/check.sh; echo "EXIT=$?"
```
→ `EXIT=0`.

**Positive control — prove the bleed existed and is gone.** Before your change, the
first test above **must fail**. Run it against the pre-change engine (stash your edit by
hand, run, restore) and paste both outputs. A test that passes before and after proves
nothing.

**If it fails:** If the demo's invalidation path goes empty, a `workflow.py` call site
was missed — `grep -n "evaluate_plan" backend/writai/loop/workflow.py` lists them all.
If a scenario test regresses, `scenarios/authority_contexts.py` did not get its explicit
`report=`. If a **workspace** test regresses, that is the T1 line — expected, note it,
do not fix it here.

---

## When Track A is done

Report, in this order:

1. The gate: `bash scripts/check.sh` exit code and the pytest/vitest tallies.
2. The **exact Neo4j failure output from A1** (the parity evidence) and the exact
   passing output from A2 — or, if Neo4j was unavailable, say so plainly and name what
   is unrun.
3. The driver version and the `SummaryCounters` attribute you used in A2 step 2, and the
   final Cypher you used in A4 step 3.
4. Whether A4 step 6 left line 275 reading the store or the dict, and why.
5. Every file you touched, with one line on why.
6. Every step skipped under standing rule 8, with premise versus reality at a real
   `path:line`.
7. **What you removed.** If nothing, say "nothing was removed" in those words.
8. Every shim, stub, hardcoded value or synthetic id you introduced.
9. A one-line confirmation that
   `test_equal_depth_primary_path_is_stable_when_edge_order_changes`,
   `provenance.py`, and `llm/extractor.py` are **unmodified**.
10. The reminder that `workspaces/authority_contexts.py:417` still needs its `report=`
    line in T1.

You have run no git command. Say so.
