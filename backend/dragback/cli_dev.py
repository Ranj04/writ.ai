"""Developer-side Dragback CLI group (Lane A).

`cli.py` registers this module's parser and dispatches to `run()`. It is frozen
after the seam commit, so every later change to the `dev` command surface happens
here. This module must not import `dragback.cli` at module scope: `cli.py`
imports it, and a top-level back-import would be a cycle.

The group is deliberately read-mostly. It binds a working directory to one
assignment, shows what the supervisor believes about every registered session —
including the ones it could not bind — and explains an interrupt as a path
rather than a badge. No command here decides anything: the service does.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from dragback.terminal import (
    BLOCK,
    CONTINUING,
    INDENT,
    STOPPED,
    UNBOUND,
    Style,
    arrow_path,
    field,
    supports_colour,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime cycle
    from dragback.cli import DragbackClient

GROUP_NAME = "dev"

AddRuntimeOptions = Callable[..., None]

JsonObject = dict[str, Any]

# The sessions collection lives on the agent service next to the hook endpoints.
SESSIONS_PATH = "/supervisor/sessions"
HOOK_API_KEY_HEADER = "X-Dragback-Hook-API-Key"
ENV_HOOK_API_KEY = "DRAGBACK_HOOK_API_KEY"
ATTACH_DIRECTORY = ".dragback"
ATTACH_FILENAME = "attach"

_MISSING = "—"
_UNBOUND = "unbound"
# Keys the service may use to name the decision a session must acknowledge. The
# id is never invented locally: acknowledging the wrong decision would silently
# unblock a run the authority layer still refuses.
_DECISION_ID_KEYS = (
    "decision_id",
    "pending_decision_id",
    "interrupt_decision_id",
    "unacknowledged_decision_id",
)


def register(
    subparsers: Any,
    add_runtime_options: AddRuntimeOptions,
) -> argparse.ArgumentParser:
    """Register `dragback dev ...`. Called once from `cli.build_parser`."""

    group = subparsers.add_parser(
        GROUP_NAME,
        help="Bind, inspect, and explain this developer's supervised sessions.",
    )
    add_runtime_options(group, defaults=False)
    commands = group.add_subparsers(dest="command", required=True)

    def command_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        child = commands.add_parser(name, help=help_text)
        add_runtime_options(child, defaults=False)
        return child

    attach = command_parser(
        "attach",
        "Bind this working directory to one supervisor assignment.",
    )
    attach.add_argument("assignment_id")
    attach.add_argument("--workspace", required=True, help="Workspace ID.")

    status = command_parser(
        "status",
        "Show registered sessions and their bindings, including unbound ones.",
    )
    status.add_argument("--workspace", help="Limit to one workspace.")

    why = command_parser(
        "why",
        "Explain an interrupt: provenance path, affected scopes, and evidence.",
    )
    why.add_argument("session_id", nargs="?")
    why.add_argument("--workspace", help="Workspace ID.")

    ack = command_parser(
        "ack",
        "Acknowledge an outright-invalidated assignment so its session stops being denied.",
    )
    ack.add_argument("session_id")

    watch = command_parser(
        "watch",
        "Stream supervisor interrupts and redirects for a workspace.",
    )
    watch.add_argument("workspace_id")

    return group


def run(*, client: DragbackClient, args: argparse.Namespace) -> int:
    """Execute one `dragback dev` command."""

    from dragback.cli import CliError

    command = str(args.command)
    json_output = bool(getattr(args, "json", False))

    if command == "attach":
        return _run_attach(args=args, json_output=json_output)
    if command == "status":
        return _run_status(client=client, args=args, json_output=json_output)
    if command == "why":
        return _run_why(client=client, args=args, json_output=json_output)
    if command == "ack":
        return _run_ack(client=client, args=args, json_output=json_output)
    if command == "watch":
        return _run_watch(client=client, args=args, json_output=json_output)
    raise CliError(f"Unknown dev command {command!r}.", code="UNKNOWN_COMMAND")


# --------------------------------------------------------------------------- #
# attach
# --------------------------------------------------------------------------- #


def _run_attach(*, args: argparse.Namespace, json_output: bool) -> int:
    """Write `.dragback/attach`. Local only — the service resolves the binding."""

    from dragback.cli import CliError

    assignment_id = _identifier(
        getattr(args, "assignment_id", None),
        label="assignment ID",
        code="INVALID_ASSIGNMENT_ID",
    )
    workspace_id = _identifier(
        getattr(args, "workspace", None),
        label="workspace ID",
        code="INVALID_WORKSPACE",
    )

    directory = Path.cwd() / ATTACH_DIRECTORY
    path = directory / ATTACH_FILENAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Keep the assignment-only format for stage directories that already
        # contain this marker. The service binds only a globally unique match;
        # --workspace remains an operator-visible cross-check in the CLI output.
        # The trailing newline keeps the file readable with cat and editable by
        # hand; the service strips it before matching.
        path.write_text(f"{assignment_id}\n", encoding="utf-8")
    except OSError as exc:
        raise CliError(
            f"Could not write {path}: {exc.strerror or exc}",
            code="ATTACH_WRITE_FAILED",
        ) from exc

    if json_output:
        _emit_json(
            {
                "attach_path": str(path),
                "assignment_id": assignment_id,
                "workspace_id": workspace_id,
            }
        )
        return 0
    print(str(path))
    print(f"Assignment: {assignment_id}")
    print(f"Workspace: {workspace_id}")
    print(
        "The next Claude Code session started here binds explicitly to that "
        "assignment."
    )
    return 0


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def _run_status(
    *,
    client: DragbackClient,
    args: argparse.Namespace,
    json_output: bool,
) -> int:
    payload, items = _fetch_sessions(client)
    workspace_id = _optional_text(getattr(args, "workspace", None))
    selected = [item for item in items if _in_workspace(item, workspace_id)]

    if json_output:
        _emit_json({"sessions": selected, "correlation_id": payload.get("correlation_id")})
        return 0

    if not selected:
        print("No supervised sessions are registered.")
        return 0

    # docs/TERMINAL_OUTPUT_SPEC.md §4.
    workspaces = _WorkspaceCache(client)
    style = Style(supports_colour(sys.stdout))
    rows: list[tuple[str, str, str, str, bool]] = []
    unbound = 0
    for item in selected:
        binding = _binding_of(item)
        bound = _is_bound(binding)
        if not bound:
            unbound += 1
        state = _session_state(item, binding, workspaces)
        rows.append(
            (
                _text(binding.get("agent_name") or binding.get("session_id")),
                _text(_binding_field(binding, "task_id"), _UNBOUND if not bound else _MISSING),
                state,
                _text(binding.get("source"), _UNBOUND if not bound else _MISSING),
                bound,
            )
        )

    graph_version = workspaces.graph_version(workspace_id)
    heading = f"{len(selected)} session{'s' if len(selected) != 1 else ''}"
    if graph_version:
        heading = f"{heading} · {graph_version}"
    print()
    print(f"{INDENT}{heading}")
    print()

    name_width = max((len(row[0]) for row in rows), default=0)
    task_width = max((len(row[1]) for row in rows), default=0)
    for name, task, state, _source, bound in rows:
        if not bound:
            # A session Dragback could not bind is allowed everything. It is
            # listed, and marked, so the gap is visible rather than silent.
            leader = UNBOUND
            body = f"{name.ljust(name_width)}  {style.label(_UNBOUND)}"
        elif _is_stopped_state(state):
            leader = style.stopped(STOPPED)
            body = (
                f"{name.ljust(name_width)}  {task.ljust(task_width)}  "
                f"{style.stopped(state)}"
            )
        else:
            leader = style.preserved(CONTINUING)
            body = (
                f"{name.ljust(name_width)}  {task.ljust(task_width)}  "
                f"{style.preserved(state)}"
            )
        print(f"{INDENT}{INDENT}{leader}  {body}")
    if unbound:
        print()
        print(
            INDENT
            + style.label(
                f"{unbound} of {len(selected)} unbound: "
                "registered, visible, not enforced."
            )
        )
    print()
    return 0


# --------------------------------------------------------------------------- #
# why
# --------------------------------------------------------------------------- #


def _run_why(
    *,
    client: DragbackClient,
    args: argparse.Namespace,
    json_output: bool,
) -> int:
    from dragback.cli import CliError

    _, items = _fetch_sessions(client)
    requested = _optional_text(getattr(args, "session_id", None))
    workspace_option = _optional_text(getattr(args, "workspace", None))
    item = _select_session(items, requested, workspace_option)
    binding = _binding_of(item)

    if not _is_bound(binding):
        raise CliError(
            f"Session {_text(binding.get('session_id'))} is unbound, so no "
            "assignment explains it. Run `dragback dev attach` in its working "
            "directory.",
            code="SESSION_UNBOUND",
        )

    workspaces = _WorkspaceCache(client)
    assignment = _assignment_for_session(
        workspaces,
        item=item,
        binding=binding,
        workspace_option=workspace_option,
    )
    workspace_id = _binding_field(binding, "workspace_id") or workspace_option
    # The report carries the affected scopes and the evidence ref. Fetching it is
    # free once the assignment came from the workspace, and best effort when the
    # service inlined everything the explanation needs.
    report: JsonObject | None = None
    if workspace_id is not None and _evidence_ref(item, assignment, None) is None:
        workspace = workspaces.optional(workspace_id)
        report = _invalidation_report(workspace) if workspace is not None else None
    provenance = _string_list(assignment.get("provenance_path"))
    assignment_scopes = sorted(_string_list(assignment.get("scopes")))
    affected = sorted(_string_list(report.get("affected_scopes"))) if report else []
    reason = _optional_text(assignment.get("interrupt_reason"))
    instruction = _optional_text(assignment.get("redirect_instruction"))
    evidence = _evidence_ref(item, assignment, report)

    if json_output:
        _emit_json(
            {
                "session_id": binding.get("session_id"),
                "workspace_id": _binding_field(binding, "workspace_id"),
                "assignment_id": _binding_field(binding, "assignment_id"),
                "task_id": _binding_field(binding, "task_id") or assignment.get("task_id"),
                "source": binding.get("source"),
                "state": assignment.get("state"),
                "provenance_path": provenance,
                "affected_scopes": affected or assignment_scopes,
                "assignment_scopes": assignment_scopes,
                "interrupt_reason": reason,
                "redirect_instruction": instruction,
                "evidence_ref": evidence,
            }
        )
        return 0

    # docs/TERMINAL_OUTPUT_SPEC.md §3.
    style = Style(supports_colour(sys.stdout))
    scopes = affected or assignment_scopes
    task_id = _text(_binding_field(binding, "task_id") or assignment.get("task_id"))
    workspace = workspaces.workspace(
        _binding_field(binding, "workspace_id") or workspace_option
    )
    decision = _changed_decision(workspace)

    print()
    print(f"{INDENT}{style.stopped(BLOCK)}  Your agent changed direction.")
    print()

    if reason:
        print(field(style, "Because", f'"{reason}"'))
        attribution = _attribution(workspace)
        if attribution:
            print(field(style, "", attribution))
        print()

    # The scope is what made this selective rather than blanket, so it prints
    # whether or not the decision id happens to be readable from here.
    if decision or scopes:
        print(field(style, "Which became", decision or _MISSING))
        if scopes:
            print(field(style, "", f"scope: {', '.join(scopes)}"))
        if affected and assignment_scopes and assignment_scopes != affected:
            print(field(style, "", f"this task: {', '.join(assignment_scopes)}"))
        print()

    if provenance:
        print(field(style, "Which reached", arrow_path(provenance)))
        # The two-column ending is the product: what stopped, beside what did not.
        for line in _reached_rows(workspace, task_id, style):
            print(field(style, "", line))
        print()

    preserved = _preserved_titles(workspace)
    if preserved:
        print(field(style, "Preserved", style.preserved(", ".join(preserved))))
    if instruction:
        print(field(style, "Changed", style.stopped(instruction)))
    if evidence:
        print(field(style, "Evidence", evidence))
    print()
    return 0


# --------------------------------------------------------------------------- #
# ack
# --------------------------------------------------------------------------- #


def _run_ack(
    *,
    client: DragbackClient,
    args: argparse.Namespace,
    json_output: bool,
) -> int:
    from dragback.cli import CliError, Route

    session_id = _identifier(
        getattr(args, "session_id", None),
        label="session ID",
        code="INVALID_SESSION_ID",
    )
    _, items = _fetch_sessions(client)
    item = _select_session(items, session_id, None)
    decision_id = _decision_id(item)
    if decision_id is None:
        # The service names the decision only while it is actually denying this
        # session. Deriving one from the workspace instead would let a developer
        # acknowledge a change before it interrupts them — a bypass, not a
        # shortcut — so an absent id is an error rather than a guess.
        raise CliError(
            f"Session {session_id} is not blocked by an unacknowledged decision, "
            "so there is nothing to acknowledge.",
            code="DECISION_ID_UNRESOLVED",
        )

    # The service route is /acknowledge and takes no body; the decision it
    # releases is the one currently blocking this session, resolved server-side.
    payload = client.request(
        Route("POST", f"{SESSIONS_PATH}/{quote(session_id, safe='')}/acknowledge"),
        body={},
        headers=_hook_headers(),
    )

    if json_output:
        _emit_json(payload)
        return 0
    binding = _binding_of(payload)
    acknowledged = _string_list(binding.get("acknowledged_decision_ids"))
    print(f"Acknowledged {decision_id} for session {session_id}.")
    if acknowledged:
        print("Acknowledged decisions: " + ", ".join(acknowledged))
    return 0


# --------------------------------------------------------------------------- #
# watch
# --------------------------------------------------------------------------- #


def _run_watch(
    *,
    client: DragbackClient,
    args: argparse.Namespace,
    json_output: bool,
) -> int:
    from dragback.cli import _redact

    workspace_id = _identifier(
        getattr(args, "workspace_id", None),
        label="workspace ID",
        code="INVALID_WORKSPACE",
    )
    if not json_output:
        print(f"Watching supervisor transitions for {workspace_id}. Ctrl-C stops.")

    lifecycle: str | None = None
    assignment_states: dict[str, tuple[str, str]] = {}

    for envelope in client.workspace_events(workspace_id):
        safe = _redact(envelope)
        if not isinstance(safe, Mapping):
            continue
        workspace = safe.get("data")
        if not isinstance(workspace, Mapping):
            continue
        supervisor = workspace.get("supervisor")
        if not isinstance(supervisor, Mapping):
            continue
        event_name = _optional_text(safe.get("event"))

        supervisor_state = _optional_text(supervisor.get("state"))
        if supervisor_state is not None and supervisor_state != lifecycle:
            _print_transition(
                {
                    "kind": "supervisor",
                    "event": event_name,
                    "workspace_id": workspace_id,
                    "previous_state": lifecycle,
                    "state": supervisor_state,
                },
                json_output=json_output,
            )
            lifecycle = supervisor_state

        raw_assignments = supervisor.get("assignments")
        if not isinstance(raw_assignments, list):
            continue
        for raw in raw_assignments:
            if not isinstance(raw, Mapping):
                continue
            key = _optional_text(raw.get("id")) or _optional_text(raw.get("task_id"))
            state = _optional_text(raw.get("state"))
            if key is None or state is None:
                continue
            run_id = _optional_text(raw.get("run_id")) or ""
            previous = assignment_states.get(key)
            if previous == (state, run_id):
                continue
            assignment_states[key] = (state, run_id)
            _print_transition(
                {
                    "kind": "assignment",
                    "event": event_name,
                    "workspace_id": workspace_id,
                    "assignment_id": _optional_text(raw.get("id")),
                    "task_id": _optional_text(raw.get("task_id")),
                    "previous_state": previous[0] if previous is not None else None,
                    "state": state,
                    "run_id": run_id or None,
                    "interrupt_reason": _optional_text(raw.get("interrupt_reason")),
                    "redirect_instruction": _optional_text(
                        raw.get("redirect_instruction")
                    ),
                },
                json_output=json_output,
            )

    if not json_output:
        print("The workspace event stream closed.")
    return 0


def _print_transition(record: JsonObject, *, json_output: bool) -> None:
    if json_output:
        from dragback.cli import _redact

        print(json.dumps(_redact(record), sort_keys=True), flush=True)
        return

    previous = _text(record.get("previous_state"))
    state = _text(record.get("state"))
    if record.get("kind") == "supervisor":
        print(f"supervisor\t{previous} → {state}", flush=True)
        return
    label = _text(record.get("task_id") or record.get("assignment_id"))
    run_id = _optional_text(record.get("run_id"))
    suffix = f"\trun={run_id}" if run_id else ""
    print(f"{label}\t{previous} → {state}{suffix}", flush=True)
    reason = _optional_text(record.get("interrupt_reason"))
    instruction = _optional_text(record.get("redirect_instruction"))
    if reason:
        print(f"  reason: {reason}", flush=True)
    if instruction:
        print(f"  instruction: {instruction}", flush=True)


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #


def _emit_json(payload: JsonObject) -> None:
    from dragback.cli import _redact

    print(json.dumps(_redact(payload), indent=2, sort_keys=True, default=str))


def _identifier(value: Any, *, label: str, code: str) -> str:
    """Reject identifiers that would corrupt a file, a path, or a table row."""

    from dragback.cli import CliError

    text = str(value).strip() if value is not None else ""
    if not text:
        raise CliError(f"A {label} is required.", code=code)
    if len(text) > 200:
        raise CliError(f"The {label} is too long.", code=code)
    if any(character < " " or character == "\x7f" for character in text):
        raise CliError(
            f"The {label} cannot contain control characters.",
            code=code,
        )
    return text


def _hook_headers() -> dict[str, str]:
    """The session routes authenticate on the hook key and fail closed without it."""

    key = os.environ.get(ENV_HOOK_API_KEY, "").strip()
    return {HOOK_API_KEY_HEADER: key} if key else {}


def _fetch_sessions(client: DragbackClient) -> tuple[JsonObject, list[JsonObject]]:
    from dragback.cli import Route

    payload = client.request(Route("GET", SESSIONS_PATH), headers=_hook_headers())
    return payload, _session_items(payload)


def _session_items(payload: Mapping[str, Any]) -> list[JsonObject]:
    from dragback.cli import CliError

    for key in ("sessions", "bindings", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [
                {str(name): entry for name, entry in item.items()}
                for item in value
                if isinstance(item, Mapping)
            ]
    raise CliError(
        "The Dragback agent service did not return a session list.",
        code="INVALID_RESPONSE",
    )


def _binding_of(item: Mapping[str, Any]) -> JsonObject:
    """Accept either a bare `SessionBinding` or one wrapped in `{"binding": …}`."""

    binding = item.get("binding")
    if isinstance(binding, Mapping):
        return {str(key): value for key, value in binding.items()}
    return {str(key): value for key, value in item.items()}


def _assignment_of(binding: Mapping[str, Any]) -> Mapping[str, Any]:
    """`ClaudeCodeSessionBinding.assignment` is a nested AssignmentLocator."""

    nested = binding.get("assignment")
    return nested if isinstance(nested, Mapping) else {}


def _binding_field(binding: Mapping[str, Any], name: str) -> str | None:
    """Read a locator field from the nested assignment, or a flat fallback."""

    nested = _optional_text(_assignment_of(binding).get(name))
    return nested if nested is not None else _optional_text(binding.get(name))


def _is_bound(binding: Mapping[str, Any]) -> bool:
    if binding.get("is_bound") is True or binding.get("bound") is True:
        return True
    return _binding_field(binding, "assignment_id") is not None


def _in_workspace(item: Mapping[str, Any], workspace_id: str | None) -> bool:
    if workspace_id is None:
        return True
    binding = _binding_of(item)
    # An unbound session belongs to no workspace, and filtering it away would
    # hide exactly the gap this command exists to show.
    if not _is_bound(binding):
        return True
    return _binding_field(binding, "workspace_id") == workspace_id


def _session_state(
    item: Mapping[str, Any],
    binding: Mapping[str, Any],
    workspaces: _WorkspaceCache,
) -> str:
    """The assignment state behind a session, from wherever it is available.

    `GET /supervisor/sessions` returns bindings, and a binding has no state — the
    assignment does. The workspace read is best effort on purpose: the binding
    table is the point of this command, and an unreachable workspace should cost
    one column, not the whole answer.
    """

    for key in ("state", "assignment_state", "session_state"):
        value = _optional_text(item.get(key))
        if value is not None:
            return value
    # A binding's nested `assignment` is an AssignmentLocator — ids only, no
    # state. Only an entry that actually carries a state is the assignment
    # itself; otherwise the state has to come from the workspace.
    candidate = item.get("assignment")
    assignment: Mapping[str, Any] | None = (
        candidate
        if isinstance(candidate, Mapping) and "state" in candidate
        else None
    )
    if assignment is None:
        if not _is_bound(binding):
            return _UNBOUND
        workspace_id = _binding_field(binding, "workspace_id")
        workspace = workspaces.optional(workspace_id) if workspace_id else None
        if workspace is not None:
            assignment = _assignment_in_workspace(
                workspace,
                assignment_id=_optional_text(_binding_field(binding, "assignment_id")),
                task_id=_optional_text(_binding_field(binding, "task_id")),
            )
    if assignment is not None:
        value = _optional_text(assignment.get("state"))
        if value is not None:
            return value
    return _MISSING if _is_bound(binding) else _UNBOUND


def _select_session(
    items: list[JsonObject],
    session_id: str | None,
    workspace_id: str | None,
) -> JsonObject:
    from dragback.cli import CliError

    if session_id is not None:
        for item in items:
            if _optional_text(_binding_of(item).get("session_id")) == session_id:
                return item
        raise CliError(
            f"No registered session {session_id}. Run `dragback dev status`.",
            code="SESSION_NOT_FOUND",
        )

    candidates = [
        item
        for item in items
        if _is_bound(_binding_of(item)) and _in_workspace(item, workspace_id)
    ]
    if not candidates:
        raise CliError(
            "No bound session is registered, so there is nothing to explain.",
            code="SESSION_NOT_FOUND",
        )
    if len(candidates) > 1:
        raise CliError(
            "Several bound sessions are registered; name one explicitly.",
            code="AMBIGUOUS_SESSION",
        )
    return candidates[0]


class _WorkspaceCache:
    """One `GET /live-workspaces/{id}` per workspace, however often it is asked."""

    def __init__(self, client: DragbackClient) -> None:
        self._client = client
        self._workspaces: dict[str, JsonObject] = {}
        self._failed: set[str] = set()

    def get(self, workspace_id: str) -> JsonObject:
        from dragback.cli import ROUTES

        cached = self._workspaces.get(workspace_id)
        if cached is not None:
            return cached
        workspace = self._client.request(ROUTES["show"], workspace_id=workspace_id)
        self._workspaces[workspace_id] = workspace
        return workspace

    def optional(self, workspace_id: str) -> JsonObject | None:
        """Best effort: a workspace this CLI cannot read is not a fatal error."""

        from dragback.cli import CliError

        if workspace_id in self._failed:
            return None
        try:
            return self.get(workspace_id)
        except CliError:
            self._failed.add(workspace_id)
            return None

    def workspace(self, workspace_id: str | None) -> JsonObject | None:
        if not workspace_id:
            return None
        return self.optional(workspace_id)

    def graph_version(self, workspace_id: str | None) -> str:
        """The snapshot every listed session is being judged against.

        Only reported when it is unambiguous: with no workspace selected, several
        workspaces could be on different snapshots, and printing one of them in
        the heading would be a claim about all of them.
        """

        workspace = self.workspace(workspace_id)
        if workspace is None:
            if len(self._workspaces) == 1:
                workspace = next(iter(self._workspaces.values()))
            else:
                return ""
        return _optional_text(workspace.get("graph_version")) or ""


def _workspace_id_for(
    binding: Mapping[str, Any],
    workspace_option: str | None,
) -> str:
    from dragback.cli import CliError

    workspace_id = _binding_field(binding, "workspace_id") or workspace_option
    if workspace_id is None:
        raise CliError(
            "The session does not name a workspace; pass --workspace.",
            code="INVALID_WORKSPACE",
        )
    return workspace_id


def _assignment_in_workspace(
    workspace: Mapping[str, Any],
    *,
    assignment_id: str | None,
    task_id: str | None,
) -> JsonObject | None:
    supervisor = workspace.get("supervisor")
    assignments = (
        supervisor.get("assignments") if isinstance(supervisor, Mapping) else None
    )
    if not isinstance(assignments, list):
        return None
    for raw in assignments:
        if not isinstance(raw, Mapping):
            continue
        if assignment_id is not None and _optional_text(raw.get("id")) == assignment_id:
            return {str(key): value for key, value in raw.items()}
        if task_id is not None and _optional_text(raw.get("task_id")) == task_id:
            return {str(key): value for key, value in raw.items()}
    return None


def _assignment_for_session(
    workspaces: _WorkspaceCache,
    *,
    item: Mapping[str, Any],
    binding: Mapping[str, Any],
    workspace_option: str | None,
) -> JsonObject:
    """The session's assignment: inline if the service sent it, else fetched."""

    from dragback.cli import CliError

    # Only a real assignment, not the binding's AssignmentLocator: the locator
    # carries ids alone, and returning it here would silently drop the interrupt
    # reason and the provenance path — the two things `why` exists to show.
    inline = item.get("assignment")
    if isinstance(inline, Mapping) and "state" in inline:
        return {str(key): value for key, value in inline.items()}

    workspace_id = _workspace_id_for(binding, workspace_option)
    assignment = _assignment_in_workspace(
        workspaces.get(workspace_id),
        assignment_id=_optional_text(_binding_field(binding, "assignment_id")),
        task_id=_optional_text(_binding_field(binding, "task_id")),
    )
    if assignment is None:
        raise CliError(
            f"Workspace {workspace_id} has no assignment "
            f"{_text(binding.get('assignment_id') or binding.get('task_id'))}.",
            code="ASSIGNMENT_NOT_FOUND",
        )
    return assignment


def _invalidation_report(workspace: Mapping[str, Any]) -> JsonObject | None:
    report = workspace.get("invalidation_report")
    if isinstance(report, Mapping):
        return {str(key): value for key, value in report.items()}
    return None


def _decision_id(item: Mapping[str, Any]) -> str | None:
    """The decision to acknowledge, as the service reported it — never inferred."""

    sources: list[Mapping[str, Any]] = [item, _binding_of(item)]
    assignment = item.get("assignment")
    if isinstance(assignment, Mapping):
        sources.append(assignment)
    for source in sources:
        for key in _DECISION_ID_KEYS:
            value = _optional_text(source.get(key))
            if value is not None:
                return value
    return None


def _evidence_ref(
    item: Mapping[str, Any],
    assignment: Mapping[str, Any],
    report: Mapping[str, Any] | None,
) -> str | None:
    sources: list[Mapping[str, Any]] = [assignment, item, _binding_of(item)]
    if report is not None:
        sources.append(report)
    for source in sources:
        value = _optional_text(source.get("evidence_ref"))
        if value is not None:
            return value
        refs = _string_list(source.get("evidence_refs"))
        if refs:
            return refs[0]
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set | frozenset):
        return [str(entry) for entry in value if entry not in (None, "")]
    return []


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _text(value: Any, default: str = _MISSING) -> str:
    return _optional_text(value) or default


# --------------------------------------------------------------------------- #
# Rendering helpers — docs/TERMINAL_OUTPUT_SPEC.md sections 3 and 4
# --------------------------------------------------------------------------- #

#: States that mean this session was stopped by an approved change.
_STOPPED_STATES = frozenset({"interrupted", "redirected"})


def _is_stopped_state(state: str) -> bool:
    return state.strip().casefold() in _STOPPED_STATES


def _changed_decision(workspace: JsonObject | None) -> str:
    """`DEC-018, approved, supersedes DEC-004` — from the applied mutation."""

    if not workspace:
        return ""
    mutation = workspace.get("latest_approved_mutation")
    if not isinstance(mutation, Mapping):
        return ""
    decision = mutation.get("decision")
    if not isinstance(decision, Mapping):
        return ""
    parts = [_text(decision.get("id"), "")]
    status = _optional_text(decision.get("approval_status"))
    if status:
        parts.append(status.casefold())
    supersedes = _optional_text(mutation.get("supersedes_id"))
    if supersedes:
        parts.append(f"supersedes {supersedes}")
    return ", ".join(part for part in parts if part)


def _attribution(workspace: JsonObject | None) -> str:
    """Who approved it and when. Omitted rather than invented."""

    if not workspace:
        return ""
    mutations = workspace.get("approved_mutations")
    if not isinstance(mutations, list) or not mutations:
        return ""
    latest = mutations[-1]
    if not isinstance(latest, Mapping):
        return ""
    role = _optional_text(latest.get("actor_role"))
    decision = latest.get("mutation")
    effective = ""
    if isinstance(decision, Mapping):
        inner = decision.get("decision")
        if isinstance(inner, Mapping):
            effective = _optional_text(inner.get("effective_at")) or ""
    return " · ".join(part for part in (role, effective) if part)


def _tasks_of(workspace: JsonObject | None) -> list[Mapping[str, Any]]:
    if not workspace:
        return []
    tasks = workspace.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [task for task in tasks if isinstance(task, Mapping)]


def _report_of(workspace: JsonObject | None) -> Mapping[str, Any]:
    if not workspace:
        return {}
    report = workspace.get("invalidation_report")
    return report if isinstance(report, Mapping) else {}


def _preserved_titles(workspace: JsonObject | None) -> list[str]:
    report = _report_of(workspace)
    preserved = report.get("preserved_task_ids")
    if not isinstance(preserved, list):
        return []
    titles = {
        _text(task.get("id"), ""): _text(task.get("title"), "")
        for task in _tasks_of(workspace)
    }
    return [titles[task_id] for task_id in preserved if titles.get(task_id)]


def _reached_rows(
    workspace: JsonObject | None,
    task_id: str,
    style: Style,
) -> list[str]:
    """Each task the change reached, labelled invalidated or still valid.

    Never collapsed into one list: showing the surviving sibling beside the
    stopped one is the whole selective-invalidation claim.
    """

    report = _report_of(workspace)
    invalidated = report.get("invalidated_task_ids")
    preserved = report.get("preserved_task_ids")
    invalid_ids = set(invalidated) if isinstance(invalidated, list) else set()
    preserved_ids = set(preserved) if isinstance(preserved, list) else set()

    rows: list[str] = []
    entries = [
        (
            _text(task.get("id"), ""),
            _text(task.get("title"), ""),
        )
        for task in _tasks_of(workspace)
    ]
    width = max((len(entry[0]) for entry in entries), default=0)
    title_width = max((len(entry[1]) for entry in entries), default=0)
    for entry_id, title in entries:
        if entry_id in invalid_ids:
            label = style.stopped("invalidated")
        elif entry_id in preserved_ids:
            label = style.preserved("still valid")
        else:
            continue
        marker = " ←" if entry_id == task_id else ""
        rows.append(
            f"{entry_id.ljust(width)}  {title.ljust(title_width)}  {label}{marker}"
        )
    return rows
