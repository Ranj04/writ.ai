#!/usr/bin/env python3
"""Dragback SessionStart hook — registers the session with the supervisor.

Reports facts only: cwd, git branch, ``.dragback/attach``, ``.dragback/task``. The
client never resolves a binding; the server does that deterministically.

Registration failure is not fatal — PreToolUse still fails closed on its own. Always
exit code 0.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dragback_hook_lib as lib  # noqa: E402


def build_output(
    event: Dict[str, Any],
    config: Optional[lib.HookConfig] = None,
    transport: Optional[lib.Transport] = None,
) -> Dict[str, Any]:
    """Register, and describe the resulting binding back to the model."""

    try:
        cwd = event.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            cwd = os.getcwd()
        active_config = config or lib.HookConfig.from_env(cwd=cwd)
        send: lib.Transport = transport or lib.http_transport

        session_id = str(event.get("session_id") or "").strip()
        if not session_id:
            return _context(
                "Dragback could not register this session (no session id in the hook "
                "event). Tool calls will be denied until a session registers."
            )

        body = lib.register_request_body(cwd, lib.git_branch(cwd), session_id)
        try:
            response = send("POST", active_config.register_url(session_id), body, active_config)
        except Exception:
            return _context(
                "Dragback supervisor unreachable at session start; this session is not "
                "registered. Start the agent service, or tool calls will be denied."
            )
        return _context(lib.describe_binding(response))
    except Exception:
        return _context("Dragback session registration failed.")


def _context(text: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": lib.HOOK_EVENT_SESSION_START,
            "additionalContext": lib.truncate_context(text),
        }
    }


def main(stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> int:
    try:
        payload = build_output(lib.read_event(stdin))
    except Exception:
        payload = _context("Dragback session registration failed.")
    lib.emit_json(payload, stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
