from __future__ import annotations

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
