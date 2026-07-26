"""Terminal styling for writ.ai's CLI, per `docs/TERMINAL_OUTPUT_SPEC.md`.

Plain ANSI escapes only — no `rich`, no `textual`. For most of the demo the
terminal *is* the interface, so it has to render identically on a projector, in
a screen recording, and in a pipe.

The palette is deliberately three colours: dim grey for labels, one amber for
what is stopping, green for what is preserved. Adding a fourth is how terminal
output starts looking like a dashboard.

Colour is dropped entirely when `NO_COLOR` is set or the stream is not a TTY, so
piping to a file yields the same text without escape sequences.
"""
from __future__ import annotations

import os
from typing import IO

# Claude-Code-style line leaders.
BLOCK = "⏺"  # a block of output
STOPPED = "⏹"  # this session was stopped
CONTINUING = "⏵"  # this session is still running
UNBOUND = "—"  # registered but not matched to an assignment

INDENT = "  "

_RESET = "\033[0m"
_DIM = "\033[90m"  # labels
_AMBER = "\033[33m"  # the single accent: what is stopping
_GREEN = "\033[32m"  # what is preserved


def supports_colour(stream: IO[str]) -> bool:
    """`NO_COLOR` wins, then TTY. Never guess from TERM alone."""

    if os.environ.get("NO_COLOR") is not None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


class Style:
    """Applies or drops ANSI codes. One instance per rendered command."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        return f"{code}{text}{_RESET}"

    def label(self, text: str) -> str:
        return self._wrap(_DIM, text)

    def stopped(self, text: str) -> str:
        return self._wrap(_AMBER, text)

    def preserved(self, text: str) -> str:
        return self._wrap(_GREEN, text)


def field(style: Style, label: str, value: str, *, width: int = 14) -> str:
    """`  Label          value` — a dim label in a fixed column, value plain.

    The label is padded before colouring: escape sequences have no width, so
    padding a coloured string misaligns every row.
    """

    return f"{INDENT}{style.label(label.ljust(width))}{value}"


def arrow_path(nodes: list[str]) -> str:
    """`A → B → C`. A path, not a badge."""

    return " → ".join(node for node in nodes if node)
