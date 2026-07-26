#!/usr/bin/env python3
"""writ.ai SessionEnd hook — releases the session binding.

DELETEs the session and drops its cached verdict so a stale deny cannot outlive the
session that earned it. Every failure is swallowed; always exit code 0.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

import writai_hook_lib as lib  # noqa: E402


def release_session(
    event: Dict[str, Any],
    config: Optional[lib.HookConfig] = None,
    transport: Optional[lib.Transport] = None,
) -> bool:
    """Returns whether the supervisor confirmed the release."""

    try:
        cwd = event.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            cwd = os.getcwd()
        active_config = config or lib.HookConfig.from_env(cwd=cwd)
        session_id = str(event.get("session_id") or "").strip()
        if not session_id:
            return False

        lib.forget_cached_verdict(active_config, session_id)
        try:
            send: lib.Transport = transport or lib.http_transport
            send(
                "POST",
                active_config.session_url(session_id),
                {"session_id": session_id},
                active_config,
            )
        except Exception:
            return False
        return True
    except Exception:
        return False


def main(stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> int:
    try:
        release_session(lib.read_event(stdin))
    except Exception:
        pass
    lib.emit_json({}, stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
