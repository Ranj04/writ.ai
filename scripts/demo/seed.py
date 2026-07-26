"""Stage seeder for the five-session writ.ai demo.

Re-runnable. Every run deletes and rebuilds the scratch workspace store, so the
starting state is byte-identical each time. That matters because deny-once is
per ASSIGNMENT: without a clean re-seed the second rehearsal silently allows and
the demo looks broken.

    PYTHONPATH=backend .venv/bin/python scripts/demo/seed.py            # seed only
    PYTHONPATH=backend .venv/bin/python scripts/demo/seed.py --serve    # seed + serve

Never writes to ~/.claude/settings.json. Only .claude/settings.local.json inside
the scratch session directories under /tmp/writai-stage/.
"""
from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import cast

import os

STAGE = Path("/tmp/writai-stage")
STORE = STAGE / "live-workspaces.json"
# MUST precede every writai import. `agent_api` constructs its repository,
# assignment gateway and session router at import time, and the router closes
# over those objects; reassigning module attributes later leaves the live route
# serving the production store. That failure mode looks exactly like a working
# demo right up until nothing is denied.
os.environ["WRITAI_WORKSPACE_STORE"] = str(STORE)
# The session routes authenticate the hook and fail closed when no key is set,
# so the demo needs one on both sides. Local-only, and never a real secret.
DEMO_HOOK_API_KEY = "writai-demo-hook-key"
os.environ.setdefault("WRITAI_HOOK_API_KEY", DEMO_HOOK_API_KEY)

import uvicorn  # noqa: E402
import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from writai.services import agent_api, authority_api  # noqa: E402
from writai.supervisor_contract import InterruptRequest  # noqa: E402
from writai.workspaces.interrupt_port import (  # noqa: E402
    WorkspaceSupervisorInterruptPort,
)
from writai.domain import utc_now  # noqa: E402
from writai.hashing import stable_hash  # noqa: E402
from writai.intake.approval import (  # noqa: E402
    ApprovalChannel,
    ApprovalEvidence,
)
from writai.workspaces.models import (  # noqa: E402
    LiveWorkspaceImportRequest,
    WorkspaceApprovalRequest,
    WorkspaceProposalRequest,
)
from writai.workspaces.repository import (  # noqa: E402
    JsonFileLiveWorkspaceRepository,
)

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"
WORKSPACE_ID = "csv-exports"
HOOK_DIR = Path(__file__).resolve().parents[2] / "hooks"
HOOK_TARGET = HOOK_DIR / "writai_pre_tool_use.py"
HOOK_START = HOOK_DIR / "writai_session_start.py"
HOOK_END = HOOK_DIR / "writai_session_end.py"
AGENT_PORT = 8002
AUTHORITY_PORT = 8001

REDIRECT = (
    "Exports are admin-only. Gate the export control behind an administrator "
    "check before continuing."
)
INTERRUPT_REASON = (
    "Approved decision DEC-018 changed export.authorization: exports must be "
    "admin-only."
)


# --------------------------------------------------------------------------
# Session fixtures. The starter prompts are load-bearing: a real Claude Code
# session rejects a redirect that has nothing to do with its actual work, so
# each session must genuinely be doing the thing the redirect corrects.
# --------------------------------------------------------------------------

SESSIONS: tuple[dict[str, str], ...] = (
    {
        "slug": "sara",
        "person": "Sara",
        "task": "TASK-201",
        "session_id": "sess-sara",
        "filename": "csv_writer.py",
        "source": '''"""CSV serialization for account data exports."""
from __future__ import annotations


ACCOUNT_COLUMNS = ["account_id", "email", "plan", "created_at", "balance_cents"]


def rows_for(accounts):
    """Yield one list-of-values per account, in ACCOUNT_COLUMNS order."""
    for account in accounts:
        yield [account.get(column) for column in ACCOUNT_COLUMNS]


def write_csv(accounts, handle):
    """Write accounts to `handle` as CSV.

    TODO: this hand-rolled join is wrong. Values containing commas, quotes or
    newlines are emitted unescaped, which produces a file most spreadsheet
    programs refuse to open.
    """
    handle.write(",".join(ACCOUNT_COLUMNS) + "\\n")
    for row in rows_for(accounts):
        handle.write(",".join("" if value is None else str(value) for value in row) + "\\n")
''',
        "prompt": """You are working on TASK-201: Generate valid CSV files.

Open csv_writer.py in this directory. write_csv() hand-rolls the CSV by joining
on commas, so any account whose email or plan name contains a comma, a double
quote or a newline produces a corrupt file that Excel and Numbers refuse to open.

First edit: rewrite write_csv() to use Python's stdlib `csv` module
(csv.writer with the default dialect) so quoting and escaping are handled
correctly, and make it write a proper header row. Keep the ACCOUNT_COLUMNS
order and keep rows_for() as the row source. Then show me the diff.
""",
    },
    {
        "slug": "alex",
        "person": "Alex",
        "task": "TASK-202",
        "session_id": "sess-alex",
        "filename": "export_stream.py",
        "source": '''"""Export delivery. Currently buffers the whole export in memory."""
from __future__ import annotations

import io

from csv_writer import write_csv


def build_export(accounts):
    """Materialize the entire CSV in a StringIO before anything is returned.

    TODO: a 400k-row account set is roughly 90MB of Python string here. Two
    concurrent exports have taken the worker out with an OOM twice this month.
    """
    buffer = io.StringIO()
    write_csv(accounts, buffer)
    return buffer.getvalue()


def export_response(accounts):
    body = build_export(accounts)
    return {
        "status": 200,
        "headers": {
            "Content-Type": "text/csv",
            "Content-Disposition": 'attachment; filename="accounts.csv"',
        },
        "body": body,
    }
''',
        "prompt": """You are working on TASK-202: Stream large exports.

Open export_stream.py in this directory. build_export() materializes the whole
CSV into a StringIO before returning it, so a 400k-row account set costs ~90MB
of resident memory and two concurrent exports have OOM'd the worker.

First edit: replace build_export() with a generator, stream_export(accounts),
that yields the header row and then one encoded CSV line at a time without ever
holding the full document in memory. Update export_response() to pass that
generator through as the body instead of a string. Then show me the diff.
""",
    },
    {
        "slug": "priya",
        "person": "Priya",
        "task": "TASK-203",
        "session_id": "sess-priya",
        "filename": "export_button.py",
        "source": '''"""Renders the account-data export control in the settings page."""
from __future__ import annotations


def render_settings_actions(user):
    """Build the action list for the account settings page."""
    actions = [
        {"id": "change-password", "label": "Change password"},
        {"id": "download-invoices", "label": "Download invoices"},
    ]
    actions.extend(export_actions(user))
    return actions


def export_actions(user):
    """Return the export control for this user.

    TODO: still hidden behind the old beta flag. It is supposed to be visible
    to every signed-in user now.
    """
    if not user.get("beta_export_enabled"):
        return []
    return [{"id": "export-csv", "label": "Export account data (CSV)"}]
''',
        "prompt": """You are working on TASK-203: Expose the export control to all users.

Open export_button.py in this directory. The CSV export button is still gated
behind the old `beta_export_enabled` flag, so almost nobody can see it. Product
wants it visible to every authenticated user.

First edit: change export_actions() so the "Export account data (CSV)" action is
returned for any authenticated user, dropping the beta_export_enabled check
entirely. Then show me the diff.
""",
    },
    {
        "slug": "marcus",
        "person": "Marcus",
        "task": "TASK-204",
        "session_id": "sess-marcus",
        "filename": "export_api.py",
        "source": '''"""HTTP entry point for requesting an account-data export."""
from __future__ import annotations


ALLOWED_EXPORT_ROLES = {"admin", "support_lead", "billing"}


def authorize_export(request):
    """Decide whether this caller may request an export.

    TODO: this allowlist is left over from the internal-only pilot. Any
    authenticated caller is supposed to be able to request their own export.
    """
    user = request.get("user")
    if user is None:
        return {"allowed": False, "status": 401, "reason": "authentication required"}
    if user.get("role") not in ALLOWED_EXPORT_ROLES:
        return {"allowed": False, "status": 403, "reason": "role not permitted to export"}
    return {"allowed": True, "status": 200, "reason": "ok"}


def post_export(request):
    verdict = authorize_export(request)
    if not verdict["allowed"]:
        return {"status": verdict["status"], "body": {"error": verdict["reason"]}}
    return {"status": 202, "body": {"export": "queued"}}
''',
        "prompt": """You are working on TASK-204: Open the export API to all users.

Open export_api.py in this directory. authorize_export() still enforces the
ALLOWED_EXPORT_ROLES allowlist from the internal-only pilot, so ordinary
customers get a 403 when they POST to the export endpoint. Every authenticated
caller should be able to request their own export.

First edit: remove the role allowlist from authorize_export() so any
authenticated user is allowed through (keep the 401 for anonymous callers), and
delete the now-unused ALLOWED_EXPORT_ROLES constant. Then show me the diff.
""",
    },
    {
        "slug": "dan",
        "person": "Dan",
        "task": "TASK-205",
        "session_id": "sess-dan",
        "filename": "EXPORT_PERMISSIONS.md",
        "source": """# Account data export - permission model

Status: DRAFT - do not publish until this reflects the shipped behaviour.

## Who can export

TODO: this section is stale. It still describes the internal-only pilot, where
only staff roles could trigger an export.

Today the export endpoint is restricted to users holding one of the internal
roles (`admin`, `support_lead`, `billing`). Customers must open a support ticket
and wait for an agent to run the export on their behalf.

## Formats

CSV only. One row per account, UTF-8, RFC 4180 quoting.

## Delivery

The export is streamed back on the same request as `text/csv`.
""",
        "prompt": """You are working on TASK-205: Document the export permission model.

Open EXPORT_PERMISSIONS.md in this directory. The "Who can export" section is
stale - it still describes the internal-only pilot where only staff roles
(admin, support_lead, billing) could trigger an export, and tells customers to
file a support ticket.

First edit: rewrite the "Who can export" section to state that account data
exports are available to all authenticated users, that no staff role is
required, and that the support-ticket workaround is retired. Then show me the
diff.
""",
    },
)


def _load(name: str) -> dict[str, object]:
    with (EXAMPLES / name).open(encoding="utf-8") as handle:
        return cast(dict[str, object], yaml.safe_load(handle))


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _start_authority() -> None:
    """Bring up the real authority service agent_api calls, unless it is up."""
    if _port_open(AUTHORITY_PORT):
        return
    server = uvicorn.Server(
        uvicorn.Config(
            authority_api.app,
            host="127.0.0.1",
            port=AUTHORITY_PORT,
            log_level="warning",
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return
        time.sleep(0.1)


def _approval(
    repository: JsonFileLiveWorkspaceRepository,
    *,
    role: str,
    decision_id: str,
    baseline: bool,
) -> WorkspaceApprovalRequest:
    """Build an approval bound to the exact proposal on disk right now.

    Lane B binds every approval to a fingerprint and instance id so a proposal
    cannot change between confirmation and application. The seeder honours that
    rather than routing around it: both values are read from the stored record.
    """

    record = repository.get(WORKSPACE_ID)
    if baseline:
        decision = record.definition.baseline_decision
        instance_id = record.baseline_proposal_instance_id
    else:
        assert record.pending_mutation is not None, "no pending proposal to approve"
        # A change is fingerprinted over the whole mutation, not just its
        # decision: the supersession target and affected scopes are part of what
        # the human confirmed.
        decision = record.pending_mutation
        instance_id = record.pending_proposal_instance_id
    assert instance_id is not None, "workspace has no proposal instance id"
    fingerprint = stable_hash(decision)
    return WorkspaceApprovalRequest(
        actor_role=role,
        proposal_fingerprint=fingerprint,
        proposal_instance_id=instance_id,
        approval_evidence=ApprovalEvidence(
            workspace_id=WORKSPACE_ID,
            decision_id=decision_id,
            approver_user_id=f"demo-seeder:{role}",
            permission_id=role,
            channel=ApprovalChannel.CLI,
            evidence_ref=f"seed://{WORKSPACE_ID}/{decision_id}",
            approved_at=utc_now(),
            confirmed_proposal_fingerprint=fingerprint,
            confirmed_proposal_instance_id=instance_id,
        ),
    )


#: The seeder drives the orchestrator in process, so the channel authentication
#: in front of the approval routes never runs. That is a deliberate, documented
#: trade-off (ASSUMPTIONS A-5) and it bypasses no authority check — but it must
#: not be reachable by accident, so it is gated on an explicit opt-in that the
#: demo sets and nothing else does.
SEED_UNAUTHENTICATED_APPROVAL_ENV = "WRITAI_DEMO_UNAUTHENTICATED_APPROVAL"


def _require_unauthenticated_approval_optin() -> None:
    """Refuse to seed unless a human has said this is a demo machine.

    What is bypassed is the *channel* authentication: the HTTP approval routes
    require an ``ApprovalAttemptEnvelope`` carrying a Hexclave-resolvable
    ``approval_token``, and no local demo has one. What is NOT bypassed is any
    authority decision — role, scope, confidence, the three-way requirement
    match and Lane B's proposal-binding check all run unchanged inside
    ``approve_baseline`` and ``approve_decision``.

    The risk this closes is narrow and real: pointed at a store that is not a
    demo store, this writes approvals nobody authenticated.
    """

    if (os.environ.get(SEED_UNAUTHENTICATED_APPROVAL_ENV) or "").strip() != "1":
        raise SystemExit(
            "refusing to seed.\n\n"
            "  This seeder approves the baseline and the change by calling the\n"
            "  orchestrator directly, so the channel authentication in front of\n"
            "  the approval routes never runs. Every authority check still runs\n"
            "  -- role, scope, confidence, the three-way requirement match and\n"
            "  the proposal binding -- but nobody authenticated the approver.\n\n"
            "  That is fine on a demo machine and nowhere else, so say so:\n\n"
            f"      export {SEED_UNAUTHENTICATED_APPROVAL_ENV}=1\n\n"
            f"  It writes to {STORE}, which it deletes and rebuilds.\n"
            "  See ASSUMPTIONS.md A-5 and the real-vs-simulated panel."
        )


def seed_graph() -> JsonFileLiveWorkspaceRepository:
    """Delete the store, rebuild it, and fire the interrupt. Idempotent."""
    _require_unauthenticated_approval_optin()
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    # The service already points here (see the env var above), so reuse ITS
    # repository rather than building a second one over the same file.
    repository = agent_api.workspace_repository

    _start_authority()

    # Seeding drives the orchestrator in-process rather than the HTTP approval
    # routes. Those routes now require an ApprovalAttemptEnvelope carrying a
    # Hexclave-resolvable approval_token, which no local demo has. The authority
    # decisions themselves are still real: role, scope, confidence and the
    # three-way requirement match are all checked inside approve_baseline and
    # approve_decision exactly as they are in production. What is bypassed is the
    # *channel* authentication in front of them, and only for seeding.
    orchestrator = agent_api.workspace_orchestrator
    orchestrator.import_workspace(
        LiveWorkspaceImportRequest.model_validate(
            _load("writai-five-sessions.yaml")
        )
    )
    orchestrator.approve_baseline(
        WORKSPACE_ID,
        _approval(
            repository,
            role="approve_product",
            decision_id=orchestrator.get(WORKSPACE_ID).baseline_decision.id,
            baseline=True,
        ),
    )
    authorized = orchestrator.authorize(WORKSPACE_ID)
    assert authorized.initial_authorization is not None
    assert authorized.initial_authorization.verdict.value == "ALLOW"
    orchestrator.propose_decision(
        WORKSPACE_ID,
        WorkspaceProposalRequest.model_validate(
            _load("writai-five-sessions-change.yaml")
        ),
    )
    orchestrator.approve_decision(
        WORKSPACE_ID,
        "DEC-018",
        _approval(
            repository,
            role="approve_compliance",
            decision_id="DEC-018",
            baseline=False,
        ),
    )

    # Lane B across the frozen seam. redirect_instruction is what turns this
    # into deny-once-with-a-correction instead of a permanent deny.
    WorkspaceSupervisorInterruptPort(repository=repository).interrupt(
        InterruptRequest(
            workspace_id=WORKSPACE_ID,
            decision_id="DEC-018",
            affected_scopes=frozenset({"export.authorization"}),
            provenance_path=(),
            interrupt_reason=INTERRUPT_REASON,
            redirect_instruction=REDIRECT,
        )
    )
    return repository


def _hook_command(script: Path, port: int) -> str:
    """Env inline so each stage directory targets THIS server, not the default.

    A session pointed at the wrong port registers nowhere, is treated as unbound,
    and is allowed everything — which looks exactly like a working demo.
    """

    endpoint = f"WRITAI_HOOK_ENDPOINT=http://127.0.0.1:{port}/supervisor/sessions "
    key = os.environ["WRITAI_HOOK_API_KEY"]
    if key != DEMO_HOOK_API_KEY:
        # An overridden key is a real secret. Settings files are world-readable,
        # so inherit it from the launching shell instead of writing it to disk.
        return f"{endpoint}python3 {script}"
    return f"WRITAI_HOOK_API_KEY={key} {endpoint}python3 {script}"


def _settings_local(port: int) -> str:
    """Project-local hook wiring. Never `~/.claude/settings.json`."""

    def entry(script: Path, matcher: str | None = "*") -> dict[str, object]:
        hook = {"type": "command", "command": _hook_command(script, port), "timeout": 10}
        return {"matcher": matcher, "hooks": [hook]} if matcher else {"hooks": [hook]}

    return json.dumps(
        {
            "hooks": {
                # SessionStart fires on named lifecycle events, not "*": a resume,
                # clear, compact or fork must re-register or the session silently
                # reads as unregistered.
                "SessionStart": [entry(HOOK_START, "startup|resume|clear|compact|fork")],
                "PreToolUse": [entry(HOOK_TARGET)],
                "SessionEnd": [entry(HOOK_END, matcher=None)],
            }
        },
        indent=2,
    )


def seed_sessions(port: int = AGENT_PORT) -> list[Path]:
    """Create the five scratch session directories. Never touches ~/.claude."""
    for script in (HOOK_TARGET, HOOK_START, HOOK_END):
        if not script.exists():
            raise SystemExit(f"missing hook script: {script}")

    directories: list[Path] = []
    for session in SESSIONS:
        directory = STAGE / session["slug"]
        (directory / ".writai").mkdir(parents=True, exist_ok=True)
        (directory / ".claude").mkdir(parents=True, exist_ok=True)
        (directory / ".writai" / "task").write_text(
            session["task"], encoding="utf-8"
        )
        (directory / ".claude" / "settings.local.json").write_text(
            _settings_local(port) + "\n", encoding="utf-8"
        )
        (directory / session["filename"]).write_text(
            session["source"], encoding="utf-8"
        )
        (directory / "PROMPT.txt").write_text(session["prompt"], encoding="utf-8")
        directories.append(directory)
    return directories


def main() -> None:
    argv = sys.argv[1:]
    serve = "--serve" in argv
    port = AGENT_PORT
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    seed_graph()
    seed_sessions(port)

    print(f"seeded store   {STORE}")
    print(f"seeded hook    {HOOK_TARGET}")
    for session in SESSIONS:
        survives = session["task"] in {"TASK-201", "TASK-202"}
        state = "survives   " if survives else "interrupted"
        print(
            f"  {state}  {session['person']:<7} {session['task']}  "
            f"{STAGE / session['slug']}"
        )

    print("")
    print("start the server:")
    print(f"  {SEED_UNAUTHENTICATED_APPROVAL_ENV}=1 \\")
    print(f"  PYTHONPATH=backend {REPO / '.venv/bin/python'} "
          f"{Path(__file__).resolve()} --serve")
    print("")
    print("launch the five sessions, one per terminal:")
    for session in SESSIONS:
        directory = STAGE / session["slug"]
        print(f'  cd {directory} && claude "$(cat PROMPT.txt)"')
    print("")
    print("re-seed between rehearsals (deny-once is per assignment):")
    print(f"  {SEED_UNAUTHENTICATED_APPROVAL_ENV}=1 \\")
    print(f"  PYTHONPATH=backend {REPO / '.venv/bin/python'} {Path(__file__).resolve()}")

    if serve:
        print("")
        # Refuse a port that is already taken, and say so, rather than printing
        # "listening" and then failing to bind. A confident success message in
        # front of a dead server is how a rehearsal ends up curling someone
        # else's process and reading its 404s as a broken product.
        if _port_open(port):
            raise SystemExit(
                f"port {port} is already in use — stop the process holding it "
                f"(lsof -tiTCP:{port} -sTCP:LISTEN | xargs kill) or pass "
                f"--port N, then seed again so the session directories point at "
                f"the port you actually serve on"
            )
        print(f"agent service listening on http://127.0.0.1:{port}", flush=True)
        uvicorn.run(
            agent_api.app, host="127.0.0.1", port=port, log_level="warning"
        )


if __name__ == "__main__":
    main()
