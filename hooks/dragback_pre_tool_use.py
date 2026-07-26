#!/usr/bin/env python3
"""Dragback PreToolUse hook — the enforcement point.

Reads the hook event on stdin, asks the Dragback supervisor whether this session is
still authorized, and writes the permission decision to stdout. Exit code 0 in every
normal case; exit code 2 only when the verdict could not be written at all, because
that is then the only remaining way to block the call.

The request body is exactly ``{"session_id": ..., "tool_name": ..., "timestamp": ...}``.
Tool input, file contents, the transcript path, the permission mode and the cwd never
leave this machine on this call. (SessionStart separately sends cwd and branch once, so
the service can read ``.dragback/task`` itself — see ``dragback_hook_lib``.)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dragback_hook_lib as lib  # noqa: E402


def main(stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> int:
    payload: Dict[str, Any]
    config = None
    session_id = ""
    redirect_id = None
    try:
        event = lib.read_event(stdin)
        config = lib.HookConfig.from_env(cwd=event.get("cwd") if isinstance(event, dict) else None)
        raw_session = event.get("session_id") if isinstance(event, dict) else None
        session_id = raw_session.strip() if isinstance(raw_session, str) else ""
        payload, redirect_id = lib.build_pre_tool_use_result(event, config=config)
    except Exception:
        # Hooks fail OPEN in Claude Code, so this script's own failure must close.
        payload = lib.deny_output(lib.REASON_INTERNAL_ERROR)

    # Emit BEFORE notifying. The HTTP call may already have spent the request
    # timeout; adding a notification in front of the write can push the process
    # past the hook's command timeout, and a hook killed before it writes is a
    # hook that allowed the call.
    if not lib.emit_json(payload, stdout):
        # We could not deliver a verdict at all. Exit code 2 is the only
        # remaining way to block, so use it here and nowhere else.
        #
        # Deliberately no acknowledgement here: the verdict never reached the
        # agent, so the service must keep this interrupt open and re-deliver it.
        return 2

    # The verdict IS on stdout now, so the redirect has genuinely been delivered.
    # Only at this point may we tell the service, on the next check, that it can
    # advance the assignment. Recording it any earlier — when the response
    # arrived, say — would acknowledge a redirect a killed process never showed
    # anyone, and the service would move past an interrupt nobody received.
    if config is not None and session_id:
        try:
            lib.record_delivered_redirect_id(config, session_id, redirect_id)
        except Exception:
            # A failure here costs one repeated redirect, never a missed one.
            pass

    if lib.is_denied(payload):
        # A6. A notification failure must never change the verdict, so the call
        # site is wrapped as well as the notifier itself. By now the decision is
        # already on stdout, so nothing here can affect enforcement.
        try:
            config = lib.HookConfig.from_env()
            lib.notify_denied(config, lib.decision_reason(payload))
        except Exception:
            pass
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
