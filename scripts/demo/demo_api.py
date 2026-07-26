#!/usr/bin/env python3
"""Read-only helpers for the Dragback demo launcher.

Standard library only, and written for Python 3.9 so it runs on a stock macOS
system python when the repo's .venv is absent. It reads the running services and
prints tab-separated rows the shell can consume; it never writes to a service,
never approves anything, and never decides a verdict.

Exit codes are the contract the shell scripts branch on:

    0  the answer is in stdout
    3  the service could not be reached
    4  the service answered, but the thing asked about does not exist
    5  the service answered with a shape this helper does not understand
    6  usage error
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import urllib.error
import urllib.request

#: Field separator for every row this helper prints.
#:
#: NOT a tab. Bash treats tab as IFS whitespace, so `IFS=$'\t' read -r a b c d`
#: collapses runs of tabs and silently shifts every field after an empty one — an
#: assignment with no recorded agent name would slide the session directory into
#: the prompt-index column and the launcher would build the wrong thing without
#: complaining. ASCII unit separator is non-whitespace, so empty fields survive,
#: and `cell()` guarantees it never appears inside a value.
SEP = "\x1f"

EXIT_OK = 0
EXIT_UNREACHABLE = 3
EXIT_NOT_FOUND = 4
EXIT_MALFORMED = 5
EXIT_USAGE = 6

TIMEOUT_SECONDS = 5.0

# The scope split the five-session fixture exists to prove: three sessions must
# be interrupted by an export.authorization change and two must survive it.
INTERRUPTED_SCOPE = "export.authorization"
PRESERVED_SCOPE = "export.generation"


def fail(message, code):
    sys.stderr.write("%s\n" % message)
    return code


def get_json(url):
    """GET one JSON document. Raises nothing the caller has to catch."""

    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    # The supervisor session routes are authenticated and fail closed: they
    # disclose which machines are running which task. Without this header
    # `GET /supervisor/sessions` answers 401 and the readiness check reports
    # "did not answer with a session list" for a service that is working fine.
    hook_api_key = (os.environ.get("DRAGBACK_HOOK_API_KEY") or "").strip()
    if hook_api_key:
        request.add_header("X-Dragback-Hook-API-Key", hook_api_key)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return None, error.code
    except Exception:
        return None, None
    try:
        return json.loads(body), 200
    except ValueError:
        return None, -1


def text(value):
    """One safe row cell: no separator, no control characters, never "None"."""

    if value is None:
        return ""
    out = str(value)
    for char in ("\t", "\n", "\r", SEP, "\v", "\f"):
        out = out.replace(char, " ")
    return out.strip()


def row_out(*cells):
    """Print one record. Every cell goes through `text` — no exceptions."""

    print(SEP.join(text(cell) for cell in cells))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def command_health(argv):
    if len(argv) != 1:
        return fail("usage: demo_api.py health BASE_URL", EXIT_USAGE)
    payload, status = get_json(argv[0].rstrip("/") + "/health")
    if payload is None:
        return EXIT_UNREACHABLE if status is None else EXIT_MALFORMED
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return EXIT_MALFORMED
    print(text(payload.get("graph_version", "")))
    return EXIT_OK


def _workspace(agent_url, workspace_id):
    return get_json(
        "%s/live-workspaces/%s" % (agent_url.rstrip("/"), workspace_id)
    )


def command_workspace(argv):
    """Key=value facts about the seeded workspace, for a readiness line."""

    if len(argv) != 2:
        return fail("usage: demo_api.py workspace AGENT_URL WORKSPACE_ID", EXIT_USAGE)
    payload, status = _workspace(argv[0], argv[1])
    if payload is None:
        if status is None:
            return EXIT_UNREACHABLE
        if status == 404:
            print("found=no")
            return EXIT_NOT_FOUND
        return EXIT_MALFORMED
    if not isinstance(payload, dict):
        return EXIT_MALFORMED

    supervisor = payload.get("supervisor")
    assignments = []
    supervisor_state = ""
    execution_mode = ""
    if isinstance(supervisor, dict):
        supervisor_state = text(supervisor.get("state"))
        execution_mode = text(supervisor.get("execution_mode"))
        raw = supervisor.get("assignments")
        if isinstance(raw, list):
            assignments = [item for item in raw if isinstance(item, dict)]

    report = payload.get("invalidation_report")
    invalidated = []
    if isinstance(report, dict):
        raw_invalidated = report.get("invalidated_task_ids")
        if isinstance(raw_invalidated, list):
            invalidated = [text(item) for item in raw_invalidated]

    print("found=yes")
    print("status=%s" % text(payload.get("status")))
    print("graph_version=%s" % text(payload.get("graph_version")))
    print("supervisor_state=%s" % supervisor_state)
    print("execution_mode=%s" % execution_mode)
    print("assignment_count=%d" % len(assignments))
    print("invalidated_count=%d" % len(invalidated))
    return EXIT_OK


def _task_facts(payload):
    """task_id -> (title, requirement text), read from the seeded workspace.

    The canned prompts are written in these exact words on purpose: the deny
    payload names the task by its title and the requirement by its key and
    value, so an agent whose work uses the same vocabulary can recognise the
    redirect as being about the file in front of it rather than a misroute.
    """

    facts = {}
    if not isinstance(payload, dict):
        return facts

    baseline = payload.get("baseline_decision")
    requirements = {}
    if isinstance(baseline, dict):
        attributes = baseline.get("attributes")
        if isinstance(attributes, dict):
            raw = attributes.get("requirements")
            if isinstance(raw, dict):
                requirements = raw

    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return facts
    for task in tasks:
        if not isinstance(task, dict):
            continue
        scopes = task.get("scopes") if isinstance(task.get("scopes"), list) else []
        pairs = []
        for scope in scopes:
            values = requirements.get(str(scope))
            if isinstance(values, dict):
                for key in sorted(values):
                    # Both halves through `text`: a requirement value carrying a
                    # separator would otherwise add a phantom column downstream.
                    pairs.append("%s: %s" % (text(key), text(values[key])))
        facts[text(task.get("id"))] = (
            text(task.get("title")),
            "; ".join(pairs),
        )
    return facts


def command_assignments(argv):
    """TSV: assignment_id, task_id, agent_name, state, scopes, snapshot,
    task title, current approved requirement."""

    if len(argv) != 2:
        return fail(
            "usage: demo_api.py assignments AGENT_URL WORKSPACE_ID", EXIT_USAGE
        )
    payload, status = _workspace(argv[0], argv[1])
    if payload is None:
        if status is None:
            return EXIT_UNREACHABLE
        if status == 404:
            return EXIT_NOT_FOUND
        return EXIT_MALFORMED
    supervisor = payload.get("supervisor") if isinstance(payload, dict) else None
    if not isinstance(supervisor, dict):
        return EXIT_NOT_FOUND
    raw = supervisor.get("assignments")
    if not isinstance(raw, list):
        return EXIT_MALFORMED

    facts = _task_facts(payload)
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        scopes = item.get("scopes")
        if not isinstance(scopes, list):
            scopes = []
        task_id = text(item.get("task_id"))
        title, requirement = facts.get(task_id, ("", ""))
        rows.append(
            (
                text(item.get("id")),
                task_id,
                text(item.get("agent_name")),
                text(item.get("state")),
                ",".join(text(scope) for scope in scopes),
                text(item.get("decision_snapshot")),
                title,
                requirement,
            )
        )
    # Stable by task id: the demo narrative depends on Sara being session 1
    # every rehearsal, not on repository iteration order.
    rows.sort(key=lambda row: row[1])
    for row in rows:
        row_out(*row)
    return EXIT_OK


def _locator_field(binding, name):
    """Read an id from the nested `assignment` locator, with a flat fallback.

    `ClaudeCodeSessionBinding` nests `assignment_id` and `task_id` inside an
    `AssignmentLocator`; reading them flat returns nothing, which makes every
    bound session look UNBOUND. That reads as the loudest possible failure in
    `check.sh` while actually being a bug in this helper, so it is worth the
    two lines. The flat fallback is kept for a service that inlines them.
    """

    if not isinstance(binding, dict):
        return ""
    nested = binding.get("assignment")
    if isinstance(nested, dict) and nested.get(name):
        return text(nested.get(name))
    return text(binding.get(name))


def _flag(item, name):
    """A tri-state boolean cell: `yes`, `no`, or empty when the service omits it."""

    value = item.get(name) if isinstance(item, dict) else None
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return ""


def command_sessions(argv):
    """TSV: session_id, task_id, assignment_id, source, cwd, decision_id,
    bound, snapshot_current, deny_spent, state, snapshot, graph.

    The last six come from `GET /supervisor/sessions` directly. They are what
    `check.sh` needs to catch the three silent demo-killers without inferring
    anything: an unbound session, a session pinned to a superseded snapshot,
    and an assignment whose deny-once has already been spent. Asking the
    service means the readiness check and the hook cannot disagree.

    `decision_id` is populated by the service only while a session is actually
    being denied and a human acknowledgement would release it. An empty cell
    means there is nothing to acknowledge — never acknowledge a decision that is
    not blocking anyone.
    """

    if len(argv) != 1:
        return fail("usage: demo_api.py sessions AGENT_URL", EXIT_USAGE)
    payload, status = get_json(argv[0].rstrip("/") + "/supervisor/sessions")
    if payload is None:
        return EXIT_UNREACHABLE if status is None else EXIT_MALFORMED
    if not isinstance(payload, dict):
        return EXIT_MALFORMED
    raw = payload.get("sessions")
    if not isinstance(raw, list):
        return EXIT_MALFORMED

    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        binding = item.get("binding") if isinstance(item.get("binding"), dict) else item
        rows.append(
            (
                text(binding.get("session_id")),
                _locator_field(binding, "task_id"),
                _locator_field(binding, "assignment_id"),
                text(binding.get("source")) or "unbound",
                text(binding.get("cwd")),
                text(binding.get("decision_id")),
                _flag(item, "bound"),
                _flag(item, "snapshot_current"),
                _flag(item, "deny_spent"),
                text(item.get("state")),
                text(item.get("decision_snapshot")),
                text(item.get("current_decision_snapshot")),
            )
        )
    rows.sort(key=lambda row: (row[1], row[0]))
    for row in rows:
        row_out(*row)
    return EXIT_OK


def command_plan(argv):
    """Choose which assignments this run drives, and in which order.

    Three sessions on the changed scope and two on the surviving scope is the
    proof the fixture ships. When fewer assignments exist the shortfall is
    filled from the other group and the caller is expected to say so.
    """

    if len(argv) != 2:
        return fail("usage: demo_api.py plan COUNT ASSIGNMENTS_TSV", EXIT_USAGE)
    try:
        wanted = int(argv[0])
    except ValueError:
        return fail("COUNT must be a number.", EXIT_USAGE)
    if wanted < 1:
        return fail("COUNT must be at least 1.", EXIT_USAGE)

    try:
        with open(argv[1], "r", encoding="utf-8") as handle:
            rows = [line.rstrip("\n").split(SEP) for line in handle if line.strip()]
    except OSError:
        return fail("Could not read %s." % argv[1], EXIT_NOT_FOUND)

    usable = [row for row in rows if len(row) >= 5]
    interrupted = [row for row in usable if INTERRUPTED_SCOPE in row[4].split(",")]
    preserved = [row for row in usable if INTERRUPTED_SCOPE not in row[4].split(",")]

    # 3 of 5, scaled, so --sessions 10 keeps the same story shape.
    interrupted_target = min(len(interrupted), (3 * wanted + 2) // 5)
    preserved_target = min(len(preserved), wanted - interrupted_target)
    shortfall = wanted - interrupted_target - preserved_target
    if shortfall > 0:
        interrupted_target = min(len(interrupted), interrupted_target + shortfall)
        shortfall = wanted - interrupted_target - preserved_target
    if shortfall > 0:
        preserved_target = min(len(preserved), preserved_target + shortfall)

    selected = preserved[:preserved_target] + interrupted[:interrupted_target]
    if not selected:
        return fail("No assignments are available to bind sessions to.", EXIT_NOT_FOUND)

    selected.sort(key=lambda row: row[1])
    for row in selected:
        row_out(*row)
    return EXIT_OK


def command_match(argv):
    """Compare the session manifest with what the supervisor actually bound.

    Prints one row per expected session:
        index, task_id, dir, verdict, detail
    where verdict is `bound`, `wrong-task`, `unbound` or `missing`.
    """

    if len(argv) != 2:
        return fail("usage: demo_api.py match MANIFEST_TSV SESSIONS_TSV", EXIT_USAGE)
    try:
        with open(argv[0], "r", encoding="utf-8") as handle:
            manifest = [
                line.rstrip("\n").split(SEP) for line in handle if line.strip()
            ]
    except OSError:
        return fail("Could not read %s." % argv[0], EXIT_NOT_FOUND)
    try:
        with open(argv[1], "r", encoding="utf-8") as handle:
            sessions = [
                line.rstrip("\n").split(SEP) for line in handle if line.strip()
            ]
    except OSError:
        sessions = []

    by_cwd = {}
    for row in sessions:
        if len(row) >= 5 and row[4]:
            by_cwd[os.path.realpath(row[4])] = row

    exit_code = EXIT_OK
    for entry in manifest:
        # manifest: index, task_id, assignment_id, agent_name, scopes, dir
        if len(entry) < 6:
            continue
        index, task_id, _assignment_id, _agent, _scopes, directory = entry[:6]
        found = by_cwd.get(os.path.realpath(directory))
        if found is None:
            verdict, detail = "missing", "no session registered from this directory"
            exit_code = EXIT_NOT_FOUND
        elif not found[2]:
            verdict, detail = "unbound", "session %s registered but unbound" % found[0]
            exit_code = EXIT_NOT_FOUND
        elif found[1] != task_id:
            verdict = "wrong-task"
            detail = "session %s is bound to %s" % (found[0], found[1] or "nothing")
            exit_code = EXIT_NOT_FOUND
        else:
            verdict = "bound"
            detail = "session %s via %s" % (found[0], found[3])
        row_out(index, task_id, directory, verdict, detail)
    return exit_code


def command_render_prompt(argv):
    """Fill a canned prompt's placeholders — literally, never through sed.

    The values come from the workspace API, so they are input: a `|`, `&` or
    backslash in one of them would corrupt a `sed s|…|…|g` program, and on some
    sed implementations worse than corrupt it. `str.replace` has no program text
    to escape into.
    """

    if len(argv) != 7:
        return fail(
            "usage: demo_api.py render-prompt SOURCE DEST AGENT TASK SCOPES "
            "TITLE REQUIREMENT",
            EXIT_USAGE,
        )
    source, dest, agent, task, scopes, title, requirement = argv
    try:
        with open(source, "r", encoding="utf-8") as handle:
            body = handle.read()
    except OSError:
        return fail("Could not read %s." % source, EXIT_NOT_FOUND)

    for token, value in (
        ("{{AGENT_NAME}}", agent),
        ("{{TASK_ID}}", task),
        ("{{SCOPES}}", scopes),
        ("{{TASK_TITLE}}", title),
        ("{{REQUIREMENT}}", requirement),
    ):
        body = body.replace(token, value)

    try:
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError:
        return fail("Could not write %s." % dest, EXIT_NOT_FOUND)
    return EXIT_OK


def command_settings(argv):
    """Write one session's Claude Code hook settings as real JSON.

    Hand-assembled JSON is how a repo path containing a quote or a backslash
    silently corrupts the file that configures enforcement. `json.dump` escapes;
    `shlex.quote` keeps the interpreter and script path one shell word each.

    This writes ONLY inside a generated session directory, which lives outside
    every checkout. It never touches a repo's .claude/settings.json and never
    touches ~/.claude/settings.json.
    """

    if len(argv) != 6:
        return fail(
            "usage: demo_api.py settings DEST REPO_DIR AGENT_URL PYTHON_BIN "
            "CACHE_PATH WITH_HOOKS",
            EXIT_USAGE,
        )
    dest, repo_dir, agent_url, python_bin, cache_path, with_hooks = argv

    def hook(script):
        command = "%s %s" % (
            shlex.quote(python_bin),
            shlex.quote(os.path.join(repo_dir, "hooks", script)),
        )
        return [{"type": "command", "command": command, "timeout": 5}]

    # A settings file naming a hook script that does not exist is worse than no
    # hook at all: Claude Code reports a hook ERROR on every tool call and the
    # session cannot do anything, which is not what "degrade when a component is
    # not merged yet" means. When the scripts are absent the hooks block is
    # omitted entirely — sessions run unenforced, and the caller says so loudly.
    settings = {}
    if with_hooks == "1":
        settings["hooks"] = {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact|fork",
                    "hooks": hook("dragback_session_start.py"),
                }
            ],
            "PreToolUse": [
                {"matcher": "*", "hooks": hook("dragback_pre_tool_use.py")}
            ],
            "SessionEnd": [{"hooks": hook("dragback_session_end.py")}],
        }
    settings["env"] = {
        "DRAGBACK_HOOK_ENDPOINT": "%s/supervisor/sessions" % agent_url.rstrip("/"),
        "DRAGBACK_HOOK_TIMEOUT_SECONDS": "3",
        "DRAGBACK_HOOK_CACHE_PATH": cache_path,
    }
    # The service authenticates every session route and fails closed with 503
    # HOOK_AUTHENTICATION_NOT_CONFIGURED when no key is set. Without this the
    # hook is rejected on every call and denies -- safe, but it denies the
    # surviving sessions too, so the demo's whole point is lost.
    #
    # It has to be written here rather than inherited: under tmux the panes
    # inherit the tmux SERVER's environment, which was fixed when that server
    # started and is not this launcher's. An exported key reaches the services;
    # only this file reliably reaches the hooks.
    #
    # lib.sh resolves the key from the shell or .env before falling back to the
    # local demo default, so the value written here may be a real per-developer
    # token. Hence 0600 below -- assume it IS a secret.
    hook_api_key = (os.environ.get("DRAGBACK_HOOK_API_KEY") or "").strip()
    if hook_api_key:
        settings["env"]["DRAGBACK_HOOK_API_KEY"] = hook_api_key
    try:
        with open(dest, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2)
            handle.write("\n")
    except OSError:
        return fail("Could not write %s." % dest, EXIT_NOT_FOUND)
    if hook_api_key:
        # After the write, not before: an existing file would keep its old mode.
        # A failure here is not fatal -- the demo still works, the key is just
        # readable by other local accounts -- but it must be said out loud.
        try:
            os.chmod(dest, 0o600)
        except OSError:
            sys.stderr.write(
                "warning: could not restrict %s to owner-only; it carries the "
                "hook api key.\n" % dest
            )
    return EXIT_OK


def command_newest_recording(argv):
    """Newest file in a directory, chosen without xargs.

    `find -print0 | xargs -0 ls -t | head -1` runs `ls -t` with no operands when
    the directory is empty, which lists the *current* directory and hands back an
    unrelated file for the fallback to play in front of judges.
    """

    if len(argv) != 1:
        return fail("usage: demo_api.py newest-recording DIRECTORY", EXIT_USAGE)
    directory = argv[0]
    best = None
    best_mtime = None
    try:
        entries = os.listdir(directory)
    except OSError:
        return EXIT_NOT_FOUND
    for name in entries:
        if name == ".gitkeep":
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if best_mtime is None or mtime > best_mtime:
            best, best_mtime = path, mtime
    if best is None:
        return EXIT_NOT_FOUND
    print(best)
    return EXIT_OK


def command_superset_parse(argv):
    """Pull a workspace id and its worktree path out of Superset's JSON.

    The CLI reference documents that `--json` returns the workspace object with
    the id and the worktree path, but not the field spellings, and the CLI is
    beta ("commands and flags are still evolving"). So this looks for the
    plausible spellings and, failing that, for any string value that is an
    existing directory — and returns non-zero rather than guessing, which makes
    the caller fall back to a plain directory instead of pointing a session at
    something that does not exist.
    """

    if argv:
        return fail("usage: demo_api.py superset-parse   (JSON on stdin)", EXIT_USAGE)
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        return EXIT_MALFORMED
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict):
        return EXIT_MALFORMED
    # Some CLIs nest the object under a envelope key.
    for envelope in ("workspace", "data", "result"):
        inner = payload.get(envelope)
        if isinstance(inner, dict):
            payload = inner
            break

    identifier = ""
    for key in ("id", "workspaceId", "workspace_id", "uuid"):
        value = text(payload.get(key))
        if value:
            identifier = value
            break

    path = ""
    for key in (
        "worktreePath",
        "worktree_path",
        "workspacePath",
        "workspace_path",
        "path",
        "directory",
        "cwd",
    ):
        value = text(payload.get(key))
        if value and os.path.isdir(value):
            path = value
            break
    if not path:
        for value in payload.values():
            if isinstance(value, str) and value.startswith("/") and os.path.isdir(value):
                path = text(value)
                break

    if not path:
        return fail("Superset returned no usable worktree path.", EXIT_MALFORMED)
    row_out(identifier, path)
    return EXIT_OK


COMMANDS = {
    "health": command_health,
    "workspace": command_workspace,
    "assignments": command_assignments,
    "sessions": command_sessions,
    "plan": command_plan,
    "match": command_match,
    "render-prompt": command_render_prompt,
    "settings": command_settings,
    "newest-recording": command_newest_recording,
    "superset-parse": command_superset_parse,
}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        return fail(
            "usage: demo_api.py {%s} ..." % "|".join(sorted(COMMANDS)), EXIT_USAGE
        )
    try:
        return COMMANDS[argv[0]](argv[1:])
    except Exception as error:  # never a stack trace in front of an audience
        return fail("demo_api.py %s failed: %s" % (argv[0], error), EXIT_MALFORMED)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
