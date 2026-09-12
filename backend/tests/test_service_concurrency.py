from __future__ import annotations

import ast
import inspect
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from multiprocessing.synchronize import Barrier
from pathlib import Path
from threading import Event

import httpx
import pytest
from writai.domain import (
    AgentPlan,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    AuthorizationResult,
    PlanAction,
    Verdict,
)
from writai.services import agent_api, support
from writai.workspaces import orchestrator as orchestrator_module
from writai.workspaces.models import (
    LiveWorkspaceImportRequest,
    LiveWorkspaceRecord,
    WorkspaceEvent,
)
from writai.workspaces.repository import (
    JsonFileLiveWorkspaceRepository,
    SqliteLiveWorkspaceRepository,
)


def test_agent_reset_waits_for_an_in_flight_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization_started = Event()
    release_authorization = Event()
    reset_started = Event()
    reset_finished = Event()

    def slow_authorize(_url: str, **_kwargs: object) -> httpx.Response:
        authorization_started.set()
        assert release_authorization.wait(timeout=1)
        result = AuthorizationResult(
            verdict=Verdict.ALLOW,
            reason="Plan matches current approved requirements.",
            graph_version="graph-v17",
            task_id="TICKET-100",
        )
        return httpx.Response(200, json=result.model_dump(mode="json"))

    def concurrent_reset() -> dict[str, object]:
        reset_started.set()
        response = agent_api.reset_demo()
        reset_finished.set()
        return response

    monkeypatch.setattr(support.httpx, "post", slow_authorize)
    agent_api.reset_demo()

    with ThreadPoolExecutor(max_workers=2) as pool:
        start_future = pool.submit(agent_api.start_demo)
        assert authorization_started.wait(timeout=1)
        reset_future = pool.submit(concurrent_reset)
        assert reset_started.wait(timeout=1)
        assert reset_finished.wait(timeout=0.05) is False
        release_authorization.set()

        start_response = start_future.result(timeout=1)
        reset_future.result(timeout=1)

    assert start_response["run"] is not None
    assert agent_api.state_payload()["run"] is None


# --- Cross-process read-modify-write ------------------------------------------

WORKSPACE_ID = "lost-update"
APPENDS_PER_WORKER = 50


def _workspace_record() -> LiveWorkspaceRecord:
    when = datetime(2026, 1, 1, tzinfo=UTC)
    definition = LiveWorkspaceImportRequest(
        id=WORKSPACE_ID,
        name="Lost update probe",
        authority_policy={"probe.scope": {"approver"}},
        baseline_decision=Artifact(
            id="DEC-PROBE",
            kind=ArtifactKind.DECISION,
            title="Probe",
            text="Probe baseline.",
            scopes={"probe.scope"},
            approval_status=ApprovalStatus.PROPOSAL,
            authority_role="approver",
            effective_at=when,
            source_ref="manual://probe",
            attributes={"requirements": {"probe.scope": {"value": 1}}},
        ),
        specification=Artifact(
            id="SPEC-PROBE",
            kind=ArtifactKind.SPECIFICATION,
            title="Probe spec",
            scopes={"probe.scope"},
            source_ref="manual://probe/spec",
        ),
        ticket=Artifact(
            id="TICKET-PROBE",
            kind=ArtifactKind.TICKET,
            title="Probe ticket",
            scopes={"probe.scope"},
            source_ref="manual://probe/ticket",
        ),
        tasks=[
            Artifact(
                id="TASK-PROBE",
                kind=ArtifactKind.TASK,
                title="Probe task",
                scopes={"probe.scope"},
                source_ref="manual://probe/task",
            )
        ],
        plan=AgentPlan(
            id="PLAN-PROBE",
            ticket_id="TICKET-PROBE",
            objective="Probe",
            actions=[
                PlanAction(
                    id="ACTION-PROBE",
                    description="Probe action.",
                    scopes={"probe.scope"},
                    attributes={"value": 1},
                )
            ],
        ),
    )
    return LiveWorkspaceRecord(
        definition=definition,
        context_id=f"live-{WORKSPACE_ID}",
        graph_version="graph-v1",
        current_plan=definition.plan,
    )


def _append_worker(
    store_class: str, path: str, worker: str, barrier: Barrier
) -> None:
    repository = (
        JsonFileLiveWorkspaceRepository(path)
        if store_class == "json"
        else SqliteLiveWorkspaceRepository(path)
    )

    def append(record: LiveWorkspaceRecord) -> LiveWorkspaceRecord:
        record.history.append(
            WorkspaceEvent(
                sequence=len(record.history) + 1,
                event_type="probe.append",
                detail=f"{worker}:{len(record.history) + 1}",
            )
        )
        return record

    barrier.wait(timeout=10)
    for _ in range(APPENDS_PER_WORKER):
        repository.mutate(WORKSPACE_ID, append)


@pytest.mark.parametrize(
    ("store_class", "suffix"),
    [
        pytest.param(
            "json",
            "json",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "the JSON store's RLock is process-local; two processes "
                    "rewriting the whole document lose each other's updates"
                ),
            ),
        ),
        pytest.param("sqlite", "sqlite3"),
    ],
    ids=["json", "sqlite"],
)
def test_concurrent_saves_from_two_processes_do_not_lose_an_update(
    tmp_path: Path, store_class: str, suffix: str
) -> None:
    """Two processes append to one record through ``mutate()``; nothing is lost.

    No retry loop anywhere: a lost update must surface as a short list, not be
    papered over by the test.
    """

    path = tmp_path / f"live-workspaces.{suffix}"
    repository = (
        JsonFileLiveWorkspaceRepository(path)
        if store_class == "json"
        else SqliteLiveWorkspaceRepository(path)
    )
    repository.create(_workspace_record())

    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    workers = [
        context.Process(
            target=_append_worker,
            args=(store_class, str(path), name, barrier),
        )
        for name in ("alpha", "beta")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    assert all(worker.exitcode == 0 for worker in workers), [
        worker.exitcode for worker in workers
    ]

    history = repository.get(WORKSPACE_ID).history
    assert len(history) == 2 * APPENDS_PER_WORKER
    assert [event.sequence for event in history] == list(
        range(1, 2 * APPENDS_PER_WORKER + 1)
    )


# --- No network call inside a store transaction ------------------------------

#: Orchestrator attributes whose methods leave the process: the authority
#: transport, the executor and the supervisor runtime.
_NETWORK_ATTRIBUTES = frozenset({"_transport", "_executor", "_supervisor_runtime"})

#: ``(method, helper)`` pairs the walk may skip: a helper called from inside a
#: store transaction that genuinely must observe mid-transaction state. Every
#: entry needs a comment naming the reason. Empty means no site is excused.
_ALLOWED_HELPERS_IN_TRANSACTION: frozenset[tuple[str, str]] = frozenset()


def _self_attribute_chain(node: ast.expr) -> list[str]:
    """Return ``["_transport", "approve_baseline"]`` for ``self._transport.approve_baseline``.

    Empty when the expression is not rooted at ``self``.
    """

    chain: list[str] = []
    while isinstance(node, ast.Attribute):
        chain.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name) and node.id == "self":
        chain.reverse()
        return chain
    return []


def _is_repository_mutate(call: ast.Call) -> bool:
    return _self_attribute_chain(call.func) == ["_repository", "mutate"]


def _store_transaction_callables(
    method: ast.FunctionDef,
) -> list[tuple[str, ast.AST]]:
    """Every callable ``method`` passes to ``self._repository.mutate(...)``.

    Named callables resolve to the nested ``def`` of that name; a lambda is
    returned as itself.
    """

    nested = {
        node.name: node
        for node in ast.walk(method)
        if isinstance(node, ast.FunctionDef) and node is not method
    }
    found: list[tuple[str, ast.AST]] = []
    for node in ast.walk(method):
        if not (isinstance(node, ast.Call) and _is_repository_mutate(node)):
            continue
        if len(node.args) < 2:
            continue
        callable_arg = node.args[1]
        if isinstance(callable_arg, ast.Name) and callable_arg.id in nested:
            found.append((callable_arg.id, nested[callable_arg.id]))
        elif isinstance(callable_arg, ast.Lambda):
            found.append((f"<lambda at line {callable_arg.lineno}>", callable_arg))
        else:
            raise AssertionError(
                f"{method.name} at line {node.lineno} passes something the "
                "invariant walk cannot resolve to mutate(); extend the walk."
            )
    return found


def _direct_network_calls(node_tree: ast.AST) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []
    for node in ast.walk(node_tree):
        if not isinstance(node, ast.Call):
            continue
        chain = _self_attribute_chain(node.func)
        if chain and chain[0] in _NETWORK_ATTRIBUTES:
            calls.append(("self." + ".".join(chain), node.lineno))
    return calls


def _network_calls(
    callable_node: ast.AST,
    *,
    method_name: str,
    class_methods: dict[str, ast.FunctionDef],
) -> list[tuple[str, int]]:
    """Network calls the callable makes directly or through one same-class helper.

    A call ``self._helper(...)`` resolves to the method of that name on the
    same class and that method's body is searched for direct network calls.
    Exactly one hop: a helper the helper calls is not followed. The point is
    to catch the helper that hides a round trip, not to build a whole-program
    analysis; a second hop is where a helper's own helpers live, and those
    belong to the helper's own contract.
    """

    calls = list(_direct_network_calls(callable_node))
    for node in ast.walk(callable_node):
        if not isinstance(node, ast.Call):
            continue
        chain = _self_attribute_chain(node.func)
        if len(chain) != 1 or chain[0] not in class_methods:
            continue
        helper = chain[0]
        if (method_name, helper) in _ALLOWED_HELPERS_IN_TRANSACTION:
            continue
        for call, lineno in _direct_network_calls(class_methods[helper]):
            calls.append(
                (
                    f"self.{helper} (called at line {node.lineno}) -> {call}",
                    lineno,
                )
            )
    return calls


def test_no_store_transaction_wraps_a_network_call() -> None:
    """No callable passed to ``_repository.mutate()`` may leave the process.

    ``SqliteLiveWorkspaceRepository.mutate()`` runs its callable under
    ``BEGIN IMMEDIATE``, so anything the callable does happens with the
    store's write lock held. A call on the authority transport, the executor
    or the supervisor runtime inside it is an HTTP round trip under that lock:
    every other writer waits for the remote service, up to the transport's
    timeout. That is a latency fault that only appears under concurrency and
    never in a single-threaded test run, which is exactly the kind of defect
    a test has to catch rather than a reviewer. The pattern that avoids it is
    the split used across the orchestrator: read, check the precondition,
    make the network call, then ``mutate()`` re-checking the same precondition
    on the fresh read before applying the result.

    The walk follows one level of ``self._helper(...)`` indirection into
    methods of the same class, because ``_ensure_context`` and the supervisor
    helpers are where a round trip hides one hop from the callable. The
    supervisor runtime counts even though today's only adapter is in-process:
    the product's second adapter is a live one, and a call on it inside a
    transaction would be a real round trip under the write lock.
    """

    source = inspect.getsource(orchestrator_module)
    tree = ast.parse(source)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert classes, "no class found in the orchestrator module"

    checked = 0
    offenders: list[str] = []
    for class_node in classes:
        class_methods = {
            node.name: node
            for node in class_node.body
            if isinstance(node, ast.FunctionDef)
        }
        for method in class_methods.values():
            for callable_name, callable_node in _store_transaction_callables(method):
                checked += 1
                for call, lineno in _network_calls(
                    callable_node,
                    method_name=method.name,
                    class_methods=class_methods,
                ):
                    offenders.append(
                        f"{method.name} -> {callable_name}: {call} at "
                        f"orchestrator.py:{lineno} runs inside a store transaction"
                    )

    assert checked > 0, "no _repository.mutate(...) call sites were found"
    assert offenders == [], "\n".join(offenders)
