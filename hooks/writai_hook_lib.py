"""Shared implementation for the writ.ai Claude Code hooks.

Standard library only, and deliberately conservative about syntax: this file runs
inside the *developer's* Python, not ours, so it must work on anything from 3.9 up
and must never import a third-party package.

Two invariants dominate everything below.

1. **Privacy.** The PreToolUse request body carries exactly ``session_id``,
   ``tool_name`` and ``timestamp`` — never tool input, file contents, a
   transcript path, the permission mode, or the cwd. ``check_request_body`` is
   the single place that body is built, so there is one place to audit.
   SessionStart is the one documented exception: it sends ``cwd`` and ``branch``
   once, because the service resolves the binding by reading ``.writai/task``
   from that directory itself. Marker-file *contents* are never transmitted.
2. **Claude Code hooks fail OPEN.** A hook that times out, crashes, or returns
   nothing lets the tool call proceed. So every failure path in this module ends
   in ``deny``: an unreachable supervisor, an unparseable response, a bug in this
   file. The enforcement gap this cannot close is documented in ``README.md``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO
from urllib.parse import quote

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

HOOK_EVENT_PRE_TOOL_USE = "PreToolUse"
HOOK_EVENT_SESSION_START = "SessionStart"

DEFAULT_ENDPOINT = "http://localhost:8002/supervisor/sessions"
DEFAULT_TIMEOUT_SECONDS = 3.0
#: The command timeout the shipped settings configure for every hook event.
#: `settings.example.json` and `managed-settings.example.json` both say 5.
COMMAND_TIMEOUT_SECONDS = 5.0

#: Hard ceiling on the HTTP wait, and it must stay strictly BELOW
#: ``COMMAND_TIMEOUT_SECONDS``. A process killed before it writes has, in
#: effect, allowed the call, so a request that can outlast the command deadline
#: is a fail-open dressed as a timeout — the cross-model review found this set
#: to 10 against a 5-second deadline. One second of headroom is left for
#: interpreter start-up and for writing the verdict. A configured value above
#: this is clamped rather than honoured.
MAX_TIMEOUT_SECONDS = 4.0
DEFAULT_CACHE_PATH = ".writai/hook-verdict-cache.json"

ENV_ENDPOINT = "WRITAI_HOOK_ENDPOINT"
ENV_TIMEOUT = "WRITAI_HOOK_TIMEOUT_SECONDS"
ENV_CACHE_PATH = "WRITAI_HOOK_CACHE_PATH"
ENV_API_KEY = "WRITAI_HOOK_API_KEY"
HOOK_API_KEY_HEADER = "X-writ.ai-Hook-API-Key"
ENV_NOTIFY = "WRITAI_HOOK_NOTIFY"

#: Claude Code truncates ``additionalContext`` past 10,000 characters. We stay under it
#: ourselves so the truncation is deterministic and is *announced* to the model.
#:
#: The budget is 9,000 rather than 10,000 to reserve room for the enclosing hook
#: JSON and the one-line reason, so the *complete stdout payload* stays under
#: 10,000 — not only this one field. The measured worst case is 8,235.
MAX_ADDITIONAL_CONTEXT_CHARS = 9_000

#: Ceiling for the whole serialized hook output, asserted by the tests.
MAX_HOOK_PAYLOAD_CHARS = 10_000
TRUNCATION_NOTICE = "\n\n[writai: context truncated to fit the 10000-character hook budget]"

#: Per-field budgets, applied before the global truncation backstop.
MAX_REASON_CHARS = 400
MAX_REDIRECT_CHARS = 6_000
MAX_EVIDENCE_CHARS = 400
MAX_PROVENANCE_NODES = 12
MAX_PROVENANCE_NODE_CHARS = 120
FIELD_TRUNCATION_MARKER = " …[truncated]"

#: Keep the on-disk cache from growing without bound in a long-lived checkout.
MAX_CACHED_SESSIONS = 200
CACHE_VERSION = 1

#: A verdict is a few kilobytes. Reading an unbounded body into memory inside a
#: hook that has to finish within the command timeout is a denial-of-service on
#: our own enforcement point, so the read is capped and an oversized body is a
#: transport error — which denies.
MAX_RESPONSE_BYTES = 1_000_000

#: The cache holds verdict text, so it stays readable only by its owner. The
#: API key is never written to it (see ``write_cached_verdict``), but the
#: provenance path names internal tasks and decisions.
CACHE_FILE_MODE = 0o600
CACHE_DIR_MODE = 0o700

NOTIFY_TITLE = "writ.ai blocked a tool call"
#: Subprocess budgets, both well inside ``COMMAND_TIMEOUT_SECONDS``. The
#: notification fires only AFTER the verdict is on stdout, so a slow one cannot
#: suppress a deny — but the whole process still has to finish before Claude
#: Code kills it, and `git` runs during SessionStart *before* registration.
NOTIFY_TIMEOUT_SECONDS = 2
GIT_TIMEOUT_SECONDS = 2

ALLOW = "allow"
DENY = "deny"

REASON_UNREACHABLE_NO_CACHE = (
    "writ.ai supervisor unreachable and no cached verdict for this session — failing closed."
)
REASON_INTERNAL_ERROR = (
    "The writ.ai enforcement hook failed; failing closed rather than silently allowing."
)
REASON_NO_SESSION_ID = (
    "writ.ai could not identify this session from the hook event — failing closed."
)
REASON_ALLOW_DEFAULT = "writ.ai: session is authorized for its current assignment."


class HookTransportError(RuntimeError):
    """Any failure reaching or understanding the supervisor. Always ends in ``deny``."""


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HookConfig:
    """Hook configuration, read from the environment. See ``.env.example``."""

    endpoint: str = DEFAULT_ENDPOINT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    cache_path: Path = Path(DEFAULT_CACHE_PATH)
    api_key: str = ""
    notifications_enabled: bool = True

    @classmethod
    def from_env(
        cls,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> "HookConfig":
        source = os.environ if env is None else env
        endpoint = (source.get(ENV_ENDPOINT) or DEFAULT_ENDPOINT).strip().rstrip("/")
        # A non-HTTP scheme is not a supervisor. `file://` in particular would
        # make urllib read a local path and hand back whatever it found as a
        # verdict, so an unusable endpoint falls back to the default rather than
        # being honoured.
        if not endpoint.startswith(("http://", "https://")):
            endpoint = DEFAULT_ENDPOINT

        # A long timeout is indistinguishable from no enforcement, because the tool call
        # proceeds while we wait. Anything unparseable or non-positive falls back to 3s.
        timeout = DEFAULT_TIMEOUT_SECONDS
        raw_timeout = (source.get(ENV_TIMEOUT) or "").strip()
        if raw_timeout:
            try:
                parsed = float(raw_timeout)
            except (TypeError, ValueError):
                parsed = 0.0
            if parsed > 0:
                timeout = min(parsed, MAX_TIMEOUT_SECONDS)

        raw_cache = (source.get(ENV_CACHE_PATH) or DEFAULT_CACHE_PATH).strip()
        if not raw_cache:
            raw_cache = DEFAULT_CACHE_PATH
        cache_path = Path(raw_cache).expanduser()
        if not cache_path.is_absolute():
            cache_path = Path(cwd or os.getcwd()) / cache_path

        return cls(
            endpoint=endpoint,
            timeout_seconds=timeout,
            cache_path=cache_path,
            api_key=(source.get(ENV_API_KEY) or "").strip(),
            notifications_enabled=_env_flag(source.get(ENV_NOTIFY), default=True),
        )

    # Routes -----------------------------------------------------------------------
    def register_url(self, session_id: str) -> str:
        # Lane B's router registers at a collection route; the session id is in
        # the body, not the path.
        del session_id
        return f"{self.endpoint}/start"

    def check_url(self, session_id: str) -> str:
        return f"{self.endpoint}/{quote(session_id, safe='')}/check"

    def session_url(self, session_id: str) -> str:
        return f"{self.endpoint}/{quote(session_id, safe='')}/end"


def _env_flag(raw: Optional[str], default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("0", "false", "no", "off", ""):
        return False
    return True


# --------------------------------------------------------------------------------------
# stdin / stdout
# --------------------------------------------------------------------------------------


def read_event(stream: Optional[TextIO] = None) -> Dict[str, Any]:
    """Parse the hook event JSON from stdin. Malformed input is an empty event."""

    source = sys.stdin if stream is None else stream
    try:
        raw = source.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def emit_json(payload: Dict[str, Any], stream: Optional[TextIO] = None) -> bool:
    """Write the hook result. Returns False if the verdict could not be delivered.

    A swallowed write failure is indistinguishable from a hook that chose to
    allow, so the caller needs to know: the entry scripts turn False into exit
    code 2, which blocks the call without needing stdout at all.
    """

    target = sys.stdout if stream is None else stream
    try:
        target.write(json.dumps(payload))
        target.flush()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------------------

#: ``(method, url, body_or_None, config) -> parsed_json_object``
Transport = Callable[[str, str, Optional[Dict[str, Any]], HookConfig], Dict[str, Any]]


def http_transport(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]],
    config: HookConfig,
) -> Dict[str, Any]:
    """The real transport. stdlib ``urllib`` with an explicit, short timeout."""

    data = None
    headers = {"Accept": "application/json", "User-Agent": "writai-hook/1"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if config.api_key:
        # The service authenticates the hook on this header specifically, and
        # fails closed (503) when no key is configured on either side.
        headers[HOOK_API_KEY_HEADER] = config.api_key

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise HookTransportError("supervisor response is too large")
        raw = body.decode("utf-8", "replace")
    except HookTransportError:
        raise
    except Exception as exc:  # URLError, HTTPError, socket.timeout, ssl errors, …
        raise HookTransportError(f"{method} {url} failed: {exc}") from exc

    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise HookTransportError("supervisor returned non-JSON") from exc
    if not isinstance(parsed, dict):
        raise HookTransportError("supervisor returned a non-object body")
    return parsed


# --------------------------------------------------------------------------------------
# On-disk verdict cache
# --------------------------------------------------------------------------------------


def read_cache(config: HookConfig) -> Dict[str, Any]:
    try:
        with open(str(config.cache_path), encoding="utf-8") as handle:
            parsed = json.load(handle)
    except Exception:
        return {"version": CACHE_VERSION, "sessions": {}}
    if not isinstance(parsed, dict):
        return {"version": CACHE_VERSION, "sessions": {}}
    if not isinstance(parsed.get("sessions"), dict):
        parsed["sessions"] = {}
    return parsed


def read_cached_verdict(config: HookConfig, session_id: str) -> Optional[Dict[str, Any]]:
    """The last verdict the supervisor gave this session, or ``None``."""

    try:
        entry = read_cache(config)["sessions"].get(session_id)
        if isinstance(entry, dict) and isinstance(entry.get("verdict"), dict):
            return dict(entry["verdict"])
    except Exception:
        return None
    return None


def write_cached_verdict(config: HookConfig, session_id: str, verdict: Dict[str, Any]) -> None:
    """Persist a server verdict. A cache write failure must never change a decision."""

    try:
        cache = read_cache(config)
        sessions = cache["sessions"]
        sessions[session_id] = {"cached_at": utc_now_iso(), "verdict": verdict}
        if len(sessions) > MAX_CACHED_SESSIONS:
            ordered = sorted(
                sessions.items(),
                key=lambda item: str((item[1] or {}).get("cached_at", "")),
                reverse=True,
            )
            cache["sessions"] = dict(ordered[:MAX_CACHED_SESSIONS])
        cache["version"] = CACHE_VERSION
        _atomic_write_json(config.cache_path, cache)
    except Exception:
        pass


def read_delivered_redirect_id(config: HookConfig, session_id: str) -> Optional[str]:
    """The redirect id this session last DELIVERED, or ``None``.

    Recorded only after the deny is actually on stdout, so a hook killed between
    receiving a verdict and emitting it acknowledges nothing — and the service
    re-delivers rather than advancing past an interrupt nobody saw.
    """

    try:
        entry = read_cache(config)["sessions"].get(session_id)
        if isinstance(entry, dict):
            value = entry.get("delivered_redirect_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception:
        return None
    return None


def record_delivered_redirect_id(
    config: HookConfig,
    session_id: str,
    redirect_id: Optional[str],
) -> None:
    """Note that a redirect reached the agent. Called AFTER the verdict is written.

    A failure here costs one repeated redirect on the next tool call, never a
    missed one: without the acknowledgement the service keeps the interrupt open.
    """

    try:
        cache = read_cache(config)
        entry = cache["sessions"].get(session_id)
        if not isinstance(entry, dict):
            entry = {"cached_at": utc_now_iso(), "verdict": {}}
        if redirect_id:
            entry["delivered_redirect_id"] = redirect_id
        else:
            entry.pop("delivered_redirect_id", None)
        cache["sessions"][session_id] = entry
        cache["version"] = CACHE_VERSION
        _atomic_write_json(config.cache_path, cache)
    except Exception:
        pass


def forget_cached_verdict(config: HookConfig, session_id: str) -> None:
    try:
        cache = read_cache(config)
        if session_id in cache["sessions"]:
            del cache["sessions"][session_id]
            _atomic_write_json(config.cache_path, cache)
    except Exception:
        pass


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=CACHE_DIR_MODE)
    temporary = path.with_name(path.name + ".tmp")
    with open(str(temporary), "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    # Narrow the temporary file before it becomes the cache, so the verdict text
    # is never briefly world-readable under a permissive umask.
    os.chmod(str(temporary), CACHE_FILE_MODE)
    os.replace(str(temporary), str(path))
    os.chmod(str(path), CACHE_FILE_MODE)


# --------------------------------------------------------------------------------------
# Verdict rendering
# --------------------------------------------------------------------------------------


def utc_now_iso(now: Optional[datetime] = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def check_request_body(
    tool_name: str,
    now: Optional[datetime] = None,
    session_id: str = "",
    acknowledged_redirect_id: Optional[str] = None,
) -> Dict[str, Any]:
    """THE privacy boundary. Only these keys leave this machine — audit here.

    Never add ``tool_input``, ``cwd``, ``transcript_path``, ``permission_mode`` or
    anything derived from file contents. ``session_id`` travels in the URL path.

    ``acknowledged_redirect_id`` is not developer data either: it is an
    identifier the SERVICE issued on a previous deny, echoed back to confirm the
    redirect actually reached the agent. The hook can only ever send a value it
    was given, and it is what lets the service hold an interrupt open until
    delivery is confirmed instead of advancing on hope.
    """

    body: Dict[str, Any] = {"tool_name": tool_name, "timestamp": utc_now_iso(now)}
    if session_id:
        # The service requires the body id to match the path id. It is an
        # identifier, not content: still no tool input, file contents or
        # transcript data leaves this machine.
        body["session_id"] = session_id
    if acknowledged_redirect_id:
        body["acknowledged_redirect_id"] = acknowledged_redirect_id
    return body


def one_line(text: Any, limit: int = MAX_REASON_CHARS) -> str:
    """Collapse to a single line and cap it. ``permissionDecisionReason`` is one line."""

    value = "" if text is None else str(text)
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - len(FIELD_TRUNCATION_MARKER))] + FIELD_TRUNCATION_MARKER


def _cap(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - len(FIELD_TRUNCATION_MARKER))] + FIELD_TRUNCATION_MARKER


def compact_provenance(path: Any) -> str:
    """``DEC-018 → DEC-004 → SPEC-009 → TASK-102`` — a path, not a badge."""

    if not isinstance(path, (list, tuple)):
        return ""
    nodes: List[str] = [one_line(node, MAX_PROVENANCE_NODE_CHARS) for node in path if node]
    if not nodes:
        return ""
    if len(nodes) <= MAX_PROVENANCE_NODES:
        return " → ".join(nodes)
    head = nodes[: MAX_PROVENANCE_NODES - 5]
    tail = nodes[-5:]
    hidden = len(nodes) - len(head) - len(tail)
    return " → ".join(head + [f"…({hidden} more truncated)…"] + tail)


def truncate_context(text: str) -> str:
    """Deterministic global backstop, and it says that it truncated."""

    if len(text) <= MAX_ADDITIONAL_CONTEXT_CHARS:
        return text
    keep = MAX_ADDITIONAL_CONTEXT_CHARS - len(TRUNCATION_NOTICE)
    return text[:keep] + TRUNCATION_NOTICE


#: `docs/TERMINAL_OUTPUT_SPEC.md` §1. Two-space indent, one idea per line.
_LEADER_STOPPED = "⏹"
_INDENT = "  "
_BODY = "     "
_LABEL_WIDTH = 14


def _narrative_list(verdict: Dict[str, Any], key: str) -> List[str]:
    narrative = verdict.get("narrative")
    if not isinstance(narrative, dict):
        return []
    values = narrative.get(key)
    if not isinstance(values, list):
        return []
    return [one_line(value, 80) for value in values if value]


def _narrative_text(verdict: Dict[str, Any], key: str, limit: int) -> str:
    narrative = verdict.get("narrative")
    if not isinstance(narrative, dict):
        return ""
    return one_line(narrative.get(key), limit)


def _attribution(verdict: Dict[str, Any]) -> str:
    """`Approved by <role> <when>:` — omitted entirely when unknown.

    Never invent an approver or a time. A fabricated name on the most-read text
    in the product is worse than a missing line.
    """

    who = _narrative_text(verdict, "decided_by", 80)
    when = _narrative_text(verdict, "decided_at", 40)
    if not who and not when:
        return ""
    if who and when:
        return f"Approved by {who} · {when}:"
    return f"Approved by {who}:" if who else f"Approved {when}:"


def build_additional_context(verdict: Dict[str, Any], stale: bool = False) -> str:
    """The deny block, per `docs/TERMINAL_OUTPUT_SPEC.md` §1.

    Read by the developer *and* by the model, so it is plain text: ANSI escapes
    would be noise in the model's context. The terminal-facing copy is coloured
    separately in `render_deny_block`.

    Ordering is load-bearing. **Still valid comes before what stopped** — leading
    with what survived is the difference between an agent that adapts and one
    that starts over.
    """

    lines: List[str] = [
        f"{_INDENT}{_LEADER_STOPPED}  WRITAI — the decision behind this task changed"
    ]
    if stale:
        lines += [
            "",
            f"{_BODY}(Supervisor unreachable — last cached verdict for this session.)",
        ]

    attribution = _attribution(verdict)
    quoted = _narrative_text(verdict, "decision_text", MAX_REASON_CHARS)
    if attribution or quoted:
        lines.append("")
        if attribution:
            lines.append(f"{_BODY}{attribution}")
        if quoted:
            lines.append(f'{_BODY}"{quoted}"')

    still_valid = _narrative_list(verdict, "still_valid")
    no_longer = _narrative_list(verdict, "no_longer")
    now_required = _narrative_list(verdict, "now_required")
    if not now_required:
        redirect = _cap(verdict.get("redirect_instruction"), MAX_REDIRECT_CHARS).strip()
        if redirect:
            now_required = [one_line(redirect, MAX_REDIRECT_CHARS)]

    rows = [
        ("Still valid", still_valid),
        ("No longer", no_longer),
        ("Now required", now_required),
    ]
    if any(values for _label, values in rows):
        lines.append("")
        for label, values in rows:
            if values:
                lines.append(f"{_BODY}{label.ljust(_LABEL_WIDTH)}{', '.join(values)}")

    reason = one_line(verdict.get("reason"), MAX_REASON_CHARS)
    provenance = compact_provenance(verdict.get("provenance_path"))
    if provenance or reason:
        lines.append("")
        if provenance:
            lines.append(f"{_BODY}{'Why'.ljust(5)}{provenance} → this session")
        elif reason:
            lines.append(f"{_BODY}{'Why'.ljust(5)}{reason}")
        lines.append(f"{_BODY}{''.ljust(5)}writai dev why")

    evidence = one_line(verdict.get("evidence_ref"), MAX_EVIDENCE_CHARS)
    if evidence:
        lines.append(f"{_BODY}{''.ljust(5)}{evidence}")

    lines += [
        "",
        f"{_BODY}Do not retry the blocked call unchanged and do not work around this "
        "hook. Re-plan against the line above, then continue.",
    ]
    return truncate_context("\n".join(lines))


def decision_output(
    decision: str,
    reason: str,
    additional_context: str = "",
) -> Dict[str, Any]:
    """The exact PreToolUse output shape. ``allow`` or ``deny`` only — never ``ask``."""

    normalized = ALLOW if decision == ALLOW else DENY
    return {
        "hookSpecificOutput": {
            "hookEventName": HOOK_EVENT_PRE_TOOL_USE,
            "permissionDecision": normalized,
            "permissionDecisionReason": one_line(reason) or "writ.ai denied this tool call.",
            "additionalContext": truncate_context(additional_context),
        }
    }


def deny_output(reason: str, additional_context: str = "") -> Dict[str, Any]:
    return decision_output(DENY, reason, additional_context)


def is_denied(output: Dict[str, Any]) -> bool:
    try:
        return bool(output["hookSpecificOutput"]["permissionDecision"] == DENY)
    except Exception:
        return True


def decision_reason(output: Dict[str, Any]) -> str:
    try:
        return str(output["hookSpecificOutput"]["permissionDecisionReason"])
    except Exception:
        return REASON_INTERNAL_ERROR


def render_verdict(verdict: Dict[str, Any], stale: bool = False) -> Dict[str, Any]:
    """Server verdict -> hook output. Anything that is not exactly ``allow`` denies."""

    decision = verdict.get("decision")
    reason = one_line(verdict.get("reason"))
    if decision == ALLOW:
        context = ""
        if stale:
            context = (
                "writ.ai supervisor unreachable; reusing the last cached verdict "
                "for this session, which was allow."
            )
            reason = reason or "writ.ai: cached verdict for this session is allow."
        return decision_output(ALLOW, reason or REASON_ALLOW_DEFAULT, context)

    if decision != DENY:
        reason = f"writ.ai returned an unrecognized verdict ({decision!r}) — failing closed."
    elif stale:
        cached_reason = one_line(verdict.get("reason"), 200)
        reason = (
            "writ.ai supervisor unreachable; last cached verdict for this session denies. "
            + cached_reason
        )
    elif not reason:
        reason = "writ.ai: this session's authorization was revoked."
    return deny_output(reason, build_additional_context(verdict, stale=stale))


# --------------------------------------------------------------------------------------
# PreToolUse
# --------------------------------------------------------------------------------------


def build_pre_tool_use_output(
    event: Dict[str, Any],
    config: Optional[HookConfig] = None,
    transport: Optional[Transport] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Decide. Every failure inside this function ends in ``deny``, never in a raise."""

    return build_pre_tool_use_result(event, config, transport, now)[0]


def build_pre_tool_use_result(
    event: Dict[str, Any],
    config: Optional[HookConfig] = None,
    transport: Optional[Transport] = None,
    now: Optional[datetime] = None,
) -> "tuple":
    """``(output, redirect_id)``.

    The second value is the redirect this verdict carries, and the entry script
    records it only AFTER the output is on stdout. That ordering is the whole
    point: acknowledging a redirect the agent never saw would let the service
    advance past an interrupt nobody received.
    """

    try:
        active_config = config or HookConfig.from_env(cwd=_event_cwd(event))
        send: Transport = transport or http_transport

        # Only real strings. Coercing a dict or list here would let a malformed
        # event smuggle tool input or a path into a field we transmit.
        raw_session = event.get("session_id")
        session_id = raw_session.strip() if isinstance(raw_session, str) else ""
        if not session_id or len(session_id) > 255:
            return deny_output(REASON_NO_SESSION_ID), None

        raw_tool = event.get("tool_name")
        tool_name = raw_tool.strip() if isinstance(raw_tool, str) else ""
        if not tool_name or len(tool_name) > 255:
            tool_name = "unknown"
        body = check_request_body(
            tool_name,
            now,
            session_id=session_id,
            acknowledged_redirect_id=read_delivered_redirect_id(
                active_config, session_id
            ),
        )

        try:
            verdict = send("POST", active_config.check_url(session_id), body, active_config)
        except Exception:
            # Transport failure degrades to this session's last known DENY, never
            # to an allow. Reusing a cached allow would mean an outage authorizes
            # work — the exact fail-open this hook exists to prevent. A cached
            # allow, a cache miss and a corrupt cache are all denies.
            cached = read_cached_verdict(active_config, session_id)
            if cached is None or not _is_deny(cached):
                return deny_output(REASON_UNREACHABLE_NO_CACHE), None
            # A replayed cached deny is NOT an acknowledgement: the service
            # never saw this call, so its interrupt must stay open.
            return render_verdict(cached, stale=True), None

        if not _is_well_formed(verdict):
            return deny_output(
                "writ.ai supervisor returned an unusable response — failing closed."
            ), None
        write_cached_verdict(active_config, session_id, verdict)
        return render_verdict(verdict), _redirect_id(verdict)
    except Exception:
        return deny_output(REASON_INTERNAL_ERROR), None


def _redirect_id(verdict: Any) -> Optional[str]:
    """The service-issued id for the redirect this deny carries, if any."""

    if not isinstance(verdict, dict):
        return None
    value = verdict.get("redirect_id")
    if isinstance(value, str) and value.strip() and len(value) <= 255:
        return value.strip()
    return None


def _is_well_formed(verdict: Any) -> bool:
    """Exactly one of allow/deny, with a usable reason. Anything else denies."""

    if not isinstance(verdict, dict):
        return False
    decision = verdict.get("decision")
    if decision not in (ALLOW, DENY):
        return False
    reason = verdict.get("reason")
    # A verdict with no usable reason is not a verdict. The developer and the
    # model both read this string; an empty one is indistinguishable from the
    # service having said nothing.
    return isinstance(reason, str) and bool(reason.strip())


def _is_deny(verdict: Any) -> bool:
    return _is_well_formed(verdict) and verdict.get("decision") == DENY


def _event_cwd(event: Dict[str, Any]) -> Optional[str]:
    """The event's cwd locates the cache file on disk. It is never transmitted."""

    value = event.get("cwd")
    return value if isinstance(value, str) and value else None


# --------------------------------------------------------------------------------------
# SessionStart facts
# --------------------------------------------------------------------------------------

GitRunner = Callable[[Sequence[str], str], str]


def git_branch(cwd: str, runner: Optional[GitRunner] = None) -> Optional[str]:
    """``git rev-parse --abbrev-ref HEAD``. Any failure, or a detached HEAD, is ``None``."""

    try:
        run = runner or _run_git
        branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd).strip()
    except Exception:
        return None
    if not branch or branch == "HEAD":
        return None
    return branch


def _run_git(command: Sequence[str], cwd: str) -> str:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", "replace")


def read_marker_file(cwd: str, name: str) -> Optional[str]:
    """First non-empty line of ``.writai/<name>``, or ``None``."""

    try:
        with open(str(Path(cwd) / ".writai" / name), encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    return stripped
    except Exception:
        return None
    return None


def register_request_body(
    cwd: str,
    branch: Optional[str],
    session_id: str = "",
) -> Dict[str, Any]:
    """``ClaudeSessionStartRequest``: exactly session_id, cwd, branch.

    The service reads ``.writai/attach`` and ``.writai/task`` from the cwd
    itself, so the client reports only where it is — it never resolves a binding
    and never guesses a task. ``branch`` is a string, never null: the service
    model declares it ``str`` with a default of "".
    """

    body: Dict[str, Any] = {"cwd": cwd, "branch": (branch or "")}
    if session_id:
        body["session_id"] = session_id
    return body


def describe_binding(response: Dict[str, Any]) -> str:
    """One short paragraph for the SessionStart ``additionalContext``."""

    binding = response.get("binding")
    if not isinstance(binding, dict):
        return "writ.ai enforcement is active for this session."
    source = one_line(binding.get("source"), 60) or "unknown"
    task_id = one_line(binding.get("task_id"), 60)
    assignment_id = one_line(binding.get("assignment_id"), 80)
    if not assignment_id:
        return (
            "writ.ai enforcement is active. This session is registered but UNBOUND "
            "(no assignment matched), so tool calls are allowed and the session stays "
            "visible in `writai dev status`."
        )
    return (
        f"writ.ai enforcement is active. This session is bound to assignment "
        f"{assignment_id} (task {task_id or 'unknown'}, via {source}). If an approved "
        "upstream decision invalidates that assignment, the next tool call is denied "
        "with a redirect instruction."
    )


# --------------------------------------------------------------------------------------
# A6 — local desktop notification. Never changes a verdict, never raises.
# --------------------------------------------------------------------------------------


def notification_command(system: str, title: str, message: str) -> Optional[List[str]]:
    if system.startswith("darwin"):
        escaped_message = _applescript_escape(message)
        escaped_title = _applescript_escape(title)
        script = f'display notification "{escaped_message}" with title "{escaped_title}"'
        return ["osascript", "-e", script]
    if system.startswith("linux"):
        return ["notify-send", title, message]
    return None


def _applescript_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _default_notification_runner(command: Sequence[str]) -> None:
    subprocess.run(
        list(command),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=NOTIFY_TIMEOUT_SECONDS,
        check=False,
    )


def notify_denied(
    config: HookConfig,
    reason: str,
    runner: Optional[Callable[[Sequence[str]], None]] = None,
    system: Optional[str] = None,
) -> bool:
    """Fire a local desktop notification on deny. Returns whether one was sent.

    Swallows *everything*: a missing binary, a hung ``osascript``, an unsupported
    platform. Disable entirely with ``WRITAI_HOOK_NOTIFY=0``.
    """

    try:
        if not config.notifications_enabled:
            return False
        command = notification_command(system or sys.platform, NOTIFY_TITLE, one_line(reason, 200))
        if not command:
            return False
        if shutil.which(command[0]) is None:
            return False
        (runner or _default_notification_runner)(command)
        return True
    except Exception:
        return False
