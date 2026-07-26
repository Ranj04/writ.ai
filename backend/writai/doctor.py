"""`writai doctor` — one command that tells you which sponsor integrations are live.

The failure mode this exists to kill is a credential that is *set but dead*. An
environment variable being non-empty proves nothing, so every probe here that
can make a cheap, safe, read-only call **makes it**, and reports what the
service actually said.

Three questions per integration, always answered separately:

1. **Configured?** Are the variables this integration needs present.
2. **Credential valid?** Did a real call succeed. `UNVERIFIED` is its own answer
   and is never rendered as success — it means we could not check cheaply, not
   that it works.
3. **What is lost?** The capability that degrades when this is absent, named in
   product terms rather than as a stack trace.

Nothing here mutates anything. Every probe is a read: a list, a `GET`, a
`--version`. No message is posted, no call is placed, no watcher is created.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from writai.config import Settings
from writai.config import settings as default_settings

PROBE_TIMEOUT_SECONDS = 8.0


class ProbeStatus(StrEnum):
    """What we actually know, kept distinct from what we hope."""

    #: Configured, and a real call proved the credential works.
    LIVE = "live"
    #: Configured, but this probe could not cheaply prove the credential works.
    #: NOT a pass. Rendered differently and counted separately.
    UNVERIFIED = "unverified"
    #: Configured, and the service rejected us. A dead key set in the env is
    #: exactly this, and is the reason this command exists.
    INVALID = "invalid"
    #: Not configured. The product still runs; the named capability does not.
    ABSENT = "absent"


#: The statuses that mean "this capability is genuinely available right now".
WORKING = frozenset({ProbeStatus.LIVE})


@dataclass(frozen=True)
class ProbeResult:
    name: str
    status: ProbeStatus
    #: One line on what the probe observed. Never a raw traceback.
    detail: str
    #: What the product loses while this is not LIVE.
    degrades_to: str
    #: The variables this integration reads, for the fix-it hint.
    variables: tuple[str, ...] = ()
    #: Set when the integration is running on a fixture or a replay rather than
    #: the real service. Printed loudly: a replay must never read as live.
    replayed: bool = False
    replay_note: str = ""

    @property
    def ok(self) -> bool:
        return self.status in WORKING


@dataclass
class _Probe:
    name: str
    variables: tuple[str, ...]
    degrades_to: str
    run: Callable[[Settings], ProbeResult]
    replayed: bool = False
    replay_note: str = ""


def _result(
    probe: _Probe,
    status: ProbeStatus,
    detail: str,
) -> ProbeResult:
    return ProbeResult(
        name=probe.name,
        status=status,
        detail=detail,
        degrades_to=probe.degrades_to,
        variables=probe.variables,
        replayed=probe.replayed,
        replay_note=probe.replay_note,
    )


def _get(
    url: str,
    headers: dict[str, str],
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    """One read-only GET. Returns ``(status, body)``; never raises for HTTP codes."""

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return getattr(response, "status", 200), response.read(65_536).decode(
                "utf-8", "replace"
            )
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4_096).decode("utf-8", "replace")


def _post(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return getattr(response, "status", 200), response.read(65_536).decode(
                "utf-8", "replace"
            )
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4_096).decode("utf-8", "replace")


# --------------------------------------------------------------------------------------
# Probes. Each one is read-only.
# --------------------------------------------------------------------------------------


def _probe_gemini(settings: Settings) -> ProbeResult:
    probe = PROBES["gemini"]
    if not settings.gemini_api_key:
        return _result(
            probe,
            ProbeStatus.ABSENT,
            "GEMINI_API_KEY is unset.",
        )
    # `GET /models` is the cheapest authenticated read Gemini exposes. It does
    # not consume generation quota and returns nothing sensitive.
    url = f"{settings.gemini_base_url.rstrip('/')}/models"
    try:
        status, body = _get(url, {"x-goog-api-key": settings.gemini_api_key})
    except Exception as exc:  # noqa: BLE001 - network shapes vary; report, never raise
        return _result(probe, ProbeStatus.UNVERIFIED, f"could not reach Gemini: {exc}")
    if status == 200:
        try:
            models = json.loads(body).get("models", [])
            names = {str(item.get("name", "")).split("/")[-1] for item in models}
        except Exception:  # noqa: BLE001
            names = set()
        configured = settings.gemini_model
        if names and configured not in names:
            return _result(
                probe,
                ProbeStatus.INVALID,
                f"key works, but GEMINI_MODEL={configured!r} is not in the "
                f"{len(names)} models this key can see.",
            )
        return _result(
            probe,
            ProbeStatus.LIVE,
            f"key accepted; {len(names) or 'some'} models visible, "
            f"including {configured}.",
        )
    if status in (401, 403):
        return _result(probe, ProbeStatus.INVALID, f"key rejected (HTTP {status}).")
    return _result(probe, ProbeStatus.UNVERIFIED, f"unexpected HTTP {status}.")


def _probe_hexclave(settings: Settings) -> ProbeResult:
    probe = PROBES["hexclave"]
    if not settings.hexclave_project_id or not settings.hexclave_secret_key:
        missing = [
            name
            for name, value in (
                ("HEXCLAVE_PROJECT_ID", settings.hexclave_project_id),
                ("HEXCLAVE_SECRET_SERVER_KEY", settings.hexclave_secret_key),
            )
            if not value
        ]
        return _result(probe, ProbeStatus.ABSENT, f"unset: {', '.join(missing)}.")

    # The real contract, taken from HttpxHexclaveTransport rather than guessed:
    # access type, project id and secret key all travel as X-Hexclave-* headers.
    # A project-scoped /projects/{id}/teams path does not exist and answers 404,
    # which an earlier version of this probe mis-reported as "project not found".
    base = settings.hexclave_api_url.rstrip("/")
    headers = {
        "Accept": "application/json",
        "X-Hexclave-Access-Type": "server",
        "X-Hexclave-Project-Id": settings.hexclave_project_id,
        "X-Hexclave-Secret-Server-Key": settings.hexclave_secret_key,
    }
    try:
        status, body = _get(f"{base}/teams", headers)
    except Exception as exc:  # noqa: BLE001
        return _result(probe, ProbeStatus.UNVERIFIED, f"could not reach Hexclave: {exc}")
    if status in (401, 403):
        return _result(
            probe, ProbeStatus.INVALID, f"secret server key rejected (HTTP {status})."
        )
    if status != 200:
        return _result(probe, ProbeStatus.UNVERIFIED, f"unexpected HTTP {status}.")

    try:
        items = json.loads(body).get("items", [])
        ids = {
            str(team.get("id"))
            for team in items
            if isinstance(team, dict) and team.get("id")
        }
    except Exception:  # noqa: BLE001
        return _result(
            probe,
            ProbeStatus.UNVERIFIED,
            "key accepted, but the team list could not be parsed.",
        )

    if not ids:
        # The distinction that matters: the credential is GOOD and the blocker is
        # provisioning. Reporting this as a bad key would send someone hunting
        # for the wrong problem.
        return _result(
            probe,
            ProbeStatus.INVALID,
            "secret key is VALID and the project resolves, but it contains ZERO "
            "teams — so no valid HEXCLAVE_TEAM_ID can exist yet. Create a team "
            "in the Hexclave dashboard, then paste its id.",
        )
    if not settings.hexclave_team_id:
        return _result(
            probe,
            ProbeStatus.INVALID,
            f"secret key is valid and {len(ids)} team(s) exist, but "
            f"HEXCLAVE_TEAM_ID is unset. Paste one of: {', '.join(sorted(ids))}",
        )
    if settings.hexclave_team_id not in ids:
        return _result(
            probe,
            ProbeStatus.INVALID,
            f"HEXCLAVE_TEAM_ID is not one of this project's {len(ids)} team(s). "
            f"Available: {', '.join(sorted(ids))}",
        )
    return _result(
        probe,
        ProbeStatus.LIVE,
        f"secret key accepted and the team resolves "
        f"({len(ids)} team(s) in project).",
    )


def _probe_composio(settings: Settings) -> ProbeResult:
    probe = PROBES["composio"]
    missing = [
        name
        for name, value in (
            ("COMPOSIO_API_KEY", settings.composio_api_key),
            ("COMPOSIO_WEBHOOK_SECRET", settings.composio_webhook_secret),
        )
        if not value
    ]
    if not settings.composio_api_key:
        return _result(probe, ProbeStatus.ABSENT, f"unset: {', '.join(missing)}.")
    # v3. The v1/v2 `apps` endpoints answer 410 Gone, which this probe used to
    # report as "unverified" — a live key looking broken. Listing auth configs
    # verifies the key AND tells us whether Slack is actually connected, which
    # is the thing that decides if the loop can run.
    try:
        status, body = _get(
            f"{COMPOSIO_API_BASE}/auth_configs",
            {"x-api-key": settings.composio_api_key, "Accept": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001
        return _result(probe, ProbeStatus.UNVERIFIED, f"could not reach Composio: {exc}")
    if status in (401, 403):
        return _result(probe, ProbeStatus.INVALID, f"API key rejected (HTTP {status}).")
    if status != 200:
        return _result(probe, ProbeStatus.UNVERIFIED, f"unexpected HTTP {status}.")

    slack_configs = _slack_auth_configs(body)
    configured = (os.environ.get("COMPOSIO_SLACK_AUTH_CONFIG_ID") or "").strip()
    channel = (os.environ.get("WRITAI_SLACK_CHANNEL_ID") or "").strip()

    if not slack_configs:
        return _result(
            probe,
            ProbeStatus.INVALID,
            "API key accepted, but this account has NO enabled Slack auth "
            "config. Connect Slack in the Composio dashboard first.",
        )
    if not configured:
        # Turn-key: do not just say "unset", say the value to paste.
        found = ", ".join(sorted(slack_configs))
        return _result(
            probe,
            ProbeStatus.INVALID,
            f"API key accepted and Slack IS connected, but "
            f"COMPOSIO_SLACK_AUTH_CONFIG_ID is unset. Paste this: {found}",
        )
    if configured not in slack_configs:
        return _result(
            probe,
            ProbeStatus.INVALID,
            f"COMPOSIO_SLACK_AUTH_CONFIG_ID={configured} is not an enabled Slack "
            f"auth config on this account. Available: {', '.join(sorted(slack_configs))}",
        )
    still_missing = [name for name in missing]
    if not channel:
        still_missing.append("WRITAI_SLACK_CHANNEL_ID")
    if still_missing:
        return _result(
            probe,
            ProbeStatus.INVALID,
            "key and Slack auth config both good, but "
            + ", ".join(still_missing)
            + " unset — inbound deliveries cannot be verified or routed.",
        )
    return _result(
        probe,
        ProbeStatus.LIVE,
        f"key accepted, Slack auth config {configured} enabled, "
        f"signing secret set, channel {channel}.",
    )


COMPOSIO_API_BASE = "https://backend.composio.dev/api/v3"


def _slack_auth_configs(body: str) -> set[str]:
    """Ids of ENABLED Slack auth configs. Empty on any shape we do not recognise."""

    try:
        items = json.loads(body).get("items", [])
    except Exception:  # noqa: BLE001
        return set()
    found: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        toolkit = item.get("toolkit")
        slug = toolkit.get("slug") if isinstance(toolkit, dict) else None
        if str(slug).lower() != "slack":
            continue
        if str(item.get("status", "")).upper() not in {"ENABLED", "ACTIVE"}:
            continue
        identifier = item.get("id")
        if isinstance(identifier, str) and identifier.strip():
            found.add(identifier.strip())
    return found


def _probe_callwright(settings: Settings) -> ProbeResult:
    probe = PROBES["callwright"]
    if not settings.callwright_api_key:
        return _result(probe, ProbeStatus.ABSENT, "CALLWRIGHT_API_KEY is unset.")
    base = settings.callwright_base_url.rstrip("/")
    try:
        # Read-only. This probe must never place a call.
        status, _body = _get(
            f"{base}/v1/calls?limit=1",
            {
                "Authorization": f"Bearer {settings.callwright_api_key}",
                "Accept": "application/json",
            },
        )
    except Exception as exc:  # noqa: BLE001
        return _result(
            probe, ProbeStatus.UNVERIFIED, f"could not reach Callwright: {exc}"
        )
    if status in (401, 403):
        return _result(probe, ProbeStatus.INVALID, f"API key rejected (HTTP {status}).")
    if status >= 500:
        return _result(probe, ProbeStatus.UNVERIFIED, f"service error HTTP {status}.")
    if status >= 400:
        return _result(
            probe,
            ProbeStatus.UNVERIFIED,
            f"key not rejected, but the read returned HTTP {status}.",
        )
    live = settings.callwright_live_calls_enabled
    return _result(
        probe,
        ProbeStatus.LIVE,
        "API key accepted. Live calls are "
        + ("ENABLED." if live else "disabled (CALLWRIGHT_LIVE_CALLS_ENABLED=false)."),
    )


def _probe_crustdata(settings: Settings) -> ProbeResult:
    probe = PROBES["crustdata"]
    if not settings.crustdata_api_key:
        return _result(
            probe,
            ProbeStatus.ABSENT,
            "CRUSTDATA_API_KEY is unset; the watcher path runs on a replayed fixture.",
        )
    try:
        status, _body = _get(
            "https://api.crustdata.com/screener/company",
            {
                "Authorization": f"Token {settings.crustdata_api_key}",
                "Accept": "application/json",
            },
        )
    except Exception as exc:  # noqa: BLE001
        return _result(
            probe, ProbeStatus.UNVERIFIED, f"could not reach CrustData: {exc}"
        )
    if status in (401, 403):
        return _result(probe, ProbeStatus.INVALID, f"API key rejected (HTTP {status}).")
    if status >= 500:
        return _result(probe, ProbeStatus.UNVERIFIED, f"service error HTTP {status}.")
    return _result(
        probe,
        ProbeStatus.LIVE,
        "API key accepted. The watcher still cannot fire inside a demo "
        "(one-hour minimum interval), so the flag path stays a labelled replay.",
    )


def _probe_superset(settings: Settings) -> ProbeResult:
    probe = PROBES["superset"]
    binary = shutil.which("superset")
    project = os.environ.get("WRITAI_DEMO_SUPERSET_PROJECT", "").strip()
    if binary is None:
        return _result(probe, ProbeStatus.ABSENT, "`superset` is not on PATH.")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return _result(probe, ProbeStatus.UNVERIFIED, f"`superset --version` failed: {exc}")
    if completed.returncode != 0:
        return _result(
            probe,
            ProbeStatus.UNVERIFIED,
            "`superset` is on PATH but `--version` exited non-zero.",
        )
    version = (completed.stdout or completed.stderr).strip().splitlines()[:1]
    version_text = version[0] if version else "unknown version"
    if not project:
        return _result(
            probe,
            ProbeStatus.INVALID,
            f"{version_text} installed, but WRITAI_DEMO_SUPERSET_PROJECT is unset — "
            "`workspaces create` has no --project and cannot provision.",
        )
    return _result(
        probe,
        ProbeStatus.LIVE,
        f"{version_text}; project {project} configured.",
    )


PROBES: dict[str, _Probe] = {
    "gemini": _Probe(
        name="Gemini",
        variables=("GEMINI_API_KEY", "GEMINI_MODEL"),
        degrades_to=(
            "Slack messages cannot be turned into decision proposals. "
            "`--scope/--was/--now` still works and needs no model."
        ),
        run=_probe_gemini,
    ),
    "hexclave": _Probe(
        name="Hexclave",
        variables=(
            "HEXCLAVE_PROJECT_ID",
            "HEXCLAVE_SECRET_SERVER_KEY",
            "HEXCLAVE_TEAM_ID",
        ),
        degrades_to=(
            "No approval can resolve a real person, so every authenticated "
            "approval path fails closed. The demo falls back to the gated "
            "in-process seam, which bypasses channel auth and no authority check."
        ),
        run=_probe_hexclave,
    ),
    "composio": _Probe(
        name="Composio",
        variables=(
            "COMPOSIO_API_KEY",
            "COMPOSIO_WEBHOOK_SECRET",
            "COMPOSIO_SLACK_AUTH_CONFIG_ID",
            "WRITAI_SLACK_CHANNEL_ID",
        ),
        degrades_to=(
            "The Slack loop is shut in both directions: no decision is ingested "
            "from a channel, no approval card is posted back, and no reaction "
            "can approve. Message text must be supplied by hand."
        ),
        run=_probe_composio,
    ),
    "callwright": _Probe(
        name="Callwright",
        variables=("CALLWRIGHT_API_KEY", "CALLWRIGHT_LIVE_CALLS_ENABLED"),
        degrades_to=(
            "An unacknowledged interrupt cannot escalate to a phone call. "
            "The fixture client records the attempt instead."
        ),
        run=_probe_callwright,
    ),
    "crustdata": _Probe(
        name="CrustData",
        variables=("CRUSTDATA_API_KEY", "CRUSTDATA_WEBHOOK_BEARER"),
        degrades_to=(
            "Nothing observes an approver changing role or leaving, so decisions "
            "keep resting on an approval that may no longer hold."
        ),
        run=_probe_crustdata,
        replayed=True,
        replay_note=(
            "The watcher has a one-hour minimum interval and cannot fire inside a "
            "demo. This path REPLAYS a stored payload, and that payload is "
            "reconstructed from CrustData's documentation — not a real capture."
        ),
    ),
    "superset": _Probe(
        name="Superset",
        variables=("WRITAI_DEMO_SUPERSET_PROJECT", "WRITAI_DEMO_SUPERSET_BASE_BRANCH"),
        degrades_to=(
            "The five demo sessions get plain directories instead of isolated "
            "worktrees. The demo still runs; the sessions just share a filesystem."
        ),
        run=_probe_superset,
    ),
}


def run_probes(
    settings: Settings | None = None,
    only: Sequence[str] | None = None,
) -> list[ProbeResult]:
    active = settings or default_settings
    names = list(only) if only else list(PROBES)
    results: list[ProbeResult] = []
    for key in names:
        probe = PROBES.get(key)
        if probe is None:
            continue
        try:
            results.append(probe.run(active))
        except Exception as exc:  # noqa: BLE001 - a probe bug must not kill the report
            results.append(
                ProbeResult(
                    name=probe.name,
                    status=ProbeStatus.UNVERIFIED,
                    detail=f"the probe itself failed: {type(exc).__name__}: {exc}",
                    degrades_to=probe.degrades_to,
                    variables=probe.variables,
                )
            )
    return results


_MARK = {
    ProbeStatus.LIVE: "[ LIVE ]",
    ProbeStatus.UNVERIFIED: "[  ??  ]",
    ProbeStatus.INVALID: "[ DEAD ]",
    ProbeStatus.ABSENT: "[ ---- ]",
}


def render(results: Sequence[ProbeResult]) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append("  writ.ai — sponsor integration status")
    lines.append("  " + "-" * 66)
    for item in results:
        lines.append(f"  {_MARK[item.status]}  {item.name}")
        lines.append(f"            {item.detail}")
        if item.replayed:
            lines.append(f"            REPLAYED · {item.replay_note}")
        if not item.ok:
            lines.append(f"            without it: {item.degrades_to}")
            if item.variables:
                lines.append(f"            set: {', '.join(item.variables)}")
        lines.append("")
    live = sum(1 for item in results if item.status is ProbeStatus.LIVE)
    dead = sum(1 for item in results if item.status is ProbeStatus.INVALID)
    unknown = sum(1 for item in results if item.status is ProbeStatus.UNVERIFIED)
    absent = sum(1 for item in results if item.status is ProbeStatus.ABSENT)
    lines.append("  " + "-" * 66)
    lines.append(
        f"  {live} live · {dead} dead credential(s) · "
        f"{unknown} unverified · {absent} not configured"
    )
    if dead:
        lines.append(
            "  A DEAD credential is worse than an absent one: it is set, so the "
            "code takes the live path and fails at the worst moment."
        )
    if unknown:
        lines.append(
            "  UNVERIFIED is not a pass. It means this command could not prove "
            "the credential works, so treat it as unknown."
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in arguments
    selected = [item for item in arguments if not item.startswith("-")]
    results = run_probes(only=selected or None)
    if as_json:
        sys.stdout.write(
            json.dumps(
                [
                    {
                        "name": item.name,
                        "status": item.status.value,
                        "detail": item.detail,
                        "degrades_to": item.degrades_to,
                        "variables": list(item.variables),
                        "replayed": item.replayed,
                        "replay_note": item.replay_note,
                    }
                    for item in results
                ],
                indent=2,
            )
            + "\n"
        )
    else:
        sys.stdout.write(render(results))
    # Exit 1 only for a credential that is set and broken. An absent integration
    # is a choice, not a failure, and must not fail a preflight.
    return 1 if any(item.status is ProbeStatus.INVALID for item in results) else 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
