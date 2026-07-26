"""The canonical CSV proof, distributed across five people.

One approved decision must stop exactly three sessions and leave exactly two
running. This drives the real objects the demo uses — the orchestrator, the
interrupt port, and Lane B's session enforcement — so the number quoted on stage
is measured rather than asserted.

Ported from the pre-merge version, which called a `/check` service that no
longer exists.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import httpx
import pytest
import yaml
from dragback.domain import utc_now
from dragback.hashing import stable_hash
from dragback.intake.approval import ApprovalChannel, ApprovalEvidence
from dragback.services import authority_api, support
from dragback.supervisor_contract import InterruptRequest
from dragback.workspaces.authority_contexts import DynamicAuthorityContextRegistry
from dragback.workspaces.interrupt_port import WorkspaceSupervisorInterruptPort
from dragback.workspaces.models import (
    LiveWorkspaceImportRequest,
    WorkspaceApprovalRequest,
    WorkspaceProposalRequest,
)
from dragback.workspaces.orchestrator import LiveWorkspaceOrchestrator
from dragback.workspaces.repository import JsonFileLiveWorkspaceRepository
from dragback.workspaces.runtimes.claude_code import ClaudeCodeSupervisorRuntime
from dragback.workspaces.session_binding import ClaudeCodeSessionRegistry
from dragback.workspaces.session_enforcement import (
    ClaudeCodeSessionEnforcement,
    ClaudeHookVerdict,
    ClaudePreToolUseRequest,
    ClaudeSessionStartRequest,
    RepositorySupervisorAssignmentGateway,
)
from fastapi.testclient import TestClient
from pydantic import BaseModel

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
WORKSPACE_ID = "csv-exports"

# Two people on export.generation; three on export.authorization.
PRESERVED = {"TASK-201": "sara", "TASK-202": "alex"}
INTERRUPTED = {"TASK-203": "priya", "TASK-204": "marcus", "TASK-205": "dan"}

REDIRECT = (
    "Exports are admin-only. Gate the export control behind an administrator "
    "check before continuing."
)


def _load(name: str) -> dict[str, object]:
    with (EXAMPLES / name).open(encoding="utf-8") as handle:
        return cast(dict[str, object], yaml.safe_load(handle))


def _approval(
    repository: JsonFileLiveWorkspaceRepository,
    *,
    role: str,
    decision_id: str,
    baseline: bool,
) -> WorkspaceApprovalRequest:
    """An approval bound to the exact proposal on disk.

    Approvals are bound to a fingerprint and instance id so a proposal cannot
    change between confirmation and application. The test honours that binding
    rather than routing around it.
    """

    record = repository.get(WORKSPACE_ID)
    subject: BaseModel
    if baseline:
        subject = record.definition.baseline_decision
        instance_id = record.baseline_proposal_instance_id
    else:
        assert record.pending_mutation is not None
        # A change is fingerprinted over the whole mutation: the supersession
        # target and affected scopes are part of what a human confirmed.
        subject = record.pending_mutation
        instance_id = record.pending_proposal_instance_id
    assert instance_id is not None
    fingerprint = stable_hash(subject)
    return WorkspaceApprovalRequest(
        actor_role=role,
        proposal_fingerprint=fingerprint,
        proposal_instance_id=instance_id,
        approval_evidence=ApprovalEvidence(
            workspace_id=WORKSPACE_ID,
            decision_id=decision_id,
            approver_user_id=f"test:{role}",
            permission_id=role,
            channel=ApprovalChannel.CLI,
            evidence_ref=f"test://{WORKSPACE_ID}/{decision_id}",
            approved_at=utc_now(),
            confirmed_proposal_fingerprint=fingerprint,
            confirmed_proposal_instance_id=instance_id,
        ),
    )


def _apply_change(
    repository: JsonFileLiveWorkspaceRepository,
    root: Path,
) -> None:
    """Approve DEC-018 and deliver the correction across the frozen seam."""

    orchestrator = LiveWorkspaceOrchestrator(
        repository=repository,
        supervisor_runtime=ClaudeCodeSupervisorRuntime(),
    )
    orchestrator.propose_decision(
        WORKSPACE_ID,
        WorkspaceProposalRequest.model_validate(
            _load("dragback-five-sessions-change.yaml")
        ),
    )
    orchestrator.approve_decision(
        WORKSPACE_ID,
        "DEC-018",
        _approval(
            repository,
            role="approve_compliance",
            decision_id="DEC-018",
            baseline=False,
        ),
    )
    # Applying the decision stops the work; the interrupt port is what carries
    # the correction. Without a redirect_instruction the sessions hit a
    # permanent deny instead of deny-once.
    WorkspaceSupervisorInterruptPort(repository=repository).interrupt(
        InterruptRequest(
            workspace_id=WORKSPACE_ID,
            decision_id="DEC-018",
            affected_scopes=frozenset({"export.authorization"}),
            provenance_path=(),
            interrupt_reason="Approved decision DEC-018 changed export.authorization.",
            redirect_instruction=REDIRECT,
        )
    )


def _stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    apply_change: bool = True,
) -> tuple[ClaudeCodeSessionEnforcement, JsonFileLiveWorkspaceRepository, Path]:
    """Seed the graph, optionally apply the change, deliver the interrupt.

    Every authority call still crosses the real HTTP adapter; only the socket is
    replaced, so role, scope, confidence and the requirement match are all
    evaluated by the authority service exactly as in production.
    """

    authority = TestClient(authority_api.app)
    monkeypatch.setattr(
        authority_api,
        "workspace_contexts",
        DynamicAuthorityContextRegistry(
            grant_secret="five-session-test-secret",
            grant_ttl_seconds=3600,
            authority_threshold=0.75,
        ),
    )

    def route(method: str) -> object:
        def call(url: str, **kwargs: object) -> httpx.Response:
            parsed = urlparse(url)
            if parsed.port != 8001:
                raise httpx.ConnectError(
                    f"Unexpected service URL: {url}",
                    request=httpx.Request(method, url),
                )
            headers = cast(dict[str, str], kwargs.get("headers", {}))
            if method == "POST":
                return authority.post(
                    parsed.path,
                    json=kwargs.get("json"),
                    headers=headers,
                )
            if method == "DELETE":
                return authority.delete(parsed.path, headers=headers)
            return authority.get(parsed.path, headers=headers)

        return call

    monkeypatch.setattr(support.httpx, "post", route("POST"))
    monkeypatch.setattr(support.httpx, "get", route("GET"))
    monkeypatch.setattr(support.httpx, "delete", route("DELETE"))

    repository = JsonFileLiveWorkspaceRepository(tmp_path / "live-workspaces.json")
    runtime = ClaudeCodeSupervisorRuntime()
    orchestrator = LiveWorkspaceOrchestrator(
        repository=repository,
        supervisor_runtime=runtime,
    )
    orchestrator.import_workspace(
        LiveWorkspaceImportRequest.model_validate(_load("dragback-five-sessions.yaml"))
    )
    baseline_id = repository.get(WORKSPACE_ID).definition.baseline_decision.id
    orchestrator.approve_baseline(
        WORKSPACE_ID,
        _approval(
            repository,
            role="approve_product",
            decision_id=baseline_id,
            baseline=True,
        ),
    )
    orchestrator.authorize(WORKSPACE_ID)
    if apply_change:
        _apply_change(repository, tmp_path)
    return (
        ClaudeCodeSessionEnforcement(
            registry=ClaudeCodeSessionRegistry(),
            assignments=RepositorySupervisorAssignmentGateway(
                repository=repository,
                runtime=runtime,
            ),
        ),
        repository,
        tmp_path,
    )


@pytest.fixture
def enforcement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ClaudeCodeSessionEnforcement, JsonFileLiveWorkspaceRepository, Path]:
    return _stack(tmp_path, monkeypatch)


def _register(
    enforcement: ClaudeCodeSessionEnforcement,
    root: Path,
    task_id: str,
    person: str,
) -> str:
    """One session per person, bound by its own `.dragback/task` file."""

    cwd = root / person
    (cwd / ".dragback").mkdir(parents=True, exist_ok=True)
    (cwd / ".dragback" / "task").write_text(f"{task_id}\n", encoding="utf-8")
    session_id = f"session-{person}"
    binding = enforcement.start(
        ClaudeSessionStartRequest(session_id=session_id, cwd=str(cwd), branch="")
    ).binding
    assert binding.assignment is not None, f"{person} failed to bind"
    assert binding.assignment.task_id == task_id
    return session_id


def _check(
    enforcement: ClaudeCodeSessionEnforcement,
    session_id: str,
    ack: str | None = None,
) -> ClaudeHookVerdict:
    """One PreToolUse call, modelling what the real hook sends.

    `ack` is the `redirect_id` from this session's previous deny. The hook
    records it only once that deny is on stdout and echoes it here, and the
    service advances the assignment only when it arrives — so a deny that was
    issued but never delivered is re-delivered rather than silently spent.
    """

    return enforcement.check(
        ClaudePreToolUseRequest(
            session_id=session_id,
            tool_name="Edit",
            timestamp=utc_now(),
            acknowledged_redirect_id=ack,
        )
    )


def test_one_approved_change_stops_exactly_three_and_leaves_exactly_two(
    enforcement: tuple[ClaudeCodeSessionEnforcement, JsonFileLiveWorkspaceRepository, Path],
) -> None:
    service, _repository, root = enforcement
    sessions = {
        task: _register(service, root, task, person)
        for task, person in {**PRESERVED, **INTERRUPTED}.items()
    }

    verdicts = {task: _check(service, sid) for task, sid in sessions.items()}
    denied = {t for t, v in verdicts.items() if v.decision.value == "deny"}
    allowed = {t for t, v in verdicts.items() if v.decision.value == "allow"}

    assert denied == set(INTERRUPTED)
    assert allowed == set(PRESERVED)


def test_every_denied_session_is_told_what_to_do_instead(
    enforcement: tuple[ClaudeCodeSessionEnforcement, JsonFileLiveWorkspaceRepository, Path],
) -> None:
    service, _repository, root = enforcement
    for task, person in INTERRUPTED.items():
        verdict = _check(service, _register(service, root, task, person))
        assert verdict.decision.value == "deny", task
        assert verdict.redirect_instruction == REDIRECT, task
        # A path to THIS task, not someone else's.
        assert verdict.provenance_path[0] == "DEC-018", task
        assert task in verdict.provenance_path, task
        assert not (set(verdict.provenance_path) & (set(INTERRUPTED) - {task})), task
        # The hook has 10,000 characters to render this in.
        assert len(json.dumps(verdict.model_dump(mode="json"))) < 10_000, task


def test_a_denied_session_is_denied_once_and_then_continues(
    enforcement: tuple[ClaudeCodeSessionEnforcement, JsonFileLiveWorkspaceRepository, Path],
) -> None:
    """Deny once, then advance. This is what makes it terminate."""

    service, _repository, root = enforcement
    session_id = _register(service, root, "TASK-203", "priya")

    denied = _check(service, session_id)
    assert denied.decision.value == "deny"
    assert denied.redirect_id
    assert _check(service, session_id, ack=denied.redirect_id).decision.value == "allow"


def test_the_preserved_pair_is_never_touched(
    enforcement: tuple[ClaudeCodeSessionEnforcement, JsonFileLiveWorkspaceRepository, Path],
) -> None:
    service, repository, root = enforcement
    for task, person in PRESERVED.items():
        assert _check(service, _register(service, root, task, person)).decision.value == "allow"

    supervisor = repository.get(WORKSPACE_ID).supervisor
    assert supervisor is not None
    for assignment in supervisor.assignments:
        if assignment.task_id in PRESERVED:
            assert assignment.interrupt_reason is None, assignment.task_id
            assert assignment.interrupt_enforced is False, assignment.task_id
            assert assignment.redirect_instruction is None, assignment.task_id


def test_the_decision_never_names_the_ticket_or_any_task() -> None:
    """The graph finds the work through lineage, not through a mention."""

    decision = cast(
        dict[str, object], _load("dragback-five-sessions-change.yaml")["decision"]
    )
    text = f"{decision['title']} {decision['text']}".casefold()
    for artifact in {"TICKET-100", "PLAN-027", *PRESERVED, *INTERRUPTED}:
        assert artifact.casefold() not in text


def test_all_five_are_allowed_before_the_change_is_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The baseline half of the beat: nobody is denied until a human approves."""

    service, _repository, root = _stack(tmp_path, monkeypatch, apply_change=False)
    for task, person in {**PRESERVED, **INTERRUPTED}.items():
        verdict = _check(service, _register(service, root, task, person))
        assert verdict.decision.value == "allow", task


def test_the_blast_radius_equals_the_set_that_actually_gets_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`docs/BUILD_LANE_A.md`: the previewed count must equal reality.

    The number an approver sees on the confirmation screen is the product's
    safety claim. If it can differ from the set that is actually stopped, the
    screen is lying.
    """

    service, repository, root = _stack(tmp_path, monkeypatch, apply_change=False)
    for task, person in {**PRESERVED, **INTERRUPTED}.items():
        _register(service, root, task, person)

    # What the approver is shown, before the change is applied.
    preview = WorkspaceSupervisorInterruptPort(repository=repository).preview(
        InterruptRequest(
            workspace_id=WORKSPACE_ID,
            decision_id="DEC-018",
            affected_scopes=frozenset({"export.authorization"}),
            provenance_path=(),
            interrupt_reason="Exports must be admin-only.",
            redirect_instruction=REDIRECT,
        )
    )
    previewed = {a.removeprefix("ASSIGNMENT-") for a in preview.interrupted_assignment_ids}

    _apply_change(repository, root)

    denied = {
        task
        for task in {**PRESERVED, **INTERRUPTED}
        if _check(service, f"session-{ {**PRESERVED, **INTERRUPTED}[task] }").decision.value
        == "deny"
    }
    assert previewed == denied
    assert len(previewed) == 3


def test_every_session_continues_on_its_second_call(
    enforcement: tuple[ClaudeCodeSessionEnforcement, JsonFileLiveWorkspaceRepository, Path],
) -> None:
    """Deny once, then all five are working again. This is what terminates."""

    service, _repository, root = enforcement
    sessions = {
        task: _register(service, root, task, person)
        for task, person in {**PRESERVED, **INTERRUPTED}.items()
    }
    first = {task: _check(service, sid) for task, sid in sessions.items()}
    for task, sid in sessions.items():
        assert _check(
            service, sid, ack=first[task].redirect_id
        ).decision.value == "allow", task


def test_the_session_list_route_is_authenticated_and_lists_unbound_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`dev status` reads this. It must authenticate, and must not hide gaps."""

    from dragback.services.supervisor_api import (
        HookApiKeyVerifier,
        build_supervisor_session_router,
    )
    from dragback.services.support import install_api_support
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as _TestClient

    service, _repository, root = _stack(tmp_path, monkeypatch)
    # The router captures its verifier when it is built, so inject one rather
    # than setting the environment after import.
    app = FastAPI()
    install_api_support(app)
    app.include_router(
        build_supervisor_session_router(
            service,
            api_key_verifier=HookApiKeyVerifier(expected_api_key="test-key"),
        )
    )
    client = _TestClient(app)

    _register(service, root, "TASK-203", "priya")
    # A session in a directory with no marker file binds to nothing.
    unbound = root / "stranger"
    unbound.mkdir(parents=True, exist_ok=True)
    service.start(
        ClaudeSessionStartRequest(session_id="stranger", cwd=str(unbound), branch="")
    )

    assert client.get("/supervisor/sessions").status_code == 401

    listed = client.get(
        "/supervisor/sessions", headers={"X-Dragback-Hook-API-Key": "test-key"}
    )
    assert listed.status_code == 200
    sessions = listed.json()["sessions"]
    by_id = {item["session_id"]: item for item in sessions}
    assert by_id["session-priya"]["assignment"]["task_id"] == "TASK-203"
    # Unbound sessions are listed and visibly unbound: they are allowed
    # everything, so hiding them would turn a visible gap into a silent one.
    assert by_id["stranger"]["assignment"] is None
