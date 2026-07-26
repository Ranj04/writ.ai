from __future__ import annotations

import json
import signal
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from dragback.cli import (
    AgentAssignment,
    AgentController,
    CliError,
    ManagedProcess,
    SubprocessRunner,
    build_agent_command,
    build_agent_prompt,
    parse_sse_lines,
    run,
)

WORKSPACE_ID = "voyagr-reservation"
TASK_ID = "TASK-CALL-GUEST"


def _assignment(
    *,
    task_id: str = TASK_ID,
    run_id: str = "RUN-CALL-17",
    state: str = "running",
    provider: str = "codex",
    redirected_from_run_id: str | None = None,
    redirect_instruction: str | None = None,
    authorized_actions: Sequence[str] | None = None,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "agent_name": "guest-call-agent",
        "runtime_provider": provider,
        "execution_mode": "simulated",
        "run_id": run_id,
        "state": state,
        "scopes": ["booking.guest_contact"],
        "authorized_actions": list(
            authorized_actions
            or (
                "Contact the guest about the approved reservation time.",
                "Place the approved reservation call at 7:00 PM.",
            )
        ),
        "plan_id": "PLAN-VOYAGR-001",
        "decision_snapshot": "graph-v17",
        "redirected_from_run_id": redirected_from_run_id,
        "interrupt_reason": (
            "The approved arrival time changed." if state == "interrupted" else None
        ),
        "redirect_instruction": redirect_instruction,
        "provenance_path": [
            "DEC-VOYAGR-002",
            "SPEC-VOYAGR",
            "TICKET-VOYAGR",
            TASK_ID,
            "PLAN-VOYAGR-001",
        ],
        "interrupt_enforced": state in {"interrupted", "resumed"},
    }


def _workspace(*assignments: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": WORKSPACE_ID,
        "status": "change-applied",
        "supervisor": {"assignments": list(assignments), "simulated": True},
    }


class FakeProcess(ManagedProcess):
    def __init__(
        self,
        *,
        exits_after_interrupt: bool,
        exits_after_terminate: bool = True,
        exits_after_kill: bool = True,
    ) -> None:
        self.running = True
        self.exits_after_interrupt = exits_after_interrupt
        self.exits_after_terminate = exits_after_terminate
        self.exits_after_kill = exits_after_kill
        self.signals: list[signal.Signals] = []
        self.wait_timeouts: list[float] = []
        self.terminated = False
        self.killed = False

    def is_running(self) -> bool:
        return self.running

    def interrupt(self) -> None:
        self.signals.append(signal.SIGINT)

    def wait(self, timeout_seconds: float) -> bool:
        self.wait_timeouts.append(timeout_seconds)
        if not self.running:
            return True
        if self.exits_after_interrupt:
            self.running = False
            return True
        return False

    def terminate(self) -> None:
        self.terminated = True
        if self.exits_after_terminate:
            self.running = False

    def kill(self) -> None:
        self.killed = True
        if self.exits_after_kill:
            self.running = False


class FakeRunner:
    def __init__(
        self,
        process_outcomes: Sequence[bool] = (True,),
        *,
        terminate_outcomes: Sequence[bool] | None = None,
        kill_outcomes: Sequence[bool] | None = None,
    ) -> None:
        self.process_outcomes = list(process_outcomes)
        self.terminate_outcomes = list(
            terminate_outcomes
            if terminate_outcomes is not None
            else [True] * len(process_outcomes)
        )
        self.kill_outcomes = list(
            kill_outcomes
            if kill_outcomes is not None
            else [True] * len(process_outcomes)
        )
        self.starts: list[tuple[tuple[str, ...], Path, FakeProcess]] = []

    def start(self, command: Sequence[str], *, cwd: Path) -> FakeProcess:
        outcome = self.process_outcomes.pop(0)
        process = FakeProcess(
            exits_after_interrupt=outcome,
            exits_after_terminate=self.terminate_outcomes.pop(0),
            exits_after_kill=self.kill_outcomes.pop(0),
        )
        self.starts.append((tuple(command), cwd, process))
        return process


def _model(raw: Mapping[str, Any]) -> AgentAssignment:
    return AgentAssignment.from_mapping(raw)


def test_provider_commands_are_argv_lists_with_dragback_owned_controls(
    tmp_path: Path,
) -> None:
    codex_assignment = _model(_assignment())
    codex = build_agent_command(
        codex_assignment,
        provider="codex",
        workspace_id=WORKSPACE_ID,
        cwd=tmp_path,
        provider_args=("--model", "gpt-5"),
    )
    claude_assignment = _model(
        _assignment(provider="claude-code", run_id="RUN-CLAUDE-18")
    )
    claude = build_agent_command(
        claude_assignment,
        provider="claude-code",
        workspace_id=WORKSPACE_ID,
        cwd=tmp_path,
        provider_args=("--verbose",),
    )

    assert codex[:6] == (
        "codex",
        "-C",
        str(tmp_path),
        "--no-alt-screen",
        "--model",
        "gpt-5",
    )
    assert codex[-1].startswith("You are a coding subagent")
    assert claude[:4] == ("claude", "--name", "RUN-CLAUDE-18", "--verbose")
    assert claude[-1].startswith("You are a coding subagent")


def test_prompt_contains_bounded_assignment_and_redirect_context() -> None:
    assignment = _model(
        _assignment(
            state="resumed",
            run_id="RUN-CALL-18",
            redirected_from_run_id="RUN-CALL-17",
            redirect_instruction="Call at 8:30 PM, not 7:00 PM.",
            authorized_actions=(
                "Contact the guest about the changed reservation.",
                "Place the approved reservation call at 8:30 PM.",
            ),
        )
    )

    prompt = build_agent_prompt(assignment, workspace_id=WORKSPACE_ID)

    assert "Task: TASK-CALL-GUEST" in prompt
    assert "Run: RUN-CALL-18" in prompt
    assert "Decision snapshot: graph-v17" in prompt
    assert "Authorized actions:" in prompt
    assert "Place the approved reservation call at 8:30 PM." in prompt
    assert "Call at 8:30 PM, not 7:00 PM." in prompt
    assert "DEC-VOYAGR-002 -> SPEC-VOYAGR" in prompt
    assert "Do not infer, mint, or reuse authorization" in prompt


def test_initial_prompt_contains_actual_authorized_work() -> None:
    prompt = build_agent_prompt(
        _model(_assignment()),
        workspace_id=WORKSPACE_ID,
    )

    assert "Authorized actions:" in prompt
    assert "Contact the guest about the approved reservation time." in prompt
    assert "Place the approved reservation call at 7:00 PM." in prompt


def test_unsafe_provider_overrides_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(CliError, match="override Dragback controls"):
        build_agent_command(
            _model(_assignment()),
            provider="codex",
            workspace_id=WORKSPACE_ID,
            cwd=tmp_path,
            provider_args=("--yolo",),
        )


@pytest.mark.parametrize(
    "unsafe_option",
    (
        "-s",
        "-awriter",
        "-cmodel_reasoning_effort=low",
        "-nunsafe-name",
        "--sandbox",
        "--ask-for-approval=never",
        "--config",
        "--add-dir",
        "--allow-dangerously-skip-permissions",
    ),
)
def test_provider_arguments_cannot_override_dragback_controls(
    unsafe_option: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(CliError, match="override Dragback controls"):
        build_agent_command(
            _model(_assignment()),
            provider="codex",
            workspace_id=WORKSPACE_ID,
            cwd=tmp_path,
            provider_args=(unsafe_option,),
        )


@pytest.mark.parametrize("provider", ["codex", "claude-code"])
def test_generic_assignment_accepts_developer_selected_provider(
    provider: str,
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    controller = AgentController(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        provider=provider,
        cwd=tmp_path,
        provider_args=(),
        interrupt_timeout=1,
        process_runner=runner,
    )

    controller.launch_initial(
        _workspace(_assignment(provider="generic", state="running"))
    )

    assert len(runner.starts) == 1
    expected_executable = "codex" if provider == "codex" else "claude"
    assert runner.starts[0][0][0] == expected_executable


@pytest.mark.parametrize("state", ["queued", "redirected"])
def test_controller_never_launches_non_runnable_assignment_states(
    state: str,
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    controller = AgentController(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        provider="codex",
        cwd=tmp_path,
        provider_args=(),
        interrupt_timeout=1,
        process_runner=runner,
    )

    controller.launch_initial(_workspace(_assignment(state=state)))

    assert runner.starts == []


def test_queued_assignment_launches_only_after_running_ping(tmp_path: Path) -> None:
    runner = FakeRunner()
    controller = AgentController(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        provider="codex",
        cwd=tmp_path,
        provider_args=(),
        interrupt_timeout=1,
        process_runner=runner,
    )
    controller.launch_initial(_workspace(_assignment(state="queued")))

    controller.handle_workspace(_workspace(_assignment(state="running")))

    assert len(runner.starts) == 1


def test_redirected_assignment_waits_for_resumed_ping(tmp_path: Path) -> None:
    runner = FakeRunner()
    controller = AgentController(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        provider="codex",
        cwd=tmp_path,
        provider_args=(),
        interrupt_timeout=1,
        process_runner=runner,
    )
    controller.launch_initial(
        _workspace(
            _assignment(
                state="redirected",
                run_id="RUN-CALL-18",
                redirected_from_run_id="RUN-CALL-17",
            )
        )
    )

    controller.handle_workspace(
        _workspace(
            _assignment(
                state="running",
                run_id="RUN-CALL-18",
                redirected_from_run_id="RUN-CALL-17",
            )
        )
    )
    assert runner.starts == []

    controller.handle_workspace(
        _workspace(
            _assignment(
                state="resumed",
                run_id="RUN-CALL-18",
                redirected_from_run_id="RUN-CALL-17",
            )
        )
    )
    assert len(runner.starts) == 1


def test_sse_parser_handles_comments_and_multiline_json() -> None:
    envelopes = list(
        parse_sse_lines(
            [
                "retry: 2000",
                ": keep-alive",
                "event: live-workspace.supervisor.snapshot",
                'data: {"event":"live-workspace.supervisor.snapshot",',
                'data: "data":{"id":"voyagr-reservation"}}',
                "",
            ]
        )
    )

    assert envelopes == [
        {
            "event": "live-workspace.supervisor.snapshot",
            "data": {"id": WORKSPACE_ID},
        }
    ]


def test_controller_filters_sibling_tasks_and_escalates_after_sigint(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(process_outcomes=(False,))
    controller = AgentController(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        provider="codex",
        cwd=tmp_path,
        provider_args=(),
        interrupt_timeout=2.5,
        process_runner=runner,
    )
    controller.launch_initial(_workspace(_assignment()))
    process = runner.starts[0][2]

    controller.handle_workspace(
        _workspace(
            _assignment(),
            _assignment(
                task_id="TASK-PREPARE-SUMMARY",
                run_id="RUN-SUMMARY-17",
                state="interrupted",
            ),
        )
    )
    assert process.signals == []

    controller.handle_workspace(_workspace(_assignment(state="interrupted")))

    assert process.signals == [signal.SIGINT]
    assert process.wait_timeouts == [2.5, 2.5]
    assert process.terminated is True


def test_controller_uses_bounded_kill_fallback_before_relaunch(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        process_outcomes=(False, True),
        terminate_outcomes=(False, True),
        kill_outcomes=(True, True),
    )
    controller = AgentController(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        provider="codex",
        cwd=tmp_path,
        provider_args=(),
        interrupt_timeout=0.5,
        process_runner=runner,
    )
    controller.launch_initial(_workspace(_assignment()))
    process = runner.starts[0][2]

    controller.handle_workspace(_workspace(_assignment(state="interrupted")))

    assert process.signals == [signal.SIGINT]
    assert process.terminated is True
    assert process.killed is True
    assert process.wait_timeouts == [0.5, 0.5, 0.5]
    assert controller.active_process is None

    controller.handle_workspace(
        _workspace(
            _assignment(
                state="resumed",
                run_id="RUN-CALL-18",
                redirected_from_run_id="RUN-CALL-17",
            )
        )
    )
    assert len(runner.starts) == 2


def test_controller_blocks_relaunch_when_old_process_survives_kill(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        process_outcomes=(False,),
        terminate_outcomes=(False,),
        kill_outcomes=(False,),
    )
    controller = AgentController(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        provider="codex",
        cwd=tmp_path,
        provider_args=(),
        interrupt_timeout=0.1,
        process_runner=runner,
    )
    controller.launch_initial(_workspace(_assignment()))
    controller.handle_workspace(_workspace(_assignment(state="interrupted")))
    controller.handle_workspace(
        _workspace(
            _assignment(
                state="resumed",
                run_id="RUN-CALL-18",
                redirected_from_run_id="RUN-CALL-17",
            )
        )
    )

    assert len(runner.starts) == 1
    assert controller.active_process is runner.starts[0][2]


def test_controller_relaunches_only_linked_resumed_run(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(process_outcomes=(True, True))
    controller = AgentController(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        provider="codex",
        cwd=tmp_path,
        provider_args=(),
        interrupt_timeout=1,
        process_runner=runner,
    )
    controller.launch_initial(_workspace(_assignment()))
    controller.handle_workspace(_workspace(_assignment(state="interrupted")))

    controller.handle_workspace(
        _workspace(
            _assignment(
                state="resumed",
                run_id="RUN-UNLINKED",
                redirected_from_run_id="SOME-OTHER-RUN",
            )
        )
    )
    assert len(runner.starts) == 1

    controller.handle_workspace(
        _workspace(
            _assignment(
                state="resumed",
                run_id="RUN-CALL-18",
                redirected_from_run_id="RUN-CALL-17",
                redirect_instruction="Use the newly approved 8:30 PM time.",
            )
        )
    )

    assert len(runner.starts) == 2
    assert runner.starts[1][0][0] == "codex"
    assert "newly approved 8:30 PM" in runner.starts[1][0][-1]

    controller.handle_workspace(
        _workspace(
            _assignment(
                state="resumed",
                run_id="RUN-CALL-18",
                redirected_from_run_id="RUN-CALL-17",
            )
        )
    )
    assert len(runner.starts) == 2


def test_snapshot_jump_stops_old_run_then_waits_for_resumed_replacement(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(process_outcomes=(True, True))
    controller = AgentController(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        provider="codex",
        cwd=tmp_path,
        provider_args=(),
        interrupt_timeout=1,
        process_runner=runner,
    )
    controller.launch_initial(_workspace(_assignment()))
    old_process = runner.starts[0][2]

    controller.handle_workspace(
        _workspace(
            _assignment(
                state="redirected",
                run_id="RUN-CALL-18",
                redirected_from_run_id="RUN-CALL-17",
            )
        )
    )

    assert old_process.signals == [signal.SIGINT]
    assert len(runner.starts) == 1
    assert controller.active_process is None

    controller.handle_workspace(
        _workspace(
            _assignment(
                state="resumed",
                run_id="RUN-CALL-18",
                redirected_from_run_id="RUN-CALL-17",
            )
        )
    )

    assert len(runner.starts) == 2
    assert runner.starts[1][0][0] == "codex"


def test_snapshot_jump_directly_to_resumed_stops_old_run_before_relaunch(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(process_outcomes=(True, True))
    controller = AgentController(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        provider="codex",
        cwd=tmp_path,
        provider_args=(),
        interrupt_timeout=1,
        process_runner=runner,
    )
    controller.launch_initial(_workspace(_assignment()))
    old_process = runner.starts[0][2]

    controller.handle_workspace(
        _workspace(
            _assignment(
                state="resumed",
                run_id="RUN-CALL-18",
                redirected_from_run_id="RUN-CALL-17",
            )
        )
    )

    assert old_process.signals == [signal.SIGINT]
    assert len(runner.starts) == 2
    assert controller.active_process is runner.starts[1][2]


def test_completed_supervisor_stops_child_and_marks_controller_terminal(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    controller = AgentController(
        workspace_id=WORKSPACE_ID,
        task_id=TASK_ID,
        provider="codex",
        cwd=tmp_path,
        provider_args=(),
        interrupt_timeout=1,
        process_runner=runner,
    )
    controller.launch_initial(_workspace(_assignment()))
    completed = _workspace(_assignment(state="continuing"))
    assert isinstance(completed["supervisor"], dict)
    completed["supervisor"]["state"] = "completed"

    controller.handle_workspace(completed)

    assert controller.terminal is True
    assert runner.starts[0][2].signals == [signal.SIGINT]
    assert controller.active_process is None


def test_dry_run_never_starts_agent_and_never_prints_tokens(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeRunner()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/live-workspaces/{WORKSPACE_ID}"
        payload = _workspace(_assignment())
        payload["grant_token"] = "must-never-appear"
        payload["nested"] = {"signed_token": "also-secret"}
        return httpx.Response(200, json=payload)

    exit_code = run(
        [
            "agent",
            "--timeout",
            "3",
            "run",
            "--workspace",
            WORKSPACE_ID,
            "--task",
            TASK_ID,
            "--provider",
            "codex",
            "--cwd",
            str(tmp_path),
            "--dry-run",
            "--",
            "--model",
            "gpt-5",
        ],
        transport=httpx.MockTransport(handler),
        process_runner=runner,
    )

    assert exit_code == 0
    assert runner.starts == []
    output = capsys.readouterr().out
    assert "[SIMULATED SUPERVISOR DATA]" in output
    assert "DRY RUN" in output
    assert "--model gpt-5" in output
    assert "must-never-appear" not in output
    assert "also-secret" not in output


def test_run_rejects_missing_or_wrong_task_assignment(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json=_workspace(
                _assignment(task_id="TASK-PREPARE-SUMMARY"),
            ),
        )
    )

    exit_code = run(
        [
            "agent",
            "run",
            WORKSPACE_ID,
            "--task",
            TASK_ID,
            "--provider",
            "codex",
            "--cwd",
            str(tmp_path),
            "--dry-run",
        ],
        transport=response,
        process_runner=FakeRunner(),
    )

    assert exit_code == 2
    assert "No supervisor assignment exists" in capsys.readouterr().err


def test_run_consumes_workspace_stream_and_redirects_without_real_agents(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(process_outcomes=(False, True))
    sibling = _assignment(
        task_id="TASK-PREPARE-SUMMARY",
        run_id="RUN-SUMMARY-17",
        state="interrupted",
    )
    interrupted = _assignment(state="interrupted")
    resumed = _assignment(
        state="resumed",
        run_id="RUN-CALL-18",
        redirected_from_run_id="RUN-CALL-17",
        redirect_instruction="Call only at the approved 8:30 PM time.",
        authorized_actions=(
            "Contact the guest about the changed reservation.",
            "Call only at the approved 8:30 PM time.",
        ),
    )
    completed = _assignment(
        state="completed",
        run_id="RUN-CALL-18",
        redirected_from_run_id="RUN-CALL-17",
        authorized_actions=(
            "Contact the guest about the changed reservation.",
            "Call only at the approved 8:30 PM time.",
        ),
    )
    events = "".join(
        (
            "event: live-workspace.supervisor.snapshot\n",
            "data: "
            + json.dumps(
                {
                    "event": "live-workspace.supervisor.snapshot",
                    "data": _workspace(_assignment(), sibling),
                }
            )
            + "\n\n",
            "event: live-workspace.supervisor.changed\n",
            "data: "
            + json.dumps(
                {
                    "event": "live-workspace.supervisor.changed",
                    "data": _workspace(interrupted),
                }
            )
            + "\n\n",
            "event: live-workspace.supervisor.changed\n",
            "data: "
            + json.dumps(
                {
                    "event": "live-workspace.supervisor.changed",
                    "data": _workspace(resumed),
                }
            )
            + "\n\n",
            "event: live-workspace.supervisor.changed\n",
            "data: "
            + json.dumps(
                {
                    "event": "live-workspace.supervisor.changed",
                    "data": _workspace(completed),
                }
            )
            + "\n\n",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                text=events,
                headers={"Content-Type": "text/event-stream"},
            )
        pytest.fail("non-dry-run must subscribe before launching")

    exit_code = run(
        [
            "agent",
            "run",
            WORKSPACE_ID,
            "--task",
            TASK_ID,
            "--provider",
            "codex",
            "--cwd",
            str(tmp_path),
            "--interrupt-timeout",
            "0.25",
        ],
        transport=httpx.MockTransport(handler),
        process_runner=runner,
    )

    assert exit_code == 0
    assert len(runner.starts) == 2
    first_process = runner.starts[0][2]
    assert first_process.signals == [signal.SIGINT]
    assert first_process.terminated is True
    assert "8:30 PM" in runner.starts[1][0][-1]
    assert runner.starts[1][2].signals == [signal.SIGINT]


def test_event_stream_http_error_never_launches_child(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeRunner()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(
                503,
                json={
                    "error": {
                        "code": "EVENTS_UNAVAILABLE",
                        "message": "Event stream unavailable.",
                    }
                },
            )
        return httpx.Response(200, json=_workspace(_assignment()))

    exit_code = run(
        [
            "agent",
            "run",
            WORKSPACE_ID,
            "--task",
            TASK_ID,
            "--provider",
            "codex",
            "--cwd",
            str(tmp_path),
        ],
        transport=httpx.MockTransport(handler),
        process_runner=runner,
    )

    assert exit_code == 2
    assert runner.starts == []
    assert "Event stream unavailable" in capsys.readouterr().err


def test_malformed_stream_after_snapshot_cleans_up_child(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeRunner()
    snapshot = {
        "event": "live-workspace.supervisor.snapshot",
        "data": _workspace(_assignment()),
    }
    stream = (
        "event: live-workspace.supervisor.snapshot\n"
        f"data: {json.dumps(snapshot)}\n\n"
        "event: live-workspace.supervisor.changed\n"
        "data: {not-json}\n\n"
    )

    exit_code = run(
        [
            "agent",
            "run",
            "--workspace",
            WORKSPACE_ID,
            "--task",
            TASK_ID,
            "--provider",
            "codex",
            "--cwd",
            str(tmp_path),
        ],
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                text=stream,
                headers={"Content-Type": "text/event-stream"},
            )
        ),
        process_runner=runner,
    )

    assert exit_code == 2
    assert runner.starts[0][2].signals == [signal.SIGINT]
    assert "invalid JSON" in capsys.readouterr().err


def test_clean_stream_eof_is_an_error_and_cleans_up_child(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeRunner()
    snapshot = {
        "event": "live-workspace.supervisor.snapshot",
        "data": _workspace(_assignment()),
    }

    exit_code = run(
        [
            "agent",
            "run",
            WORKSPACE_ID,
            "--task",
            TASK_ID,
            "--provider",
            "codex",
            "--cwd",
            str(tmp_path),
        ],
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                text=(
                    "event: live-workspace.supervisor.snapshot\n"
                    f"data: {json.dumps(snapshot)}\n\n"
                ),
                headers={"Content-Type": "text/event-stream"},
            )
        ),
        process_runner=runner,
    )

    assert exit_code == 2
    assert runner.starts[0][2].signals == [signal.SIGINT]
    assert "closed before the assignment completed" in capsys.readouterr().err


def test_ctrl_c_cleans_up_child_and_returns_130(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeRunner()
    snapshot = {
        "event": "live-workspace.supervisor.snapshot",
        "data": _workspace(_assignment()),
    }

    class InterruptingStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield (
                "event: live-workspace.supervisor.snapshot\n"
                f"data: {json.dumps(snapshot)}\n\n"
            ).encode()
            raise KeyboardInterrupt

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                stream=InterruptingStream(),
                headers={"Content-Type": "text/event-stream"},
            )
        pytest.fail("non-dry-run must subscribe before launching")

    exit_code = run(
        [
            "agent",
            "run",
            WORKSPACE_ID,
            "--task",
            TASK_ID,
            "--provider",
            "codex",
            "--cwd",
            str(tmp_path),
        ],
        transport=httpx.MockTransport(handler),
        process_runner=runner,
    )

    assert exit_code == 130
    assert runner.starts[0][2].signals == [signal.SIGINT]
    assert "supervised process stopped" in capsys.readouterr().err


def test_subprocess_launch_error_is_a_safe_cli_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_executable(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("dragback.cli.subprocess.Popen", missing_executable)

    with pytest.raises(CliError) as captured:
        SubprocessRunner().start(("codex", "prompt"), cwd=tmp_path)

    assert captured.value.code == "PROVIDER_NOT_FOUND"
    assert "codex" in captured.value.message


def test_subprocess_runner_preserves_callers_interactive_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class DummyPopen:
        def poll(self) -> int:
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return DummyPopen()

    monkeypatch.setattr("dragback.cli.subprocess.Popen", fake_popen)

    SubprocessRunner().start(("codex", "prompt"), cwd=tmp_path)

    assert captured["command"] == ["codex", "prompt"]
    assert captured["shell"] is False
    assert "start_new_session" not in captured
    assert "stdin" not in captured
    assert "stdout" not in captured
    assert "stderr" not in captured
