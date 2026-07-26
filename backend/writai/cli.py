from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, NoReturn, Protocol
from urllib.parse import quote, urlsplit

import httpx
import yaml

from writai import cli_approve, cli_dev

DEFAULT_AGENT_URL = "http://127.0.0.1:8002"
DEFAULT_TIMEOUT_SECONDS = 15.0

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class Route:
    method: str
    path: str


# The public API contract is intentionally centralized here. The CLI contains no
# authority logic and can follow additive API changes without changing commands.
ROUTES: dict[str, Route] = {
    "import": Route("POST", "/live-workspaces/import"),
    "list": Route("GET", "/live-workspaces"),
    "show": Route("GET", "/live-workspaces/{workspace_id}"),
    "approve-baseline": Route(
        "POST", "/live-workspaces/{workspace_id}/baseline/approve"
    ),
    "authorize": Route("POST", "/live-workspaces/{workspace_id}/authorize"),
    "propose-change": Route(
        "POST", "/live-workspaces/{workspace_id}/decisions/propose"
    ),
    "approve-change": Route(
        "POST",
        "/live-workspaces/{workspace_id}/decisions/{decision_id}/approve",
    ),
    "cancel-change": Route(
        "DELETE", "/live-workspaces/{workspace_id}/decisions/pending"
    ),
    "update-plan": Route("PUT", "/live-workspaces/{workspace_id}/plan"),
    "reauthorize": Route("POST", "/live-workspaces/{workspace_id}/reauthorize"),
    "verify-initial": Route(
        "POST", "/live-workspaces/{workspace_id}/grants/initial/verify"
    ),
    "verify-replacement": Route(
        "POST", "/live-workspaces/{workspace_id}/grants/replacement/verify"
    ),
    "agent-events": Route("GET", "/live-workspaces/{workspace_id}/events"),
    "replay-crustdata": Route("POST", "/intake/crustdata/person/replay"),
}

_TOKEN_KEYS = {
    "token",
    "signed_token",
    "grant_token",
    "signature",
}
_DETERMINISTIC_VERIFICATION_CODES = {
    "BINDING_MISMATCH",
    "CURRENT_PLAN_REJECTED",
    "EXPIRED",
    "INVALID_TOKEN",
    "NON_ALLOW_VERDICT",
    "PLAN_HASH_MISMATCH",
    "STALE_SNAPSHOT",
}


class CliError(Exception):
    """A safe error suitable for terminal output."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "CLI_ERROR",
        status_code: int | None = None,
        payload: JsonObject | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.payload = payload


class WritaiArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


class WritaiClient:
    def __init__(
        self,
        *,
        agent_url: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized_url = agent_url.strip().rstrip("/")
        if not normalized_url:
            raise CliError("The agent service URL cannot be empty.", code="INVALID_URL")
        parsed_url = urlsplit(normalized_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise CliError(
                "The agent service URL must be an absolute HTTP(S) URL.",
                code="INVALID_URL",
            )
        self._client = httpx.Client(
            base_url=normalized_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "writai-cli/0.1"},
        )

    def __enter__(self) -> WritaiClient:
        self._client.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._client.__exit__(exc_type, exc_value, traceback)

    def request(
        self,
        route: Route,
        *,
        workspace_id: str | None = None,
        decision_id: str | None = None,
        body: JsonObject | None = None,
        headers: dict[str, str] | None = None,
    ) -> JsonObject:
        path_values = {
            "workspace_id": _path_segment(workspace_id),
            "decision_id": _path_segment(decision_id),
        }
        path = route.path.format(**path_values)
        try:
            response = self._client.request(
                route.method, path, json=body, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise CliError(
                "The writ.ai agent service timed out.",
                code="TRANSPORT_TIMEOUT",
            ) from exc
        except httpx.RequestError as exc:
            raise CliError(
                f"Could not reach the writ.ai agent service: {exc}",
                code="TRANSPORT_ERROR",
            ) from exc

        payload = _response_payload(response)
        if response.is_success:
            return payload

        error = payload.get("error")
        error_mapping = error if isinstance(error, Mapping) else {}
        code = str(error_mapping.get("code") or f"HTTP_{response.status_code}")
        message = str(
            error_mapping.get("message")
            or payload.get("message")
            or f"The service returned HTTP {response.status_code}."
        )
        raise CliError(
            message,
            code=code,
            status_code=response.status_code,
            payload=payload,
        )

    def workspace_events(self, workspace_id: str) -> Iterator[JsonObject]:
        path = ROUTES["agent-events"].path.format(
            workspace_id=_path_segment(workspace_id)
        )
        try:
            with self._client.stream(
                "GET",
                path,
                headers={"Accept": "text/event-stream"},
                timeout=httpx.Timeout(
                    connect=self._client.timeout.connect,
                    read=None,
                    write=self._client.timeout.write,
                    pool=self._client.timeout.pool,
                ),
            ) as response:
                if not response.is_success:
                    response.read()
                    payload = _response_payload(response)
                    error = payload.get("error")
                    error_mapping = error if isinstance(error, Mapping) else {}
                    raise CliError(
                        str(
                            error_mapping.get("message")
                            or f"The service returned HTTP {response.status_code}."
                        ),
                        code=str(
                            error_mapping.get("code")
                            or f"HTTP_{response.status_code}"
                        ),
                        status_code=response.status_code,
                        payload=payload,
                    )
                yield from parse_sse_lines(response.iter_lines())
        except CliError:
            raise
        except httpx.TimeoutException as exc:
            raise CliError(
                "The writ.ai workspace event stream timed out.",
                code="TRANSPORT_TIMEOUT",
            ) from exc
        except httpx.RequestError as exc:
            raise CliError(
                f"Could not reach the writ.ai workspace event stream: {exc}",
                code="TRANSPORT_ERROR",
            ) from exc


class ManagedProcess(Protocol):
    def is_running(self) -> bool: ...

    def interrupt(self) -> None: ...

    def wait(self, timeout_seconds: float) -> bool: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class ProcessRunner(Protocol):
    def start(self, command: Sequence[str], *, cwd: Path) -> ManagedProcess: ...


class SubprocessManagedProcess:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    def is_running(self) -> bool:
        return self._process.poll() is None

    def interrupt(self) -> None:
        if self.is_running():
            self._process.send_signal(signal.SIGINT)

    def wait(self, timeout_seconds: float) -> bool:
        try:
            self._process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return False
        return True

    def terminate(self) -> None:
        if self.is_running():
            self._process.terminate()

    def kill(self) -> None:
        if self.is_running():
            self._process.kill()


class SubprocessRunner:
    """Launch provider CLIs directly; no shell ever interprets assignment data."""

    def start(self, command: Sequence[str], *, cwd: Path) -> ManagedProcess:
        try:
            process = subprocess.Popen(  # noqa: S603 - shell is disabled.
                list(command),
                cwd=str(cwd),
                shell=False,
            )
        except FileNotFoundError as exc:
            raise CliError(
                f"Provider executable {command[0]!r} was not found.",
                code="PROVIDER_NOT_FOUND",
            ) from exc
        except OSError as exc:
            raise CliError(
                f"Could not launch provider executable {command[0]!r}: {exc}",
                code="PROVIDER_LAUNCH_FAILED",
            ) from exc
        return SubprocessManagedProcess(process)


@dataclass(frozen=True)
class AgentAssignment:
    task_id: str
    agent_name: str
    runtime_provider: str
    execution_mode: str
    run_id: str
    state: str
    scopes: tuple[str, ...]
    authorized_actions: tuple[str, ...]
    plan_id: str
    decision_snapshot: str
    redirected_from_run_id: str | None
    interrupt_reason: str | None
    redirect_instruction: str | None
    provenance_path: tuple[str, ...]
    interrupt_enforced: bool

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> AgentAssignment:
        required = (
            "task_id",
            "agent_name",
            "runtime_provider",
            "execution_mode",
            "run_id",
            "state",
            "authorized_actions",
            "plan_id",
            "decision_snapshot",
        )
        missing = [key for key in required if raw.get(key) in (None, "")]
        if missing:
            raise CliError(
                "The supervisor assignment is missing required fields: "
                + ", ".join(missing),
                code="INVALID_ASSIGNMENT",
            )
        scopes = raw.get("scopes", [])
        authorized_actions = raw.get("authorized_actions", [])
        provenance_path = raw.get("provenance_path", [])
        if not isinstance(scopes, list) or not all(
            isinstance(scope, str) for scope in scopes
        ):
            raise CliError(
                "The supervisor assignment scopes are invalid.",
                code="INVALID_ASSIGNMENT",
            )
        if (
            not isinstance(authorized_actions, list)
            or not authorized_actions
            or len(authorized_actions) > 20
            or not all(
                isinstance(action, str)
                and 0 < len(action.strip()) <= 1000
                and "\x00" not in action
                for action in authorized_actions
            )
        ):
            raise CliError(
                "The supervisor assignment authorized actions are invalid.",
                code="INVALID_ASSIGNMENT",
            )
        if not isinstance(provenance_path, list) or not all(
            isinstance(node, str) for node in provenance_path
        ):
            raise CliError(
                "The supervisor assignment provenance path is invalid.",
                code="INVALID_ASSIGNMENT",
            )

        def optional_string(key: str) -> str | None:
            value = raw.get(key)
            return str(value) if value not in (None, "") else None

        return cls(
            task_id=str(raw["task_id"]),
            agent_name=str(raw["agent_name"]),
            runtime_provider=str(raw["runtime_provider"]),
            execution_mode=str(raw["execution_mode"]),
            run_id=str(raw["run_id"]),
            state=str(raw["state"]),
            scopes=tuple(sorted(scopes)),
            authorized_actions=tuple(
                action.strip() for action in authorized_actions
            ),
            plan_id=str(raw["plan_id"]),
            decision_snapshot=str(raw["decision_snapshot"]),
            redirected_from_run_id=optional_string("redirected_from_run_id"),
            interrupt_reason=optional_string("interrupt_reason"),
            redirect_instruction=optional_string("redirect_instruction"),
            provenance_path=tuple(provenance_path),
            interrupt_enforced=raw.get("interrupt_enforced") is True,
        )


def parse_sse_lines(lines: Iterable[str]) -> Iterator[JsonObject]:
    """Parse JSON SSE envelopes without trusting event names or terminal text."""

    data_lines: list[str] = []
    for line in lines:
        normalized = line.rstrip("\r\n")
        if normalized == "":
            if data_lines:
                raw_data = "\n".join(data_lines)
                data_lines.clear()
                try:
                    payload = json.loads(raw_data)
                except json.JSONDecodeError as exc:
                    raise CliError(
                        "The writ.ai event stream returned invalid JSON.",
                        code="INVALID_EVENT_STREAM",
                    ) from exc
                if not isinstance(payload, dict):
                    raise CliError(
                        "The writ.ai event stream returned an invalid envelope.",
                        code="INVALID_EVENT_STREAM",
                    )
                yield {str(key): value for key, value in payload.items()}
            continue
        if normalized.startswith(":"):
            continue
        field, separator, value = normalized.partition(":")
        if field != "data" or not separator:
            continue
        data_lines.append(value[1:] if value.startswith(" ") else value)
    if data_lines:
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise CliError(
                "The writ.ai event stream ended with invalid JSON.",
                code="INVALID_EVENT_STREAM",
            ) from exc
        if not isinstance(payload, dict):
            raise CliError(
                "The writ.ai event stream ended with an invalid envelope.",
                code="INVALID_EVENT_STREAM",
            )
        yield {str(key): value for key, value in payload.items()}


def _workspace_from_event(envelope: JsonObject) -> JsonObject | None:
    data = envelope.get("data")
    if not isinstance(data, Mapping):
        return None
    safe = _redact(data)
    return (
        {str(key): value for key, value in safe.items()}
        if isinstance(safe, Mapping)
        else None
    )


def _assignment_for_task(
    workspace: Mapping[str, Any],
    task_id: str,
    *,
    required: bool,
) -> AgentAssignment | None:
    supervisor = workspace.get("supervisor")
    assignments: Any = (
        supervisor.get("assignments") if isinstance(supervisor, Mapping) else None
    )
    if not isinstance(assignments, list):
        if required:
            raise CliError(
                "This workspace has no supervisor assignments.",
                code="ASSIGNMENT_NOT_FOUND",
            )
        return None
    matches = [
        item
        for item in assignments
        if isinstance(item, Mapping) and str(item.get("task_id")) == task_id
    ]
    if not matches:
        if required:
            raise CliError(
                f"No supervisor assignment exists for task {task_id}.",
                code="ASSIGNMENT_NOT_FOUND",
            )
        return None
    if len(matches) != 1:
        raise CliError(
            f"Task {task_id} has multiple active supervisor assignments.",
            code="AMBIGUOUS_ASSIGNMENT",
        )
    return AgentAssignment.from_mapping(matches[0])


def _normalized_provider(provider: str) -> str:
    normalized = provider.casefold().replace("_", "-")
    if normalized == "claude":
        return "claude-code"
    return normalized


def _validate_assignment_provider(
    assignment: AgentAssignment,
    requested_provider: str,
) -> None:
    assigned_provider = _normalized_provider(assignment.runtime_provider)
    if assigned_provider not in {"generic", requested_provider}:
        raise CliError(
            "The assignment is bound to "
            f"{assignment.runtime_provider}, not {requested_provider}.",
            code="ASSIGNMENT_PROVIDER_MISMATCH",
        )


_UNSAFE_PROVIDER_OPTIONS = {
    "-C",
    "-a",
    "-c",
    "-n",
    "-s",
    "--add-dir",
    "--allow-dangerously-skip-permissions",
    "--ask-for-approval",
    "--cd",
    "--config",
    "--cwd",
    "--name",
    "--no-alt-screen",
    "--sandbox",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-skip-permissions",
    "--permission-mode",
    "--yolo",
}


def _safe_provider_args(values: Sequence[str]) -> tuple[str, ...]:
    safe: list[str] = []
    for value in values:
        if "\x00" in value:
            raise CliError(
                "Provider arguments cannot contain NUL characters.",
                code="INVALID_PROVIDER_ARGUMENT",
            )
        option = value.split("=", 1)[0]
        unsafe_short_option = (
            len(option) > 2
            and any(
                option.startswith(prefix)
                for prefix in ("-C", "-a", "-c", "-n", "-s")
            )
        )
        if option in _UNSAFE_PROVIDER_OPTIONS or unsafe_short_option:
            raise CliError(
                f"Provider argument {option!r} would override writ.ai controls.",
                code="UNSAFE_PROVIDER_ARGUMENT",
            )
        safe.append(value)
    return tuple(safe)


def build_agent_prompt(
    assignment: AgentAssignment,
    *,
    workspace_id: str,
) -> str:
    provenance = (
        " -> ".join(assignment.provenance_path)
        if assignment.provenance_path
        else "No provenance path supplied."
    )
    redirect = assignment.redirect_instruction or "Follow the assigned plan exactly."
    interrupt = assignment.interrupt_reason or "No prior interruption."
    authorized_actions = "\n".join(
        f"- {action}" for action in assignment.authorized_actions
    )
    return "\n".join(
        (
            "You are a coding subagent controlled by the writ.ai supervisor.",
            f"Workspace: {workspace_id}",
            f"Task: {assignment.task_id}",
            f"Agent name: {assignment.agent_name}",
            f"Run: {assignment.run_id}",
            f"Plan: {assignment.plan_id}",
            f"Decision snapshot: {assignment.decision_snapshot}",
            "Authorized scopes: " + ", ".join(assignment.scopes),
            "Authorized actions:",
            authorized_actions,
            f"Execution mode: {assignment.execution_mode}",
            f"Previous interruption: {interrupt}",
            f"Current instruction: {redirect}",
            f"Authority provenance: {provenance}",
            "",
            "Work only on this task and only within the listed scopes.",
            "Treat the decision snapshot and plan as fixed inputs.",
            "If writ.ai interrupts this process, stop immediately.",
            "Do not infer, mint, or reuse authorization from this prompt.",
        )
    )


def build_agent_command(
    assignment: AgentAssignment,
    *,
    provider: str,
    workspace_id: str,
    cwd: Path,
    provider_args: Sequence[str],
) -> tuple[str, ...]:
    safe_args = _safe_provider_args(provider_args)
    prompt = build_agent_prompt(assignment, workspace_id=workspace_id)
    if provider == "codex":
        return (
            "codex",
            "-C",
            str(cwd),
            "--no-alt-screen",
            *safe_args,
            prompt,
        )
    return (
        "claude",
        "--name",
        assignment.run_id,
        *safe_args,
        prompt,
    )


class AgentController:
    def __init__(
        self,
        *,
        workspace_id: str,
        task_id: str,
        provider: str,
        cwd: Path,
        provider_args: Sequence[str],
        interrupt_timeout: float,
        process_runner: ProcessRunner,
    ) -> None:
        self.workspace_id = workspace_id
        self.task_id = task_id
        self.provider = provider
        self.cwd = cwd
        self.provider_args = tuple(provider_args)
        self.interrupt_timeout = interrupt_timeout
        self.process_runner = process_runner
        self.active_process: ManagedProcess | None = None
        self.active_assignment: AgentAssignment | None = None
        self.launched_run_ids: set[str] = set()
        self.last_interrupted_run_id: str | None = None
        self.waiting_run_id: str | None = None
        self.waiting_requires_resume = False
        self.terminal = False

    @staticmethod
    def _label(assignment: AgentAssignment) -> str:
        if assignment.execution_mode.casefold() == "simulated":
            return "[SIMULATED SUPERVISOR DATA]"
        return "[WRITAI SUPERVISOR]"

    def command_for(self, assignment: AgentAssignment) -> tuple[str, ...]:
        _validate_assignment_provider(assignment, self.provider)
        return build_agent_command(
            assignment,
            provider=self.provider,
            workspace_id=self.workspace_id,
            cwd=self.cwd,
            provider_args=self.provider_args,
        )

    def launch_initial(self, workspace: Mapping[str, Any]) -> AgentAssignment:
        assignment = _assignment_for_task(workspace, self.task_id, required=True)
        assert assignment is not None
        command = self.command_for(assignment)
        print(
            f"{self._label(assignment)} task={assignment.task_id} "
            f"run={assignment.run_id} state={assignment.state}"
        )
        state = assignment.state.casefold()
        supervisor = workspace.get("supervisor")
        supervisor_state = (
            str(supervisor.get("state")).casefold()
            if isinstance(supervisor, Mapping)
            else ""
        )
        if supervisor_state == "completed":
            self.terminal = True
            print("Supervisor is completed; no provider process was started.")
            return assignment
        if state == "interrupted":
            self.last_interrupted_run_id = assignment.run_id
            print("Assignment is interrupted; no provider process was started.")
            return assignment
        if state in {"queued", "redirected"}:
            self.waiting_run_id = assignment.run_id
            self.waiting_requires_resume = state == "redirected"
            print(
                f"Assignment is {state}; waiting for a supervisor execution ping."
            )
            return assignment
        if state not in {"running", "continuing", "resumed"}:
            print(
                f"Assignment state {assignment.state!r} is not runnable; "
                "no provider process was started."
            )
            return assignment
        self._launch(assignment, command)
        return assignment

    def preview_initial(self, workspace: Mapping[str, Any]) -> AgentAssignment:
        assignment = _assignment_for_task(workspace, self.task_id, required=True)
        assert assignment is not None
        command = self.command_for(assignment)
        print(
            f"{self._label(assignment)} task={assignment.task_id} "
            f"run={assignment.run_id} state={assignment.state}"
        )
        print("DRY RUN — provider process was not started.")
        print("Command: " + shlex.join((*command[:-1], "<writ.ai assignment prompt>")))
        print("Prompt:\n" + command[-1])
        return assignment

    def _launch(
        self,
        assignment: AgentAssignment,
        command: Sequence[str] | None = None,
    ) -> None:
        selected_command = tuple(command or self.command_for(assignment))
        self.active_process = self.process_runner.start(
            selected_command,
            cwd=self.cwd,
        )
        self.active_assignment = assignment
        self.launched_run_ids.add(assignment.run_id)
        print(
            f"{self._label(assignment)} launched {self.provider} "
            f"for run {assignment.run_id}."
        )

    def _stop_active(self, *, reason: str) -> bool:
        process = self.active_process
        assignment = self.active_assignment
        if process is None or assignment is None:
            return True
        if not process.is_running():
            self.active_process = None
            self.active_assignment = None
            return True

        label = self._label(assignment)
        process.interrupt()
        print(f"{label} SIGINT sent for run {assignment.run_id}: {reason}")
        if process.wait(self.interrupt_timeout):
            self.active_process = None
            self.active_assignment = None
            return True

        process.terminate()
        print(
            f"{label} run {assignment.run_id} did not stop in time; "
            "terminate sent."
        )
        if process.wait(self.interrupt_timeout):
            self.active_process = None
            self.active_assignment = None
            return True

        process.kill()
        print(
            f"{label} run {assignment.run_id} ignored terminate; kill sent."
        )
        if process.wait(self.interrupt_timeout):
            self.active_process = None
            self.active_assignment = None
            return True

        print(
            f"{label} run {assignment.run_id} is still alive; "
            "replacement launch remains blocked."
        )
        return False

    def shutdown(self, *, reason: str) -> None:
        self._stop_active(reason=reason)

    def handle_workspace(self, workspace: Mapping[str, Any]) -> None:
        supervisor = workspace.get("supervisor")
        supervisor_state = (
            str(supervisor.get("state")).casefold()
            if isinstance(supervisor, Mapping)
            else ""
        )
        assignment = _assignment_for_task(workspace, self.task_id, required=False)
        if supervisor_state == "completed":
            self._stop_active(reason="the supervisor completed")
            self.terminal = True
            return
        if assignment is None:
            return
        _validate_assignment_provider(assignment, self.provider)
        state = assignment.state.casefold()
        active = self.active_assignment

        if state == "completed":
            self._stop_active(reason="the assignment completed")
            self.terminal = True
            return

        if (
            active is not None
            and state in {"redirected", "resumed"}
            and assignment.run_id != active.run_id
            and assignment.redirected_from_run_id == active.run_id
        ):
            previous_run_id = active.run_id
            self.last_interrupted_run_id = previous_run_id
            if not self._stop_active(
                reason=(
                    assignment.interrupt_reason
                    or "a replacement supervisor run superseded this run"
                )
            ):
                return
            if state == "redirected":
                self.waiting_run_id = assignment.run_id
                self.waiting_requires_resume = True
                return
            if assignment.run_id not in self.launched_run_ids:
                self._launch(assignment)
            return

        if (
            active is None
            and not self.launched_run_ids
            and self.waiting_run_id == assignment.run_id
            and state in {"running", "continuing", "resumed"}
            and (not self.waiting_requires_resume or state == "resumed")
        ):
            self.waiting_run_id = None
            self.waiting_requires_resume = False
            self._launch(assignment)
            return

        if state == "interrupted":
            if active is None or assignment.run_id != active.run_id:
                return
            self.last_interrupted_run_id = assignment.run_id
            self._stop_active(
                reason=assignment.interrupt_reason or "assignment interrupted"
            )
            return

        if state != "resumed" or assignment.run_id in self.launched_run_ids:
            return
        if self.active_assignment is not None:
            return
        if (
            self.last_interrupted_run_id is None
            or assignment.redirected_from_run_id != self.last_interrupted_run_id
        ):
            return
        self._launch(assignment)

    def handle_envelope(self, envelope: JsonObject) -> None:
        workspace = _workspace_from_event(envelope)
        if workspace is not None:
            self.handle_workspace(workspace)


def _response_payload(response: httpx.Response) -> JsonObject:
    if response.status_code == 204 or not response.content:
        return {}
    try:
        raw = response.json()
    except ValueError as exc:
        raise CliError(
            "The writ.ai agent service returned a non-JSON response.",
            code="INVALID_RESPONSE",
            status_code=response.status_code,
        ) from exc
    if not isinstance(raw, dict):
        raise CliError(
            "The writ.ai agent service returned an invalid response shape.",
            code="INVALID_RESPONSE",
            status_code=response.status_code,
        )
    return {str(key): value for key, value in raw.items()}


def _path_segment(value: str | None) -> str:
    return quote(value, safe="") if value is not None else ""


def _load_document(filename: str) -> JsonObject:
    if filename == "-":
        raw = sys.stdin.read()
        source = "standard input"
        suffix = ""
    else:
        path = Path(filename)
        source = str(path)
        suffix = path.suffix.casefold()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CliError(
                f"Could not read {source}: {exc.strerror or exc}",
                code="INPUT_ERROR",
            ) from exc

    try:
        parsed: object
        if suffix == ".json":
            parsed = json.loads(raw)
        else:
            parsed = yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise CliError(
            f"Could not parse {source} as YAML or JSON: {exc}",
            code="INPUT_ERROR",
        ) from exc
    if not isinstance(parsed, dict):
        raise CliError(
            f"{source} must contain one object at its top level.",
            code="INPUT_ERROR",
        )
    return {str(key): value for key, value in parsed.items()}


def _redact(value: Any, *, key: str | None = None) -> Any:
    normalized_key = key.casefold() if key else ""
    if (
        normalized_key in _TOKEN_KEYS
        or "token" in normalized_key
        or (normalized_key == "signed_grant" and isinstance(value, str))
    ):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _first_value(value: Any, names: set[str]) -> Any:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key) in names:
                return child
        for child in value.values():
            result = _first_value(child, names)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = _first_value(child, names)
            if result is not None:
                return result
    return None


def _verification_outcome(
    payload: JsonObject,
    *,
    grant: str,
) -> tuple[bool, str, str]:
    expected_key = f"{grant}_verification"
    expected = payload.get(expected_key)
    verification: Any = expected if isinstance(expected, Mapping) else payload
    raw_code = _first_value(
        verification,
        {"verification_code", "code"},
    )
    raw_applied = _first_value(verification, {"applied"})
    code = str(raw_code or "UNKNOWN")
    reason = str(
        _first_value(verification, {"reason", "message"})
        or "The service did not provide a verification reason."
    )
    return code == "VALID" and raw_applied is True, code, reason


def _error_payload(error: CliError) -> JsonObject:
    body: JsonObject = {
        "error": {
            "code": error.code,
            "message": error.message,
        }
    }
    if error.status_code is not None:
        body["error"]["status_code"] = error.status_code
    if error.payload:
        correlation_id = error.payload.get("correlation_id")
        if correlation_id:
            body["correlation_id"] = correlation_id
    return body


def _string(value: Any, default: str = "—") -> str:
    return str(value) if value not in (None, "") else default


def _print_human(command: str, payload: JsonObject) -> None:
    safe = _redact(payload)
    assert isinstance(safe, dict)

    if command == "replay-crustdata":
        print("CrustData person observation replay")
        print(f"Source: {_string(safe.get('source_label'))}")
        print(f"Duplicate: {_string(safe.get('duplicate'))}")
        raw_flags = safe.get("flags", [])
        flags = raw_flags if isinstance(raw_flags, list) else []
        print(f"New review flags: {len(flags)}")
        for item in flags:
            if not isinstance(item, Mapping):
                continue
            print(
                f"- {_string(item.get('person_name'))} "
                f"({_string(item.get('person_id'))}): "
                f"{_string(item.get('change_kind'))}"
            )
            print(
                f"  Decision: {_string(item.get('workspace_id'))}/"
                f"{_string(item.get('decision_id'))}"
            )
            print(
                f"  Approval: {_string(item.get('approval_evidence_ref'))} "
                f"at {_string(item.get('approved_at'))}"
            )
            print(f"  Why: {_string(item.get('explanation'))}")
        if safe.get("duplicate") is True:
            raw_existing = safe.get("existing_flag_ids", [])
            existing = raw_existing if isinstance(raw_existing, list) else []
            print(f"Existing review flags retained: {len(existing)}")
            for flag_id in existing:
                print(f"- {_string(flag_id)}")
        return

    if command == "list":
        raw_workspaces = safe.get("workspaces", safe.get("items", []))
        workspaces = raw_workspaces if isinstance(raw_workspaces, list) else []
        if not workspaces:
            print("No live workspaces found.")
            return
        print("WORKSPACE\tSTATUS\tGRAPH\tNAME")
        for item in workspaces:
            if not isinstance(item, Mapping):
                continue
            print(
                "\t".join(
                    (
                        _string(item.get("id")),
                        _string(item.get("status")),
                        _string(item.get("graph_version")),
                        _string(item.get("name")),
                    )
                )
            )
        return

    headings = {
        "import": "Workspace imported",
        "show": "Live workspace",
        "approve-baseline": "Baseline approval",
        "authorize": "Plan authorization",
        "propose-change": "Decision change proposed",
        "approve-change": "Decision change approval",
        "cancel-change": "Pending decision change canceled",
        "update-plan": "Plan updated",
    }
    print(headings.get(command, "writ.ai response"))
    fields: tuple[tuple[str, set[str]], ...] = (
        ("Workspace", {"workspace_id", "id"}),
        ("Name", {"name"}),
        ("Status", {"status"}),
        ("Graph", {"graph_version", "decision_snapshot"}),
        (
            "Decision",
            {"decision_id", "changed_decision_id", "pending_decision_id"},
        ),
        ("Plan", {"plan_id"}),
        ("Verdict", {"verdict"}),
        ("Authorization", {"authorization_id"}),
        ("Expires", {"expires_at"}),
        ("Reason", {"reason"}),
    )
    pending_mutation = safe.get("pending_mutation")
    pending_decision = (
        pending_mutation.get("decision")
        if isinstance(pending_mutation, Mapping)
        else None
    )
    current_plan = safe.get("current_plan")
    authorization_key = {
        "authorize": "initial_authorization",
        "approve-change": "conflict_authorization",
        "update-plan": "replacement_authorization",
    }.get(command)
    if command == "show":
        authorization_key = next(
            (
                candidate
                for candidate in (
                    "replacement_authorization",
                    "conflict_authorization",
                    "initial_authorization",
                )
                if isinstance(safe.get(candidate), Mapping)
            ),
            None,
        )
    authorization = safe.get(authorization_key) if authorization_key else None
    authorization_fields = {
        "Verdict": {"verdict"},
        "Authorization": {"authorization_id"},
        "Expires": {"expires_at"},
        "Reason": {"reason"},
    }
    preferred_values = {
        "Decision": (
            pending_decision.get("id")
            if isinstance(pending_decision, Mapping)
            else None
        ),
        "Plan": (
            current_plan.get("id") if isinstance(current_plan, Mapping) else None
        ),
    }
    shown: set[str] = set()
    for label, names in fields:
        if label in authorization_fields:
            if not isinstance(authorization, Mapping):
                continue
            value = _first_value(authorization, authorization_fields[label])
        else:
            value = preferred_values.get(label) or _first_value(safe, names)
        marker = json.dumps(value, sort_keys=True, default=str) if value is not None else ""
        if value is not None and marker not in shown:
            print(f"{label}: {_string(value)}")
            shown.add(marker)


def _print_verification(payload: JsonObject, *, accepted: bool, code: str, reason: str) -> None:
    heading = "VALID — execution applied" if accepted else f"REJECTED — {code}"
    print(heading)
    print(f"Reason: {reason}")
    graph_version = _first_value(payload, {"graph_version", "decision_snapshot"})
    if graph_version is not None:
        print(f"Graph: {graph_version}")
    path = _first_value(payload, {"invalidation_path", "provenance_path"})
    if isinstance(path, list) and path:
        print("Path: " + " → ".join(str(node) for node in path))


def _add_runtime_options(
    parser: argparse.ArgumentParser,
    *,
    defaults: bool,
) -> None:
    default_url: str | object
    default_timeout: float | object
    default_json: bool | object
    if defaults:
        default_url = os.getenv("WRITAI_AGENT_URL", DEFAULT_AGENT_URL)
        default_timeout = DEFAULT_TIMEOUT_SECONDS
        default_json = False
    else:
        default_url = argparse.SUPPRESS
        default_timeout = argparse.SUPPRESS
        default_json = argparse.SUPPRESS
    parser.add_argument(
        "--agent-url",
        default=default_url,
        help=(
            "Agent service base URL "
            f"(default: WRITAI_AGENT_URL or {DEFAULT_AGENT_URL})"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=default_timeout,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=default_json,
        help="Emit redacted machine-readable JSON.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = WritaiArgumentParser(
        prog="writai",
        description=(
            "Import live workspaces and enforce snapshot-bound writ.ai authorizations."
        ),
    )
    _add_runtime_options(parser, defaults=True)
    groups = parser.add_subparsers(dest="group", required=True)

    doctor = groups.add_parser(
        "doctor",
        help="Check every sponsor integration: configured, credential valid, what is lost.",
    )
    doctor.add_argument(
        "integrations",
        nargs="*",
        help="Limit to these integrations (default: all).",
    )

    workspace = groups.add_parser(
        "workspace",
        help="Manage a user-owned live workspace.",
    )
    _add_runtime_options(workspace, defaults=False)
    commands = workspace.add_subparsers(dest="command", required=True)

    def command_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        child = commands.add_parser(name, help=help_text)
        _add_runtime_options(child, defaults=False)
        return child

    import_parser = command_parser("import", "Import a YAML or JSON workspace.")
    import_parser.add_argument("file", help="Workspace file, or - for standard input.")

    command_parser("list", "List live workspaces.")

    show = command_parser("show", "Show a live workspace.")
    show.add_argument("workspace_id")

    approve_baseline = command_parser(
        "approve-baseline",
        "Deprecated; use the authenticated Workspace approval UI.",
    )
    approve_baseline.add_argument("workspace_id")
    approve_baseline.add_argument(
        "--role",
        help="Deprecated and ignored; approval now requires verified user identity.",
    )

    authorize = command_parser(
        "authorize",
        "Authorize the current plan against the active graph snapshot.",
    )
    authorize.add_argument("workspace_id")

    propose_change = command_parser(
        "propose-change",
        "Propose an upstream decision change from YAML or JSON.",
    )
    propose_change.add_argument("workspace_id")
    propose_change.add_argument("file", help="Decision mutation file, or - for standard input.")

    approve_change = command_parser(
        "approve-change",
        "Deprecated; use `writai approve change`.",
    )
    approve_change.add_argument("workspace_id")
    approve_change.add_argument("decision_id")
    approve_change.add_argument(
        "--role",
        help="Deprecated and ignored; approval now requires verified user identity.",
    )

    cancel_change = command_parser(
        "cancel-change",
        "Cancel the pending decision proposal without changing the graph.",
    )
    cancel_change.add_argument("workspace_id")

    verify = command_parser(
        "verify",
        "Ask the executor to verify and apply a stored authorization.",
    )
    verify.add_argument("workspace_id")
    verify.add_argument(
        "--grant",
        choices=("initial", "replacement"),
        default="initial",
        help="Stored grant to verify (default: initial).",
    )

    update_plan = command_parser(
        "update-plan",
        "Replace the current agent plan from YAML or JSON.",
    )
    update_plan.add_argument("workspace_id")
    update_plan.add_argument("file", help="Plan file, or - for standard input.")

    replay_crustdata = command_parser(
        "replay-crustdata",
        "Replay a labelled CrustData person watcher fixture for human review.",
    )
    replay_crustdata.add_argument(
        "file",
        help="Labelled replay fixture in YAML or JSON, or - for standard input.",
    )
    replay_crustdata.add_argument(
        "--bearer",
        default=os.getenv("CRUSTDATA_REPLAY_BEARER"),
        help="Operator replay bearer (default: CRUSTDATA_REPLAY_BEARER).",
    )

    agent = groups.add_parser(
        "agent",
        help="Run a provider CLI under writ.ai supervisor control.",
    )
    _add_runtime_options(agent, defaults=False)
    agent_commands = agent.add_subparsers(dest="command", required=True)
    agent_run = agent_commands.add_parser(
        "run",
        help="Launch and redirect one supervised Codex or Claude Code assignment.",
    )
    _add_runtime_options(agent_run, defaults=False)
    agent_run.add_argument("workspace_id", nargs="?")
    agent_run.add_argument(
        "--workspace",
        dest="workspace_option",
        help="Workspace ID (preferred alias for the positional argument).",
    )
    agent_run.add_argument("--task", required=True, help="Assigned task ID.")
    agent_run.add_argument(
        "--provider",
        required=True,
        choices=("codex", "claude-code"),
        help="Local coding-agent CLI to supervise.",
    )
    agent_run.add_argument(
        "--cwd",
        default=".",
        help="Working directory for the provider process (default: current directory).",
    )
    agent_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the assignment without launching a process.",
    )
    agent_run.add_argument(
        "--interrupt-timeout",
        type=float,
        default=5.0,
        help="Seconds to wait after SIGINT before terminate (default: 5).",
    )

    # Frozen seam: each lane owns its own module, so neither edits this file again.
    cli_dev.register(groups, _add_runtime_options)
    cli_approve.register(groups, _add_runtime_options)

    return parser


def _parse_cli_args(argv: Sequence[str] | None) -> argparse.Namespace:
    values = list(sys.argv[1:] if argv is None else argv)
    provider_args: list[str] = []
    if "--" in values:
        separator = values.index("--")
        command_values = values[:separator]
        agent_index = next(
            (
                index
                for index, value in enumerate(command_values)
                if value == "agent"
            ),
            None,
        )
        if (
            agent_index is not None
            and "run" in command_values[agent_index + 1 :]
        ):
            provider_args = values[separator + 1 :]
            values = command_values
    args = build_parser().parse_args(values)
    if args.group == "agent":
        args.provider_args = provider_args
    return args


def _request_for_command(
    client: WritaiClient,
    args: argparse.Namespace,
) -> JsonObject:
    command = str(args.command)
    if command in {"approve-baseline", "approve-change"}:
        replacement = (
            "the authenticated Workspace approval UI"
            if command == "approve-baseline"
            else "`writai approve change`"
        )
        raise CliError(
            f"The legacy `writai workspace {command}` command is disabled; "
            f"use {replacement} so the approver's permission is verified.",
            code="COMMAND_DEPRECATED",
        )
    route_key = f"verify-{args.grant}" if command == "verify" else command
    route = ROUTES[route_key]
    workspace_id = getattr(args, "workspace_id", None)
    decision_id = getattr(args, "decision_id", None)
    body: JsonObject | None = None

    if command == "import":
        body = _load_document(args.file)
    elif command == "propose-change":
        body = _load_document(args.file)
    elif command == "verify":
        body = {}
    elif command == "update-plan":
        body = {"plan": _load_document(args.file)}
    elif command == "replay-crustdata":
        body = _load_document(args.file)
    elif command == "authorize":
        body = {}

    headers = None
    if command == "replay-crustdata" and args.bearer:
        headers = {"Authorization": f"Bearer {args.bearer}"}
    payload = client.request(
        route,
        workspace_id=workspace_id,
        decision_id=decision_id,
        body=body,
        headers=headers,
    )
    if command == "update-plan":
        return client.request(
            ROUTES["reauthorize"],
            workspace_id=workspace_id,
            body={},
        )
    return payload


def _run_agent_command(
    *,
    client: WritaiClient,
    args: argparse.Namespace,
    process_runner: ProcessRunner,
) -> int:
    if (
        not math.isfinite(args.interrupt_timeout)
        or args.interrupt_timeout <= 0
    ):
        raise CliError(
            "The interrupt timeout must be greater than zero.",
            code="INVALID_INTERRUPT_TIMEOUT",
        )
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise CliError(
            f"The provider working directory does not exist: {cwd}",
            code="INVALID_WORKING_DIRECTORY",
        )
    provider_args = tuple(args.provider_args)
    if provider_args and provider_args[0] == "--":
        provider_args = provider_args[1:]
    positional_workspace = args.workspace_id
    option_workspace = args.workspace_option
    if (
        positional_workspace
        and option_workspace
        and positional_workspace != option_workspace
    ):
        raise CliError(
            "The positional workspace and --workspace must match.",
            code="INVALID_WORKSPACE",
        )
    workspace_id = str(option_workspace or positional_workspace or "")
    if not workspace_id:
        raise CliError(
            "A workspace ID is required (use --workspace WORKSPACE_ID).",
            code="INVALID_WORKSPACE",
        )
    controller = AgentController(
        workspace_id=workspace_id,
        task_id=str(args.task),
        provider=str(args.provider),
        cwd=cwd,
        provider_args=provider_args,
        interrupt_timeout=float(args.interrupt_timeout),
        process_runner=process_runner,
    )
    if args.dry_run:
        workspace = client.request(
            ROUTES["show"],
            workspace_id=workspace_id,
        )
        safe_workspace = _redact(workspace)
        assert isinstance(safe_workspace, Mapping)
        controller.preview_initial(safe_workspace)
        return 0

    try:
        launched_from_snapshot = False
        for envelope in client.workspace_events(workspace_id):
            event_workspace = _workspace_from_event(envelope)
            if event_workspace is None:
                raise CliError(
                    "The workspace event stream omitted its workspace data.",
                    code="INVALID_EVENT_STREAM",
                )
            if not launched_from_snapshot:
                if envelope.get("event") != "live-workspace.supervisor.snapshot":
                    raise CliError(
                        "The workspace event stream did not start with a snapshot.",
                        code="INVALID_EVENT_STREAM",
                    )
                initial_assignment = controller.launch_initial(event_workspace)
                launched_from_snapshot = True
                if (
                    controller.terminal
                    or initial_assignment.state.casefold() == "completed"
                ):
                    return 0
                continue
            controller.handle_workspace(event_workspace)
            if controller.terminal:
                return 0
        message = (
            "The workspace event stream ended before its initial snapshot."
            if not launched_from_snapshot
            else "The workspace event stream closed before the assignment completed."
        )
        raise CliError(message, code="EVENT_STREAM_CLOSED")
    finally:
        controller.shutdown(reason="the writ.ai wrapper stopped")


def run(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    process_runner: ProcessRunner | None = None,
) -> int:
    args = _parse_cli_args(argv)
    json_output = bool(args.json)
    if args.group == "doctor":
        # Deliberately outside the WritaiClient block: doctor probes the SPONSOR
        # services, not ours, so it has to work on a machine where nothing local
        # is running. That is exactly when someone runs it.
        from writai import doctor as doctor_module

        return doctor_module.main(
            [*getattr(args, "integrations", []), *(["--json"] if json_output else [])]
        )
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise CliError("The HTTP timeout must be greater than zero.", code="INVALID_TIMEOUT")
        with WritaiClient(
            agent_url=str(args.agent_url),
            timeout_seconds=float(args.timeout),
            transport=transport,
        ) as client:
            if args.group == "agent":
                return _run_agent_command(
                    client=client,
                    args=args,
                    process_runner=process_runner or SubprocessRunner(),
                )
            if args.group == cli_dev.GROUP_NAME:
                return cli_dev.run(client=client, args=args)
            if args.group == cli_approve.GROUP_NAME:
                return cli_approve.run(client=client, args=args)
            payload = _request_for_command(client, args)
    except KeyboardInterrupt:
        print("writai: interrupted; supervised process stopped.", file=sys.stderr)
        return 130
    except CliError as error:
        if (
            args.command == "verify"
            and error.code in _DETERMINISTIC_VERIFICATION_CODES
        ):
            payload = error.payload or {"code": error.code, "reason": error.message}
            if json_output:
                print(json.dumps(_redact(payload), indent=2, sort_keys=True))
            else:
                _print_verification(
                    payload,
                    accepted=False,
                    code=error.code,
                    reason=error.message,
                )
            return 1
        safe_error = _error_payload(error)
        if json_output:
            print(json.dumps(safe_error, indent=2, sort_keys=True), file=sys.stderr)
        else:
            print(f"writai: {error.message} [{error.code}]", file=sys.stderr)
        return 2

    if args.command == "verify":
        accepted, code, reason = _verification_outcome(payload, grant=str(args.grant))
        if json_output:
            print(json.dumps(_redact(payload), indent=2, sort_keys=True))
        else:
            _print_verification(payload, accepted=accepted, code=code, reason=reason)
        return 0 if accepted else 1

    if json_output:
        print(json.dumps(_redact(payload), indent=2, sort_keys=True))
    else:
        _print_human(str(args.command), payload)
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
