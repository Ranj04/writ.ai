"""A4/A6 — the Claude Code hook scripts and the local deny notification.

These test a *client*: the scripts under ``hooks/`` are stdlib-only and are not a
package, so they are loaded by path. Nothing here imports the backend.

The four claims worth breaking a build over:

* the PreToolUse request body carries only ``tool_name`` and ``timestamp``;
* the hook fails **closed** on its own errors, because Claude Code fails open;
* a transport failure degrades to this session's cached verdict, and a cache miss denies;
* ``additionalContext`` stays under 10,000 characters.
"""

from __future__ import annotations

import importlib.util
import io
import json
import socket
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "hooks"

ALLOW_WORD = "allow"
SECRET = "sk-super-secret-token-do-not-transmit"


def _load_hook_module(name: str) -> ModuleType:
    """Import a hook script by path under its real module name.

    The real name matters: the scripts ``import dragback_hook_lib``, so registering
    it in ``sys.modules`` first makes the tests and the scripts share one module
    object and monkeypatching works.
    """

    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "__file__", None) == str(HOOKS_DIR / f"{name}.py"):
        return cached
    spec = importlib.util.spec_from_file_location(name, HOOKS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lib = _load_hook_module("dragback_hook_lib")
pre_tool_use = _load_hook_module("dragback_pre_tool_use")
session_start = _load_hook_module("dragback_session_start")
session_end = _load_hook_module("dragback_session_end")


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _config(tmp_path: Path, **overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "endpoint": "http://localhost:8002/supervisor/sessions",
        "timeout_seconds": 3.0,
        "cache_path": tmp_path / ".dragback" / "hook-verdict-cache.json",
        "api_key": "",
        "notifications_enabled": False,
    }
    defaults.update(overrides)
    return lib.HookConfig(**defaults)


def _event(**overrides: Any) -> dict[str, Any]:
    """A realistic PreToolUse event, including the fields that must never be sent."""

    event: dict[str, Any] = {
        "session_id": "SESSION-abc123",
        "transcript_path": "/Users/dev/.claude/projects/dragback/transcript.jsonl",
        "cwd": "/Users/dev/code/dragback",
        "permission_mode": "acceptEdits",
        "tool_name": "Edit",
        "tool_use_id": "toolu_01",
        "tool_input": {
            "file_path": "/Users/dev/code/dragback/backend/dragback/secrets.py",
            "old_string": f"API_KEY = '{SECRET}'",
            "new_string": f"API_KEY = '{SECRET}-rotated'",
        },
    }
    event.update(overrides)
    return event


def _allow_verdict() -> dict[str, Any]:
    return {
        "decision": "allow",
        "reason": "Session is bound to a current assignment.",
        "bound": True,
        "assignment_id": "ASSIGNMENT-TASK-102",
        "task_id": "TASK-102",
        "redirect_instruction": None,
        "provenance_path": [],
        "evidence_ref": None,
        "decision_snapshot": "graph-v18",
        "correlation_id": "corr-1",
    }


def _deny_verdict() -> dict[str, Any]:
    return {
        "decision": "deny",
        "reason": "Decision DEC-018 revoked authorization for TASK-102.",
        "bound": True,
        "assignment_id": "ASSIGNMENT-TASK-102",
        "task_id": "TASK-102",
        "redirect_instruction": "Gate the export control behind an administrator check.",
        "provenance_path": ["DEC-018", "DEC-004", "SPEC-009", "TICKET-100", "TASK-102"],
        "evidence_ref": "https://dragback.local/evidence/DEC-018",
        "decision_snapshot": "graph-v18",
        "correlation_id": "corr-2",
    }


class _Recorder:
    """A transport that records what the hook tried to send."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response if response is not None else _allow_verdict()
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None,
        config: Any,
    ) -> dict[str, Any]:
        self.calls.append({"method": method, "url": url, "body": body, "config": config})
        return self.response


def _exploding(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise RuntimeError("supervisor unreachable")


def _hook_output(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload["hookSpecificOutput"]
    assert isinstance(result, dict)
    return result


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    return port


# --------------------------------------------------------------------------------------
# A real HTTP listener, so the privacy claim is asserted against transmitted bytes
# --------------------------------------------------------------------------------------

_CAPTURED: list[dict[str, Any]] = []
_RESPONSE: dict[str, Any] = {}


class _CapturingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        _CAPTURED.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": raw,
            }
        )
        payload = json.dumps(_RESPONSE).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler naming
        self._handle()

    def log_message(self, fmt: str, *args: Any) -> None:
        return


class _Supervisor:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    @property
    def captured(self) -> list[dict[str, Any]]:
        return _CAPTURED

    def respond_with(self, payload: dict[str, Any]) -> None:
        _RESPONSE.clear()
        _RESPONSE.update(payload)


@pytest.fixture()
def supervisor() -> Any:
    _CAPTURED.clear()
    _RESPONSE.clear()
    _RESPONSE.update(_allow_verdict())
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CapturingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Supervisor(f"http://127.0.0.1:{server.server_address[1]}/supervisor/sessions")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------------------
# 1. Privacy — the request body is a closed set of two keys
# --------------------------------------------------------------------------------------


def test_check_request_body_has_exactly_two_keys() -> None:
    body = lib.check_request_body("Edit", datetime(2026, 7, 24, 21, 0, tzinfo=UTC))
    assert set(body) == {"tool_name", "timestamp"}
    assert body == {"tool_name": "Edit", "timestamp": "2026-07-24T21:00:00Z"}


def test_pre_tool_use_body_never_carries_tool_input_or_paths(tmp_path: Path) -> None:
    recorder = _Recorder()
    lib.build_pre_tool_use_output(_event(), config=_config(tmp_path), transport=recorder)

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/SESSION-abc123/check")

    body = call["body"]
    # session_id is an identifier the service cross-checks against the path, not
    # content. The set stays closed and carries nothing derived from the tool.
    assert set(body) == {"session_id", "tool_name", "timestamp"}
    for forbidden in ("tool_input", "cwd", "transcript_path", "permission_mode", "tool_use_id"):
        assert forbidden not in body

    serialized = json.dumps(call["body"]) + call["url"]
    assert SECRET not in serialized
    assert "transcript.jsonl" not in serialized
    assert "/Users/dev/code/dragback" not in serialized


def test_no_secret_appears_in_the_bytes_actually_transmitted(
    tmp_path: Path, supervisor: Any
) -> None:
    """The privacy claim, asserted end to end against a real socket."""

    config = _config(tmp_path, endpoint=supervisor.base_url)
    output = lib.build_pre_tool_use_output(_event(), config=config)
    assert _hook_output(output)["permissionDecision"] == "allow"

    assert len(supervisor.captured) == 1
    request = supervisor.captured[0]
    transmitted = (
        request["method"].encode()
        + b" "
        + request["path"].encode()
        + json.dumps(request["headers"]).encode()
        + request["body"]
    )
    assert SECRET.encode() not in transmitted
    assert b"tool_input" not in transmitted
    assert b"transcript" not in transmitted
    assert b"permission_mode" not in transmitted

    assert json.loads(request["body"].decode()).keys() == {
        "session_id",
        "tool_name",
        "timestamp",
    }
    assert request["path"] == "/supervisor/sessions/SESSION-abc123/check"


# --------------------------------------------------------------------------------------
# 2. Fail closed — Claude Code fails open, so the script's own failure must deny
# --------------------------------------------------------------------------------------


def test_raising_transport_denies_when_nothing_is_cached(tmp_path: Path) -> None:
    output = lib.build_pre_tool_use_output(
        _event(), config=_config(tmp_path), transport=_exploding
    )
    result = _hook_output(output)
    assert result["permissionDecision"] == "deny"
    assert "unreachable" in result["permissionDecisionReason"]


def test_unreachable_supervisor_denies_through_the_real_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _closed_port()
    monkeypatch.setenv("DRAGBACK_HOOK_ENDPOINT", f"http://127.0.0.1:{port}/supervisor/sessions")
    monkeypatch.setenv("DRAGBACK_HOOK_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setenv("DRAGBACK_HOOK_NOTIFY", "0")

    stdout = io.StringIO()
    assert pre_tool_use.main(io.StringIO(json.dumps(_event())), stdout) == 0
    assert _hook_output(json.loads(stdout.getvalue()))["permissionDecision"] == "deny"


def test_internal_crash_in_the_script_still_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """An uncaught exception would let the tool call through. It must not escape."""

    def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ValueError("bug in the hook")

    # Patch what the entry script actually calls. `build_pre_tool_use_output`
    # now delegates to `build_pre_tool_use_result`, which returns the verdict
    # together with the redirect id the script acknowledges after emitting.
    monkeypatch.setattr(lib, "build_pre_tool_use_result", boom)
    stdout = io.StringIO()
    assert pre_tool_use.main(io.StringIO(json.dumps(_event())), stdout) == 0
    result = _hook_output(json.loads(stdout.getvalue()))
    assert result["permissionDecision"] == "deny"
    assert result["permissionDecisionReason"] == lib.REASON_INTERNAL_ERROR


def test_empty_or_garbage_stdin_denies() -> None:
    for raw in ("", "   ", "not json", "[]", "null"):
        stdout = io.StringIO()
        assert pre_tool_use.main(io.StringIO(raw), stdout) == 0
        result = _hook_output(json.loads(stdout.getvalue()))
        assert result["permissionDecision"] == "deny"
        assert result["permissionDecisionReason"] == lib.REASON_NO_SESSION_ID


def test_unrecognised_or_malformed_verdicts_deny(tmp_path: Path) -> None:
    for payload in ({"decision": "ask"}, {"decision": None}, {"reason": "no decision key"}, {}):
        output = lib.build_pre_tool_use_output(
            _event(), config=_config(tmp_path), transport=_Recorder(payload)
        )
        assert _hook_output(output)["permissionDecision"] == "deny"


def test_ask_is_never_emitted(tmp_path: Path) -> None:
    """A blocked agent needs a reason, not a prompt."""

    for verdict in (_allow_verdict(), _deny_verdict(), {"decision": "ask"}):
        output = lib.build_pre_tool_use_output(
            _event(), config=_config(tmp_path), transport=_Recorder(verdict)
        )
        assert _hook_output(output)["permissionDecision"] in ("allow", "deny")


# --------------------------------------------------------------------------------------
# 3. Explicit short timeout — the command-hook default of 600s disables enforcement
# --------------------------------------------------------------------------------------


def test_timeout_defaults_to_three_seconds() -> None:
    assert lib.HookConfig.from_env(env={}, cwd="/tmp").timeout_seconds == 3.0
    assert lib.DEFAULT_TIMEOUT_SECONDS == 3.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.5", 1.5),
        # Clamped, not honoured: the ceiling now sits below the command timeout,
        # because a request that can outlast it is a fail-open dressed as a
        # timeout.
        ("10", 4.0),
        ("", 3.0),
        ("nonsense", 3.0),
        ("0", 3.0),
        ("-4", 3.0),
    ],
)
def test_timeout_is_parsed_and_never_disabled(raw: str, expected: float) -> None:
    env = {"DRAGBACK_HOOK_TIMEOUT_SECONDS": raw}
    assert lib.HookConfig.from_env(env=env, cwd="/tmp").timeout_seconds == expected


def test_http_transport_passes_the_timeout_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _Response:
        # The real ``HTTPResponse.read`` takes an optional byte cap, and the
        # transport now passes one so an unbounded body cannot be read into a
        # hook that has to finish inside the command timeout.
        def read(self, amount: int | None = None) -> bytes:
            return b"{}"

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_exc: Any) -> None:
            return None

    def fake_urlopen(request: Any, timeout: Any = None) -> Any:
        seen["timeout"] = timeout
        seen["headers"] = dict(request.headers)
        return _Response()

    monkeypatch.setattr(lib.urllib.request, "urlopen", fake_urlopen)
    config = lib.HookConfig(timeout_seconds=2.5, api_key="token-123")
    lib.http_transport("POST", "http://example.invalid/check", {"tool_name": "Edit"}, config)

    assert seen["timeout"] == 2.5
    # The service authenticates on this header specifically; urllib title-cases
    # header names, so match case-insensitively rather than pinning the casing.
    headers = {key.lower(): value for key, value in seen["headers"].items()}
    assert headers[lib.HOOK_API_KEY_HEADER.lower()] == "token-123"
    assert "authorization" not in headers


def test_settings_examples_set_a_short_command_timeout() -> None:
    settings = json.loads((HOOKS_DIR / "settings.example.json").read_text())
    events = settings["hooks"]
    assert set(events) == {"SessionStart", "PreToolUse", "SessionEnd"}
    for entries in events.values():
        for entry in entries:
            for hook in entry["hooks"]:
                assert hook["type"] == "command"
                assert hook["timeout"] <= 10


# --------------------------------------------------------------------------------------
# 4. On-disk verdict cache
# --------------------------------------------------------------------------------------


def test_every_server_verdict_is_written_to_the_cache(tmp_path: Path) -> None:
    config = _config(tmp_path)
    lib.build_pre_tool_use_output(
        _event(), config=config, transport=_Recorder(_deny_verdict())
    )
    cached = lib.read_cached_verdict(config, "SESSION-abc123")
    assert cached is not None
    assert cached["decision"] == "deny"
    assert json.loads(config.cache_path.read_text())["version"] == lib.CACHE_VERSION


def test_transport_failure_falls_back_to_the_cached_deny(tmp_path: Path) -> None:
    config = _config(tmp_path)
    lib.write_cached_verdict(config, "SESSION-abc123", _deny_verdict())

    output = lib.build_pre_tool_use_output(_event(), config=config, transport=_exploding)
    result = _hook_output(output)
    assert result["permissionDecision"] == "deny"
    assert "cached" in result["permissionDecisionReason"]
    assert "administrator check" in result["additionalContext"]


def test_a_cached_allow_is_never_reused_during_an_outage(tmp_path: Path) -> None:
    """An outage must not authorize work.

    The cache exists so a network blip degrades to the last known DENY rather
    than to no enforcement. Replaying a cached *allow* would make an internal
    failure grant permission, which is the fail-open this hook exists to stop.
    """

    config = _config(tmp_path)
    lib.write_cached_verdict(config, "SESSION-abc123", _allow_verdict())

    output = lib.build_pre_tool_use_output(_event(), config=config, transport=_exploding)
    result = _hook_output(output)
    assert result["permissionDecision"] == "deny"


def test_an_undeliverable_verdict_exits_two_rather_than_allowing(tmp_path: Path) -> None:
    """If stdout cannot take the verdict, exit code 2 is the only way to block."""

    class _Unwritable(io.StringIO):
        def write(self, _text: str) -> int:
            raise OSError("stdout is gone")

    assert not lib.emit_json({"a": 1}, _Unwritable())
    assert pre_tool_use.main(io.StringIO(json.dumps(_event())), _Unwritable()) == 2


def test_a_notification_cannot_delay_the_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The decision reaches stdout before the notifier is allowed to run."""

    order: list[str] = []
    real_emit = lib.emit_json

    def spy_emit(payload: Any, stream: Any = None) -> bool:
        order.append("emit")
        return real_emit(payload, stream)

    def spy_notify(*_args: Any, **_kwargs: Any) -> None:
        order.append("notify")

    monkeypatch.setattr(lib, "emit_json", spy_emit)
    monkeypatch.setattr(lib, "notify_denied", spy_notify)
    monkeypatch.setattr(
        lib, "build_pre_tool_use_output", lambda *_a, **_k: lib.deny_output("blocked")
    )
    pre_tool_use.main(io.StringIO(json.dumps(_event())), io.StringIO())
    assert order == ["emit", "notify"]


def test_a_malformed_verdict_denies(tmp_path: Path) -> None:
    for payload in ({"decision": "maybe"}, {"reason": "hi"}, {"decision": ALLOW_WORD, "reason": 7}):
        output = lib.build_pre_tool_use_output(
            _event(),
            config=_config(tmp_path),
            transport=_Recorder(payload),
        )
        assert _hook_output(output)["permissionDecision"] == "deny", payload


def test_cache_miss_on_transport_failure_denies(tmp_path: Path) -> None:
    """A different session's cached allow must not authorize this one."""

    config = _config(tmp_path)
    lib.write_cached_verdict(config, "SESSION-other", _allow_verdict())

    output = lib.build_pre_tool_use_output(_event(), config=config, transport=_exploding)
    result = _hook_output(output)
    assert result["permissionDecision"] == "deny"
    assert result["permissionDecisionReason"] == lib.REASON_UNREACHABLE_NO_CACHE


def test_corrupt_cache_file_denies_rather_than_crashing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.cache_path.parent.mkdir(parents=True, exist_ok=True)
    config.cache_path.write_text("{not json at all")

    output = lib.build_pre_tool_use_output(_event(), config=config, transport=_exploding)
    assert _hook_output(output)["permissionDecision"] == "deny"


def test_unwritable_cache_never_changes_the_decision(tmp_path: Path) -> None:
    config = _config(tmp_path, cache_path=tmp_path / "nope" / "\0bad" / "cache.json")
    output = lib.build_pre_tool_use_output(
        _event(), config=config, transport=_Recorder(_allow_verdict())
    )
    assert _hook_output(output)["permissionDecision"] == "allow"


def test_cache_is_pruned_to_a_bounded_size(tmp_path: Path) -> None:
    config = _config(tmp_path)
    for index in range(lib.MAX_CACHED_SESSIONS + 25):
        lib.write_cached_verdict(config, f"SESSION-{index:04d}", _allow_verdict())
    assert len(lib.read_cache(config)["sessions"]) <= lib.MAX_CACHED_SESSIONS


# --------------------------------------------------------------------------------------
# 5. additionalContext budget
# --------------------------------------------------------------------------------------


def test_deny_context_carries_redirect_provenance_and_evidence(tmp_path: Path) -> None:
    output = lib.build_pre_tool_use_output(
        _event(), config=_config(tmp_path), transport=_Recorder(_deny_verdict())
    )
    result = _hook_output(output)
    assert result["permissionDecision"] == "deny"
    context = result["additionalContext"]
    assert "Gate the export control behind an administrator check." in context
    assert "DEC-018 → DEC-004 → SPEC-009 → TICKET-100 → TASK-102" in context
    assert "https://dragback.local/evidence/DEC-018" in context
    assert len(context) < lib.MAX_ADDITIONAL_CONTEXT_CHARS


def test_oversized_verdict_is_truncated_under_ten_thousand_characters(tmp_path: Path) -> None:
    monstrous = {
        "decision": "deny",
        "reason": "R" * 40_000,
        "assignment_id": "A" * 5_000,
        "task_id": "T" * 5_000,
        "decision_snapshot": "S" * 5_000,
        "correlation_id": "C" * 5_000,
        "redirect_instruction": "I" * 80_000,
        "provenance_path": [f"NODE-{index}-" + "x" * 900 for index in range(500)],
        "evidence_ref": "https://dragback.local/evidence/" + "e" * 50_000,
    }
    output = lib.build_pre_tool_use_output(
        _event(), config=_config(tmp_path), transport=_Recorder(monstrous)
    )
    result = _hook_output(output)

    assert result["permissionDecision"] == "deny"
    assert len(result["additionalContext"]) < lib.MAX_ADDITIONAL_CONTEXT_CHARS
    assert "truncated" in result["additionalContext"]
    assert "\n" not in result["permissionDecisionReason"]
    assert len(result["permissionDecisionReason"]) <= lib.MAX_REASON_CHARS


def test_worst_case_context_length_is_recorded() -> None:
    """Pin the measured worst case so a future field addition cannot creep past it."""

    worst = lib.build_additional_context(
        {
            "decision": "deny",
            "reason": "R" * 40_000,
            "assignment_id": "A" * 5_000,
            "task_id": "T" * 5_000,
            "decision_snapshot": "S" * 5_000,
            "correlation_id": "C" * 5_000,
            "redirect_instruction": "I" * 80_000,
            "provenance_path": ["N" * 900] * 500,
            "evidence_ref": "E" * 50_000,
        },
        stale=True,
    )
    # Re-measured after the deny block was reformatted to
    # docs/TERMINAL_OUTPUT_SPEC.md section 1. The pin moved DOWN (9,496 -> 8,235),
    # so the budget got safer, and the ceiling assertion below is unchanged.
    assert len(worst) == 8_235
    assert len(worst) < lib.MAX_ADDITIONAL_CONTEXT_CHARS


def test_global_truncation_is_deterministic_and_announced() -> None:
    truncated = lib.truncate_context("z" * 25_000)
    assert len(truncated) == lib.MAX_ADDITIONAL_CONTEXT_CHARS
    assert truncated.endswith(lib.TRUNCATION_NOTICE)
    assert truncated == lib.truncate_context("z" * 25_000)

    output = lib.decision_output("deny", "blocked", "y" * 25_000)
    assert len(_hook_output(output)["additionalContext"]) == lib.MAX_ADDITIONAL_CONTEXT_CHARS


def test_provenance_is_compacted_not_dropped() -> None:
    assert lib.compact_provenance(["A", "B", "C"]) == "A → B → C"
    long_path = lib.compact_provenance([f"N{index}" for index in range(40)])
    assert long_path.startswith("N0 → N1")
    assert long_path.endswith("N39")
    assert "28 more truncated" in long_path
    assert lib.compact_provenance(None) == ""


# --------------------------------------------------------------------------------------
# 6. Output shape
# --------------------------------------------------------------------------------------


def test_output_shape_is_exactly_the_contract(tmp_path: Path) -> None:
    for verdict in (_allow_verdict(), _deny_verdict()):
        output = lib.build_pre_tool_use_output(
            _event(), config=_config(tmp_path), transport=_Recorder(verdict)
        )
        assert set(output) == {"hookSpecificOutput"}
        result = _hook_output(output)
        assert set(result) == {
            "hookEventName",
            "permissionDecision",
            "permissionDecisionReason",
            "additionalContext",
        }
        assert result["hookEventName"] == "PreToolUse"
        assert result["permissionDecision"] == verdict["decision"]
        assert isinstance(result["permissionDecisionReason"], str)
        assert isinstance(result["additionalContext"], str)


def test_allowed_session_passes_through_with_the_server_reason(tmp_path: Path) -> None:
    output = lib.build_pre_tool_use_output(
        _event(), config=_config(tmp_path), transport=_Recorder(_allow_verdict())
    )
    result = _hook_output(output)
    assert result["permissionDecision"] == "allow"
    assert result["permissionDecisionReason"] == "Session is bound to a current assignment."
    assert result["additionalContext"] == ""


def test_script_writes_json_to_stdout_and_exits_zero(
    tmp_path: Path, supervisor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor.respond_with(_deny_verdict())
    monkeypatch.setenv("DRAGBACK_HOOK_ENDPOINT", supervisor.base_url)
    monkeypatch.setenv("DRAGBACK_HOOK_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setenv("DRAGBACK_HOOK_NOTIFY", "0")

    stdout = io.StringIO()
    assert pre_tool_use.main(io.StringIO(json.dumps(_event())), stdout) == 0
    result = _hook_output(json.loads(stdout.getvalue()))
    assert result["permissionDecision"] == "deny"
    assert "administrator check" in result["additionalContext"]


# --------------------------------------------------------------------------------------
# SessionStart
# --------------------------------------------------------------------------------------


def test_session_start_posts_only_where_the_session_is(tmp_path: Path) -> None:
    """The client reports location only; the service reads the marker files."""

    (tmp_path / ".dragback").mkdir()
    (tmp_path / ".dragback" / "attach").write_text("ASSIGNMENT-TASK-102\n")
    (tmp_path / ".dragback" / "task").write_text("TASK-102\n")

    binding = {"source": "explicit", "assignment_id": "A-1", "task_id": "TASK-102"}
    recorder = _Recorder({"binding": binding})
    output = session_start.build_output(
        {"session_id": "SESSION-1", "cwd": str(tmp_path)},
        config=_config(tmp_path),
        transport=recorder,
    )

    call = recorder.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/supervisor/sessions/start")
    assert set(call["body"]) == {"session_id", "cwd", "branch"}
    # Marker-file contents are NOT transmitted: the service reads them itself.
    serialized = json.dumps(call["body"])
    assert "ASSIGNMENT-TASK-102" not in serialized
    assert "TASK-102" not in serialized

    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "A-1" in output["hookSpecificOutput"]["additionalContext"]


def test_registration_branch_is_a_string_never_null(tmp_path: Path) -> None:
    """The service declares branch as `str`; null would fail its validation."""

    body = lib.register_request_body(str(tmp_path), None, "SESSION-1")
    assert body == {
        "session_id": "SESSION-1",
        "cwd": str(tmp_path),
        "branch": "",
    }


def test_git_branch_tolerates_failure_and_detached_head() -> None:
    def ok(_command: Any, _cwd: str) -> str:
        return "feat/TASK-102-csv-export\n"

    def detached(_command: Any, _cwd: str) -> str:
        return "HEAD\n"

    def broken(_command: Any, _cwd: str) -> str:
        raise OSError("git not installed")

    assert lib.git_branch("/tmp", runner=ok) == "feat/TASK-102-csv-export"
    assert lib.git_branch("/tmp", runner=detached) is None
    assert lib.git_branch("/tmp", runner=broken) is None
    assert lib.git_branch("/nonexistent-directory-for-dragback") is None


def test_session_start_survives_an_unreachable_supervisor(tmp_path: Path) -> None:
    output = session_start.build_output(
        {"session_id": "SESSION-1", "cwd": str(tmp_path)},
        config=_config(tmp_path),
        transport=_exploding,
    )
    assert "unreachable" in output["hookSpecificOutput"]["additionalContext"]


def test_session_start_describes_an_unbound_session(tmp_path: Path) -> None:
    output = session_start.build_output(
        {"session_id": "SESSION-1", "cwd": str(tmp_path)},
        config=_config(tmp_path),
        transport=_Recorder({"binding": {"source": "unbound", "assignment_id": None}}),
    )
    assert "UNBOUND" in output["hookSpecificOutput"]["additionalContext"]


def test_session_start_main_always_emits_json_and_exits_zero() -> None:
    stdout = io.StringIO()
    assert session_start.main(io.StringIO("garbage"), stdout) == 0
    assert json.loads(stdout.getvalue())["hookSpecificOutput"]["hookEventName"] == "SessionStart"


# --------------------------------------------------------------------------------------
# SessionEnd
# --------------------------------------------------------------------------------------


def test_session_end_deletes_the_session_and_drops_its_cache(tmp_path: Path) -> None:
    config = _config(tmp_path)
    lib.write_cached_verdict(config, "SESSION-abc123", _deny_verdict())
    recorder = _Recorder({})

    assert session_end.release_session(
        {"session_id": "SESSION-abc123", "cwd": str(tmp_path)},
        config=config,
        transport=recorder,
    )
    call = recorder.calls[0]
    # Lane B's router ends a session with POST /{id}/end, not DELETE.
    assert call["method"] == "POST"
    assert call["url"].endswith("/SESSION-abc123/end")
    assert call["body"] == {"session_id": "SESSION-abc123"}
    # A stale deny must not outlive the session that earned it.
    assert lib.read_cached_verdict(config, "SESSION-abc123") is None


def test_session_end_survives_a_failing_supervisor(tmp_path: Path) -> None:
    assert not session_end.release_session(
        {"session_id": "SESSION-abc123", "cwd": str(tmp_path)},
        config=_config(tmp_path),
        transport=_exploding,
    )
    stdout = io.StringIO()
    assert session_end.main(io.StringIO(""), stdout) == 0
    assert json.loads(stdout.getvalue()) == {}


# --------------------------------------------------------------------------------------
# A6 — the desktop notification, which may never influence a verdict
# --------------------------------------------------------------------------------------


def test_notification_failure_does_not_change_the_emitted_decision(
    tmp_path: Path, supervisor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor.respond_with(_deny_verdict())
    monkeypatch.setenv("DRAGBACK_HOOK_ENDPOINT", supervisor.base_url)
    monkeypatch.setenv("DRAGBACK_HOOK_CACHE_PATH", str(tmp_path / "cache.json"))

    def exploding_notifier(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("notification subsystem is on fire")

    monkeypatch.setattr(lib, "notify_denied", exploding_notifier)

    stdout = io.StringIO()
    assert pre_tool_use.main(io.StringIO(json.dumps(_event())), stdout) == 0
    result = _hook_output(json.loads(stdout.getvalue()))
    assert result["permissionDecision"] == "deny"
    assert result["permissionDecisionReason"] == _deny_verdict()["reason"]


def test_notify_denied_swallows_a_failing_runner() -> None:
    def broken(_command: Any) -> None:
        raise OSError("osascript died")

    config = lib.HookConfig(notifications_enabled=True)
    assert lib.notify_denied(config, "blocked", runner=broken, system="darwin") is False


def test_notify_denied_fires_once_per_deny() -> None:
    seen: list[Any] = []
    config = lib.HookConfig(notifications_enabled=True)
    assert lib.notify_denied(config, "blocked", runner=seen.append, system="darwin") is True
    assert seen[0][0] == "osascript"
    assert "display notification" in seen[0][2]


def test_notifications_are_easy_to_disable() -> None:
    disabled = lib.HookConfig.from_env(env={"DRAGBACK_HOOK_NOTIFY": "0"}, cwd="/tmp")
    assert disabled.notifications_enabled is False
    calls: list[Any] = []
    assert lib.notify_denied(disabled, "blocked", runner=calls.append, system="darwin") is False
    assert calls == []
    assert lib.HookConfig.from_env(env={}, cwd="/tmp").notifications_enabled is True


def test_notification_command_per_platform() -> None:
    darwin = lib.notification_command("darwin", "Dragback", 'say "hi"')
    assert darwin is not None and darwin[0] == "osascript"
    assert '\\"hi\\"' in darwin[2]
    linux = lib.notification_command("linux", "Dragback", "blocked")
    assert linux == ["notify-send", "Dragback", "blocked"]
    assert lib.notification_command("win32", "Dragback", "blocked") is None


def test_no_notification_on_allow(
    tmp_path: Path, supervisor: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor.respond_with(_allow_verdict())
    monkeypatch.setenv("DRAGBACK_HOOK_ENDPOINT", supervisor.base_url)
    monkeypatch.setenv("DRAGBACK_HOOK_CACHE_PATH", str(tmp_path / "cache.json"))

    fired: list[Any] = []

    def record_notification(*args: Any, **_kwargs: Any) -> bool:
        fired.append(args)
        return True

    monkeypatch.setattr(lib, "notify_denied", record_notification)

    stdout = io.StringIO()
    assert pre_tool_use.main(io.StringIO(json.dumps(_event())), stdout) == 0
    assert _hook_output(json.loads(stdout.getvalue()))["permissionDecision"] == "allow"
    assert fired == []


# --------------------------------------------------------------------------------------
# Packaging claims
# --------------------------------------------------------------------------------------


def test_managed_settings_forbid_switching_the_hook_off() -> None:
    managed = json.loads((HOOKS_DIR / "managed-settings.example.json").read_text())
    assert managed["allowManagedHooksOnly"] is True
    assert set(managed["hooks"]) == {"SessionStart", "PreToolUse", "SessionEnd"}


def test_readme_states_what_still_fails_open() -> None:
    readme = (HOOKS_DIR / "README.md").read_text()
    assert "fails open" in readme.lower()
    assert "PR check" in readme


def test_hooks_import_no_third_party_packages() -> None:
    """The hook runs in the developer's environment. stdlib only, no exceptions."""

    banned = ("httpx", "requests", "pydantic", "fastapi", "dragback.", "yaml")
    for script in sorted(HOOKS_DIR.glob("*.py")):
        source = script.read_text()
        for name in banned:
            assert f"import {name}" not in source, f"{script.name} imports {name}"


def test_the_request_timeout_is_capped_below_the_command_deadline() -> None:
    """A long HTTP wait is indistinguishable from no enforcement.

    The hook process must finish inside Claude Code's command timeout; one that
    is killed before it writes has, in effect, allowed the call.
    """

    config = lib.HookConfig.from_env({lib.ENV_TIMEOUT: "600"})
    assert config.timeout_seconds == lib.MAX_TIMEOUT_SECONDS
    assert lib.HookConfig.from_env({lib.ENV_TIMEOUT: "2"}).timeout_seconds == 2.0


def test_a_verdict_with_no_usable_reason_denies(tmp_path: Path) -> None:
    """Both the developer and the model read the reason. Empty is not a verdict."""

    for payload in ({"decision": "allow"}, {"decision": "allow", "reason": "   "}):
        output = lib.build_pre_tool_use_output(
            _event(), config=_config(tmp_path), transport=_Recorder(payload)
        )
        assert _hook_output(output)["permissionDecision"] == "deny", payload


def test_the_whole_hook_process_fits_inside_the_command_deadline() -> None:
    """A request that can outlast the command timeout is a fail-open.

    Claude Code kills a hook that overruns its configured timeout, and a process
    killed before it writes has, in effect, allowed the call. The cross-model
    review of the integration found the HTTP ceiling set to 10 seconds against a
    5-second command timeout, so a slow supervisor could take enforcement down
    without ever emitting a deny.
    """

    configured = {
        hook["timeout"]
        for name in ("settings.example.json", "managed-settings.example.json")
        for entries in json.loads((HOOKS_DIR / name).read_text())["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    }
    assert configured == {lib.COMMAND_TIMEOUT_SECONDS}, (
        "the shipped settings no longer match the budget this file reasons about"
    )

    # The HTTP wait, and every subprocess, must leave room to write the verdict.
    assert lib.MAX_TIMEOUT_SECONDS < lib.COMMAND_TIMEOUT_SECONDS
    assert lib.NOTIFY_TIMEOUT_SECONDS < lib.COMMAND_TIMEOUT_SECONDS
    assert lib.GIT_TIMEOUT_SECONDS < lib.COMMAND_TIMEOUT_SECONDS

    # And a configured value above the ceiling is clamped, not honoured.
    assert lib.HookConfig.from_env({lib.ENV_TIMEOUT: "600"}).timeout_seconds == (
        lib.MAX_TIMEOUT_SECONDS
    )


# --------------------------------------------------------------------------------------
# INT-3 — the redirect acknowledgement, and its ordering
# --------------------------------------------------------------------------------------


def _redirect_verdict(redirect_id: str = "sha256:" + "b" * 64) -> dict[str, Any]:
    verdict = _deny_verdict()
    verdict["redirect_id"] = redirect_id
    return verdict


def test_the_ack_is_recorded_only_after_the_verdict_reaches_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordering IS the fix.

    Acknowledging when the response arrives would tell the service a redirect
    was delivered even if this process were killed before writing it — and the
    service would then advance past an interrupt nobody ever saw. So the ack is
    written after `emit_json` succeeds, and never when it fails.
    """

    cache = tmp_path / "cache.json"
    monkeypatch.setenv("DRAGBACK_HOOK_CACHE_PATH", str(cache))
    monkeypatch.setenv("DRAGBACK_HOOK_NOTIFY", "0")
    monkeypatch.setattr(lib, "http_transport", _Recorder(_redirect_verdict()))

    class _Unwritable(io.StringIO):
        def write(self, _text: str) -> int:
            raise OSError("stdout is gone")

    # stdout unusable: exit 2 blocks the call, and NOTHING is acknowledged.
    assert pre_tool_use.main(io.StringIO(json.dumps(_event())), _Unwritable()) == 2
    config = lib.HookConfig.from_env(cwd=str(tmp_path))
    assert lib.read_delivered_redirect_id(config, "SESSION-abc123") is None

    # stdout works: the redirect really did reach the agent, so it is recorded.
    stdout = io.StringIO()
    assert pre_tool_use.main(io.StringIO(json.dumps(_event())), stdout) == 0
    assert _hook_output(json.loads(stdout.getvalue()))["permissionDecision"] == "deny"
    assert lib.read_delivered_redirect_id(config, "SESSION-abc123") == (
        "sha256:" + "b" * 64
    )


def test_the_next_check_echoes_the_delivered_redirect_id(tmp_path: Path) -> None:
    config = _config(tmp_path)
    recorder = _Recorder(_redirect_verdict())

    # First call sends no acknowledgement, because none has been delivered yet.
    lib.build_pre_tool_use_output(_event(), config=config, transport=recorder)
    assert "acknowledged_redirect_id" not in recorder.calls[0]["body"]

    lib.record_delivered_redirect_id(config, "SESSION-abc123", "sha256:" + "b" * 64)

    lib.build_pre_tool_use_output(_event(), config=config, transport=recorder)
    assert recorder.calls[1]["body"]["acknowledged_redirect_id"] == (
        "sha256:" + "b" * 64
    )
    # Still a closed set. The ack is a service-issued identifier, not content.
    assert set(recorder.calls[1]["body"]) == {
        "session_id",
        "tool_name",
        "timestamp",
        "acknowledged_redirect_id",
    }


def test_a_replayed_cached_deny_never_acknowledges(tmp_path: Path) -> None:
    """An outage must not look like a delivered redirect.

    The service never saw the call, so its interrupt has to stay open. Returning
    an acknowledgement for a verdict the service did not issue on this call
    would advance it on the strength of a cache read.
    """

    config = _config(tmp_path)
    lib.write_cached_verdict(config, "SESSION-abc123", _redirect_verdict())

    output, redirect_id = lib.build_pre_tool_use_result(
        _event(), config=config, transport=_exploding
    )

    assert _hook_output(output)["permissionDecision"] == "deny"
    assert redirect_id is None
