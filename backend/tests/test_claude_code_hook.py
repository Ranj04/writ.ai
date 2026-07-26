"""The behaviours Lane B's ``claude_code_hook.py`` asserted, repointed at the hook that ships.

Two hook implementations coexisted after the lane merge: Lane B's single
``hooks/claude_code_hook.py`` and Lane A's ``hooks/writai_*.py``. Lane A's
ships — it is the one proven end to end against real Claude Code sessions, and
the one that survived the audit that found four fail-open holes. Lane B's is
deleted.

These tests are **not** deleted with it. Each one below is Lane B's assertion,
re-expressed against Lane A's module surface, so nothing Lane B was protecting
loses its guard. ``test_hooks.py`` covers Lane A's own 50 cases; this file
covers the eight Lane B contributed, including the three hardenings ported
across in the same commit:

* the whole stdout payload, not only ``additionalContext``, stays under 10,000;
* the verdict cache is written ``0600`` inside a ``0700`` directory;
* an oversized response body is a transport failure, which denies.
"""

from __future__ import annotations

import importlib.util
import io
import json
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "hooks"


def _load_hook_module(name: str) -> ModuleType:
    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "__file__", None) == str(HOOKS_DIR / f"{name}.py"):
        return cached
    spec = importlib.util.spec_from_file_location(name, HOOKS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lib = _load_hook_module("writai_hook_lib")
pre_tool_use = _load_hook_module("writai_pre_tool_use")
session_start = _load_hook_module("writai_session_start")
session_end = _load_hook_module("writai_session_end")

API_KEY = "developer-hook-api-key"


def _configuration(tmp_path: Path, **overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "endpoint": "http://agent.test/supervisor/sessions",
        "timeout_seconds": 0.25,
        "cache_path": tmp_path / "verdicts.json",
        "api_key": API_KEY,
        "notifications_enabled": False,
    }
    defaults.update(overrides)
    return lib.HookConfig(**defaults)


def _decision(output: dict[str, Any]) -> str:
    return str(output["hookSpecificOutput"]["permissionDecision"])


def test_pre_tool_hook_transmits_only_privacy_allowlist(tmp_path: Path) -> None:
    """Lane B's privacy assertion, plus the cache file mode it also pinned."""

    requests: list[tuple[str, str, dict[str, Any] | None, Any]] = []

    def transport(
        method: str,
        url: str,
        body: dict[str, Any] | None,
        config: Any,
    ) -> dict[str, Any]:
        requests.append((method, url, body, config))
        return {"decision": "allow", "reason": "Assignment is current."}

    output = lib.build_pre_tool_use_output(
        {
            "session_id": "session-private",
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/private/payroll.csv",
                "content": "super-secret-file-contents",
            },
            "transcript_path": "/private/transcript.jsonl",
            "cwd": str(tmp_path),
            "tool_use_id": "toolu-secret",
        },
        config=_configuration(tmp_path),
        transport=transport,
        now=datetime(2026, 7, 25, tzinfo=UTC),
    )

    assert _decision(output) == "allow"
    assert len(requests) == 1
    _method, _url, payload, config = requests[0]
    assert payload == {
        "session_id": "session-private",
        "tool_name": "Write",
        "timestamp": "2026-07-25T00:00:00Z",
    }
    assert config.timeout_seconds == 0.25
    assert config.api_key == API_KEY

    serialized_request = json.dumps(payload)
    cache_file = tmp_path / "verdicts.json"
    serialized_cache = cache_file.read_text(encoding="utf-8")
    for private_value in (
        "super-secret-file-contents",
        "/private/payroll.csv",
        "/private/transcript.jsonl",
        "toolu-secret",
        API_KEY,
    ):
        assert private_value not in serialized_request
        assert private_value not in serialized_cache

    # The API key travels in a header, never in a body and never on disk.
    assert lib.HOOK_API_KEY_HEADER == "X-writ.ai-Hook-API-Key"
    assert stat.S_IMODE(cache_file.stat().st_mode) == lib.CACHE_FILE_MODE


def test_deny_payload_is_bounded_under_ten_thousand_characters(tmp_path: Path) -> None:
    """The *whole* stdout payload stays inside Claude Code's budget, not just one field."""

    def transport(
        _method: str,
        _url: str,
        _body: dict[str, Any] | None,
        _config: Any,
    ) -> dict[str, Any]:
        return {
            "decision": "deny",
            "reason": "Changed\nrequirement " + ("R" * 5_000),
            "redirect_instruction": "redirect " + ("X" * 20_000),
            "provenance_path": ["NODE-" + ("P" * 500)] * 200,
            "evidence_ref": "slack://evidence/" + ("E" * 5_000),
        }

    output = lib.build_pre_tool_use_output(
        {"session_id": "session-denied", "tool_name": "Bash", "cwd": str(tmp_path)},
        config=_configuration(tmp_path),
        transport=transport,
    )
    specific = output["hookSpecificOutput"]

    assert specific["permissionDecision"] == "deny"
    assert "\n" not in specific["permissionDecisionReason"]
    assert len(specific["additionalContext"]) < lib.MAX_ADDITIONAL_CONTEXT_CHARS
    assert (
        len(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        < lib.MAX_HOOK_PAYLOAD_CHARS
    )
    # The deny block still explains itself rather than shrinking to a badge.
    assert "slack://evidence/" in specific["additionalContext"]
    assert "Why" in specific["additionalContext"]


def test_service_failure_emits_deny_and_reuses_cached_deny(tmp_path: Path) -> None:
    config = _configuration(tmp_path)

    def initial(
        _method: str,
        _url: str,
        _body: dict[str, Any] | None,
        _config: Any,
    ) -> dict[str, Any]:
        return {
            "decision": "deny",
            "reason": "Approved requirement changed.",
            "redirect_instruction": "Use the corrected plan.",
            "provenance_path": ["DEC-018", "TASK-102"],
            "evidence_ref": "slack://approvals/DEC-018",
        }

    first = lib.build_pre_tool_use_output(
        {"session_id": "session-cache", "tool_name": "Edit", "cwd": str(tmp_path)},
        config=config,
        transport=initial,
    )
    assert _decision(first) == "deny"

    def unavailable(
        _method: str,
        _url: str,
        _body: dict[str, Any] | None,
        _config: Any,
    ) -> dict[str, Any]:
        raise TimeoutError("service did not respond")

    cached = lib.build_pre_tool_use_output(
        {"session_id": "session-cache", "tool_name": "Edit", "cwd": str(tmp_path)},
        config=config,
        transport=unavailable,
    )
    specific = cached["hookSpecificOutput"]
    assert _decision(cached) == "deny"
    assert "cached" in specific["permissionDecisionReason"]
    assert "Use the corrected plan." in specific["additionalContext"]
    # The developer is told the verdict is stale rather than current.
    assert "unreachable" in specific["additionalContext"].lower()

    uncached = lib.build_pre_tool_use_output(
        {"session_id": "session-no-cache", "tool_name": "Read", "cwd": str(tmp_path)},
        config=config,
        transport=unavailable,
    )
    assert _decision(uncached) == "deny"
    assert uncached["hookSpecificOutput"]["permissionDecisionReason"] == (
        lib.REASON_UNREACHABLE_NO_CACHE
    )


def test_cached_allow_never_authorizes_during_outage(tmp_path: Path) -> None:
    """The fail-open hole the cross-model audit found. It stays closed."""

    config = _configuration(tmp_path)

    def allowed(
        _method: str,
        _url: str,
        _body: dict[str, Any] | None,
        _config: Any,
    ) -> dict[str, Any]:
        return {"decision": "allow", "reason": "Current assignment."}

    assert (
        _decision(
            lib.build_pre_tool_use_output(
                {"session_id": "session-allow-cache", "tool_name": "Read", "cwd": str(tmp_path)},
                config=config,
                transport=allowed,
            )
        )
        == "allow"
    )
    assert lib.read_cached_verdict(config, "session-allow-cache") is not None

    def unavailable(
        _method: str,
        _url: str,
        _body: dict[str, Any] | None,
        _config: Any,
    ) -> dict[str, Any]:
        raise OSError("offline")

    failed = lib.build_pre_tool_use_output(
        {"session_id": "session-allow-cache", "tool_name": "Read", "cwd": str(tmp_path)},
        config=config,
        transport=unavailable,
    )
    assert _decision(failed) == "deny"
    assert failed["hookSpecificOutput"]["permissionDecisionReason"] == (
        lib.REASON_UNREACHABLE_NO_CACHE
    )


def test_session_hooks_send_only_lifecycle_binding_fields(tmp_path: Path) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    keys: list[str] = []

    def transport(
        method: str,
        url: str,
        body: dict[str, Any] | None,
        config: Any,
    ) -> dict[str, Any]:
        calls.append((method, url, body))
        keys.append(config.api_key)
        return {"binding": {"source": "branch", "task_id": "TASK-102", "assignment_id": "A-102"}}

    raw = {
        "session_id": "session-lifecycle",
        "cwd": str(tmp_path),
        "transcript_path": "/private/transcript.jsonl",
        "model": "claude-test",
    }
    config = _configuration(tmp_path)
    session_start.build_output(
        raw,
        config=config,
        transport=transport,
    )
    session_end.release_session(raw, config=config, transport=transport)

    start_call, end_call = calls
    assert start_call[0] == "POST"
    assert start_call[1].endswith("/supervisor/sessions/start")
    # SessionStart is the one documented exception: cwd and branch, so the
    # service can read `.writai/task` itself. Never marker-file contents,
    # never the transcript path, never the model.
    assert set(start_call[2] or {}) == {"session_id", "cwd", "branch"}
    assert (start_call[2] or {})["cwd"] == str(tmp_path)

    assert end_call[0] == "POST"
    assert end_call[1].endswith("/supervisor/sessions/session-lifecycle/end")
    assert end_call[2] == {"session_id": "session-lifecycle"}

    assert keys == [API_KEY, API_KEY]
    for _method, _url, body in calls:
        assert "/private/transcript.jsonl" not in json.dumps(body)


def test_malformed_pre_tool_input_still_exits_zero_with_explicit_deny(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WRITAI_HOOK_CACHE_PATH", str(tmp_path / "malformed-cache.json"))
    monkeypatch.setenv("WRITAI_HOOK_NOTIFY", "0")
    output = io.StringIO()

    exit_code = pre_tool_use.main(stdin=io.StringIO("{not-json"), stdout=output)

    assert exit_code == 0
    assert _decision(json.loads(output.getvalue())) == "deny"


def test_missing_hook_api_key_fails_closed_without_entering_json_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No key configured means the service rejects the hook, and rejection denies."""

    monkeypatch.delenv("WRITAI_HOOK_API_KEY", raising=False)
    monkeypatch.setenv("WRITAI_HOOK_CACHE_PATH", str(tmp_path / "no-key-cache.json"))
    monkeypatch.setenv("WRITAI_HOOK_NOTIFY", "0")
    # A port nothing is listening on: the transport fails exactly as it would
    # against the service's 503 HOOK_AUTHENTICATION_NOT_CONFIGURED.
    monkeypatch.setenv("WRITAI_HOOK_ENDPOINT", "http://127.0.0.1:1/supervisor/sessions")

    assert lib.HookConfig.from_env(cwd=str(tmp_path)).api_key == ""

    stream = io.StringIO()
    exit_code = pre_tool_use.main(
        stdin=io.StringIO(json.dumps({"session_id": "session-no-key", "tool_name": "Read"})),
        stdout=stream,
    )

    assert exit_code == 0
    assert _decision(json.loads(stream.getvalue())) == "deny"
    assert "WRITAI_HOOK_API_KEY" not in stream.getvalue()


def test_managed_settings_make_the_hook_non_removable_and_short_timeout() -> None:
    settings = json.loads(
        (HOOKS_DIR / "managed-settings.example.json").read_text(encoding="utf-8")
    )

    assert settings["allowManagedHooksOnly"] is True
    hooks = settings["hooks"]
    assert set(hooks) == {"SessionStart", "PreToolUse", "SessionEnd"}
    commands = [
        command for groups in hooks.values() for group in groups for command in group["hooks"]
    ]
    assert all(command["type"] == "command" for command in commands)
    assert all(command["timeout"] <= 5 for command in commands)

    # The non-removable configuration must name the hardened scripts. It pointed
    # at the deleted implementation until this merge, which is exactly how the
    # unhardened hook gets wired.
    wired = " ".join(command["command"] for command in commands)
    assert "claude_code_hook.py" not in wired
    for script in (
        "writai_session_start.py",
        "writai_pre_tool_use.py",
        "writai_session_end.py",
    ):
        assert script in wired
        assert (HOOKS_DIR / script).is_file()


def test_only_one_hook_implementation_is_installed() -> None:
    """Two hooks in one directory means someone eventually wires the unhardened one."""

    assert not (HOOKS_DIR / "claude_code_hook.py").exists()
    assert sorted(path.name for path in HOOKS_DIR.glob("*.py")) == [
        "writai_hook_lib.py",
        "writai_pre_tool_use.py",
        "writai_session_end.py",
        "writai_session_start.py",
    ]


def test_an_oversized_response_body_denies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ported from Lane B: an unbounded read inside the hook is a self-inflicted DoS."""

    class _Response:
        def read(self, size: int | None = None) -> bytes:
            return b"x" * (lib.MAX_RESPONSE_BYTES + 1)

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(lib.urllib.request, "urlopen", lambda *_a, **_k: _Response())

    with pytest.raises(lib.HookTransportError):
        lib.http_transport("POST", "http://agent.test/check", {}, _configuration(tmp_path))

    output = lib.build_pre_tool_use_output(
        {"session_id": "session-huge", "tool_name": "Read", "cwd": str(tmp_path)},
        config=_configuration(tmp_path),
    )
    assert _decision(output) == "deny"


def test_a_non_http_endpoint_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ported from Lane B: `file://` would make urllib hand back a local file as a verdict."""

    for hostile in ("file:///etc/passwd", "ftp://agent.test", "not-a-url"):
        config = lib.HookConfig.from_env({lib.ENV_ENDPOINT: hostile})
        assert config.endpoint == lib.DEFAULT_ENDPOINT
