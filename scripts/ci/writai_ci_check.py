#!/usr/bin/env python3
"""writ.ai pull-request authorization backstop.

The Claude Code ``PreToolUse`` hook fails **open**: a timeout, a crash, or an
unreachable supervisor lets the tool call proceed. ``hooks/writai_hook_lib.py``
says so in its own module docstring, and ``session_enforcement.py`` names this
check as the compensating control in two separate comments. This is that check.

It resolves the branch to a task using *the same rules as session binding*
(``workspaces/session_binding.py``), asks the agent service what that task's
authorization currently is, and fails the pull request when the task was
invalidated or the grant covering it is bound to a superseded snapshot.

Three properties matter, in this order.

1. **It fails closed.** Unlike the hook, every failure path here — an
   unreachable service, a malformed response, a bug in this file — exits
   non-zero. There is no cached-verdict fallback, because a check that passes
   during an outage is not a backstop.
2. **It re-implements no policy.** The binding order, the candidate filter and
   the verdict order below mirror the service exactly, including the ordering
   subtleties documented at each site. It reads state; it never decides
   authority, and no model output reaches a verdict.
3. **It explains itself the way the hook does.** The failure text carries what
   is still valid, what no longer is, what is now required, the affected scopes
   and the provenance path — the ``docs/TERMINAL_OUTPUT_SPEC.md`` section 1
   block the developer already recognises from the terminal.

Standard library only, and 3.9-compatible syntax: ``scripts/ci/verify.sh`` runs
this on developer machines, not just on a pinned CI runner.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import AbstractSet, Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------------------
# Exit codes. Every non-zero value fails the check; the distinction is for humans.
# --------------------------------------------------------------------------------------

EXIT_OK = 0
EXIT_UNAUTHORIZED = 1
EXIT_UNREACHABLE = 2
EXIT_USAGE = 3

DEFAULT_AGENT_URL = "http://localhost:8002"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 120.0

ENV_AGENT_URL = "WRITAI_AGENT_URL"
ENV_TIMEOUT = "WRITAI_CI_TIMEOUT_SECONDS"
ENV_API_KEY = "WRITAI_CI_API_KEY"
API_KEY_HEADER = "X-writ.ai-Hook-API-Key"

#: Mirrors ``RepositorySupervisorAssignmentGateway.list_live_claude_assignments``.
#: A candidate must be live at both levels, must be a provider this product can
#: actually enforce, and must not be completed.
EXECUTION_MODE_LIVE = "live"
EXECUTION_MODES = frozenset({"live", "simulated"})
BINDABLE_PROVIDERS = frozenset({"generic", "claude-code"})
RUNTIME_PROVIDERS = frozenset({"generic", "codex", "claude-code"})
STATE_INTERRUPTED = "interrupted"
STATE_CONTINUING = "continuing"
STATE_COMPLETED = "completed"
ASSIGNMENT_STATES = frozenset(
    {
        "queued",
        "running",
        STATE_CONTINUING,
        STATE_INTERRUPTED,
        "redirected",
        "resumed",
        STATE_COMPLETED,
    }
)

VERDICT_ALLOW = "ALLOW"

#: Mirrors ``session_binding._MAX_TASK_FILE_BYTES`` and its one-line rule.
MAX_MARKER_FILE_BYTES = 512
MAX_MARKER_VALUE_CHARS = 255

# `docs/TERMINAL_OUTPUT_SPEC.md` section 1 - the same block shape the hook emits.
LEADER_STOPPED = "⏹"
LEADER_OK = "✓"
INDENT = "  "
BODY = "     "
LABEL_WIDTH = 14
MAX_ROW_ITEMS = 12
MAX_ROW_ITEM_CHARS = 80
#: The redirect instruction is the one row a reader acts on, so it gets its own
#: budget. Truncating it to the width of an ID list would cut the sentence that
#: says what to do next.
MAX_INSTRUCTION_CHARS = 1_000
MAX_PROVENANCE_NODES = 12
ARROW = " → "


class ServiceUnreachable(RuntimeError):
    """The agent service could not be reached or could not be understood."""


class MalformedServiceResponse(ServiceUnreachable):
    """The service answered, but part of the answer is not the shape it claims.

    This FAILS rather than being filtered away, and the rule is the same one the
    `.writai/attach` marker settled: *absence* of binding information is
    permissive, *failure to obtain* it is not. A workspace or assignment that is
    not even an object is a failure to obtain a clean answer — and silently
    dropping it can empty the candidate set, resolve the branch to UNBOUND, and
    PASS a branch whose authorization was never actually evaluated.

    Unknown extra fields are harmless schema drift. Missing or invalid fields
    needed to determine candidacy are not: treating an unknown execution mode,
    provider, or state as "not a candidate" would recreate the silent pass.
    """


class UsageError(RuntimeError):
    """The check was invoked without something it needs to be meaningful."""


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------


class Config(object):
    """Everything the check needs, resolved from flags then environment."""

    def __init__(
        self,
        agent_url: str,
        repo_root: Path,
        branch: str,
        timeout_seconds: float,
        api_key: str = "",
        require_binding: bool = False,
        require_grant: bool = True,
    ) -> None:
        self.agent_url = agent_url.rstrip("/")
        self.repo_root = repo_root
        self.branch = branch
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key
        self.require_binding = require_binding
        self.require_grant = require_grant

    @property
    def workspaces_url(self) -> str:
        return self.agent_url + "/live-workspaces"


def build_config(args: argparse.Namespace, env: Dict[str, str]) -> Config:
    agent_url = (args.agent_url or env.get(ENV_AGENT_URL) or "").strip()
    if not agent_url:
        # Deliberately not defaulted to localhost in CI. A check silently
        # pointed at a service that cannot exist on a runner would pass by
        # accident, and "passed because it was misconfigured" is the failure
        # mode this whole file exists to prevent.
        if is_github_actions(env):
            raise UsageError(
                "No writ.ai agent URL configured. Set the WRITAI_AGENT_URL "
                "repository variable (or pass --agent-url) so the check can "
                "reach the authority service. Failing closed."
            )
        agent_url = DEFAULT_AGENT_URL

    repo_root = Path(args.repo_root or env.get("GITHUB_WORKSPACE") or os.getcwd())
    repo_root = repo_root.expanduser()

    branch = (args.branch or "").strip() or resolve_branch(env, repo_root)
    if not branch:
        # An unresolvable branch is not the same as a branch that binds to
        # nothing: we cannot even apply the rule. Say so rather than guessing.
        raise UsageError(
            "Could not determine the branch under review. Pass --branch "
            "explicitly. Failing closed."
        )

    timeout = positive_float(
        args.timeout if args.timeout is not None else env.get(ENV_TIMEOUT),
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    return Config(
        agent_url=agent_url,
        repo_root=repo_root,
        branch=branch,
        timeout_seconds=min(timeout, MAX_TIMEOUT_SECONDS),
        api_key=(env.get(ENV_API_KEY) or "").strip(),
        require_binding=bool(args.require_binding),
        # On by default. It is scoped by where it is applied, not by a flag: an
        # unbound branch returns from `evaluate` before the grant is ever
        # consulted, so a docs PR still passes. Only a branch that actually
        # resolves to a live assignment has to show a grant.
        require_grant=not bool(args.allow_missing_grant),
    )


def is_github_actions(env: Dict[str, str]) -> bool:
    return (env.get("GITHUB_ACTIONS") or "").strip().lower() == "true"


def positive_float(raw: Any, default: float) -> float:
    try:
        parsed = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def resolve_branch(env: Dict[str, str], repo_root: Path) -> str:
    """The branch under review.

    On ``pull_request`` the checkout is a detached merge commit, so ``git`` would
    report ``HEAD``. ``GITHUB_HEAD_REF`` is the PR's source branch and is the
    only value that corresponds to what a developer's session would have been
    bound to.
    """

    for key in ("GITHUB_HEAD_REF", "GITHUB_REF_NAME"):
        value = (env.get(key) or "").strip()
        if value and value != "HEAD":
            return value
    return git_branch(repo_root)


def git_branch(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    branch = completed.stdout.decode("utf-8", "replace").strip()
    return "" if branch == "HEAD" else branch


# --------------------------------------------------------------------------------------
# Transport - every failure raises, and every raise ends in a non-zero exit
# --------------------------------------------------------------------------------------


def fetch_workspaces(config: Config, opener: Any = None) -> List[Dict[str, Any]]:
    """``GET /live-workspaces``. One request carries every field the check needs."""

    request = urllib.request.Request(
        config.workspaces_url,
        headers={"Accept": "application/json", "User-Agent": "writai-ci-check/1"},
        method="GET",
    )
    if config.api_key:
        request.add_header(API_KEY_HEADER, config.api_key)

    send = opener or urllib.request.urlopen
    try:
        with send(request, timeout=config.timeout_seconds) as response:
            raw = response.read().decode("utf-8", "replace")
    except Exception as exc:  # URLError, HTTPError, timeout, TLS failure, ...
        raise ServiceUnreachable(
            "GET %s failed: %s" % (config.workspaces_url, exc)
        ) from exc

    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise ServiceUnreachable("the agent service returned a non-JSON body") from exc
    if not isinstance(parsed, dict):
        raise ServiceUnreachable("the agent service returned a non-object body")
    workspaces = parsed.get("workspaces")
    if not isinstance(workspaces, list):
        raise ServiceUnreachable("the agent service response has no 'workspaces' list")
    for index, item in enumerate(workspaces):
        if not isinstance(item, dict):
            raise MalformedServiceResponse(
                "workspace %d is %s, not an object"
                % (index, type(item).__name__)
            )
    return list(workspaces)


# --------------------------------------------------------------------------------------
# Candidates - mirrors RepositorySupervisorAssignmentGateway
# --------------------------------------------------------------------------------------


class Candidate(object):
    """One bindable assignment, plus the workspace state it is judged against."""

    def __init__(self, workspace: Dict[str, Any], assignment: Dict[str, Any]) -> None:
        self.workspace = workspace
        self.assignment = assignment

    @property
    def workspace_id(self) -> str:
        return text_of(self.workspace.get("id"))

    @property
    def assignment_id(self) -> str:
        return text_of(self.assignment.get("id"))

    @property
    def task_id(self) -> str:
        return text_of(self.assignment.get("task_id"))

    @property
    def state(self) -> str:
        return text_of(self.assignment.get("state"))

    @property
    def scopes(self) -> List[str]:
        return sorted(string_list(self.assignment.get("scopes")))

    @property
    def graph_version(self) -> str:
        return text_of(self.workspace.get("graph_version"))

    @property
    def decision_snapshot(self) -> str:
        return text_of(self.assignment.get("decision_snapshot"))


def live_claude_candidates(workspaces: Sequence[Dict[str, Any]]) -> List[Candidate]:
    """Return the bindable set, failing if it cannot be derived safely.

    A valid empty set is not an error: a null or simulated supervisor and
    definitively simulated, non-bindable, or completed assignments contribute no
    candidates. Missing or malformed discriminator fields are different. They
    make it impossible to prove that an object is outside the bindable set.
    """

    candidates = []  # type: List[Candidate]
    for workspace_index, workspace in enumerate(workspaces):
        workspace_location = "workspace[%d]" % workspace_index
        if "supervisor" not in workspace:
            raise MalformedServiceResponse(
                "%s is missing 'supervisor'" % workspace_location
            )
        supervisor = workspace["supervisor"]
        # An explicit null supervisor is normal — this workspace was never
        # authorized. A missing field is not the same state.
        if supervisor is None:
            continue
        if not isinstance(supervisor, dict):
            raise MalformedServiceResponse(
                "%s.supervisor is %s, not an object or null"
                % (workspace_location, type(supervisor).__name__)
            )
        supervisor_location = workspace_location + ".supervisor"
        supervisor_mode = required_choice(
            supervisor,
            "execution_mode",
            EXECUTION_MODES,
            supervisor_location,
        )
        assignments = supervisor.get("assignments")
        if not isinstance(assignments, list):
            raise MalformedServiceResponse(
                "%s.assignments is %s, not a list"
                % (supervisor_location, type(assignments).__name__)
            )
        if supervisor_mode != EXECUTION_MODE_LIVE:
            continue
        for assignment_index, assignment in enumerate(assignments):
            assignment_location = "%s.assignments[%d]" % (
                supervisor_location,
                assignment_index,
            )
            if not isinstance(assignment, dict):
                raise MalformedServiceResponse(
                    "%s is %s, not an object"
                    % (assignment_location, type(assignment).__name__)
                )
            assignment_mode = required_choice(
                assignment,
                "execution_mode",
                EXECUTION_MODES,
                assignment_location,
            )
            provider = required_choice(
                assignment,
                "runtime_provider",
                RUNTIME_PROVIDERS,
                assignment_location,
            )
            state = required_choice(
                assignment,
                "state",
                ASSIGNMENT_STATES,
                assignment_location,
            )
            if (
                assignment_mode != EXECUTION_MODE_LIVE
                or provider not in BINDABLE_PROVIDERS
                or state == STATE_COMPLETED
            ):
                continue
            required_text(workspace, "id", workspace_location)
            required_text(workspace, "graph_version", workspace_location)
            required_text(assignment, "id", assignment_location)
            required_text(assignment, "task_id", assignment_location)
            required_text(assignment, "decision_snapshot", assignment_location)
            required_string_list(assignment, "scopes", assignment_location)
            candidates.append(Candidate(workspace, assignment))
    candidates.sort(key=lambda item: (item.workspace_id, item.assignment_id))
    return candidates


def required_choice(
    payload: Dict[str, Any],
    field: str,
    choices: AbstractSet[str],
    location: str,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or value not in choices:
        raise MalformedServiceResponse(
            "%s has a missing or invalid '%s' discriminator" % (location, field)
        )
    return value


def required_text(payload: Dict[str, Any], field: str, location: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MalformedServiceResponse(
            "%s candidate field '%s' must be a non-empty string"
            % (location, field)
        )
    return value.strip()


def required_string_list(
    payload: Dict[str, Any],
    field: str,
    location: str,
) -> List[str]:
    value = payload.get(field)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise MalformedServiceResponse(
            "%s candidate field '%s' must be a list of non-empty strings"
            % (location, field)
        )
    return [item.strip() for item in value]


# --------------------------------------------------------------------------------------
# Binding - mirrors session_binding._resolve_binding, in the same fixed order
# --------------------------------------------------------------------------------------

SOURCE_EXPLICIT = "explicit"
SOURCE_BRANCH = "branch"
SOURCE_TASK_FILE = "task-file"
SOURCE_UNBOUND = "unbound"


class Binding(object):
    def __init__(
        self,
        source: str,
        detail: str,
        candidate: Optional[Candidate] = None,
        unresolved_explicit: bool = False,
    ) -> None:
        self.source = source
        self.detail = detail
        self.candidate = candidate
        #: The branch asked to be bound to a named assignment and that
        #: assignment does not exist. Distinct from "no binding information",
        #: because unbound passes and this must not.
        self.unresolved_explicit = unresolved_explicit

    @property
    def is_bound(self) -> bool:
        return self.candidate is not None


def resolve_binding(
    branch: str,
    repo_root: Path,
    candidates: Sequence[Candidate],
) -> Binding:
    """Explicit attachment, then branch task ID, then ``.writai/task``, then unbound.

    Binding never asks a model to infer a task, and the order is fixed. Any
    ambiguity resolves to unbound rather than to a guess - same as the registry.
    """

    attached = read_marker_file(repo_root, "attach")
    if attached is not None:
        # `writai dev attach` writes the assignment ID alone, so the match is
        # on assignment ID; a value naming more than one live assignment is
        # ambiguous and therefore unbound, not a guess.
        matches = [item for item in candidates if item.assignment_id == attached]
        if len(matches) == 1:
            return Binding(
                SOURCE_EXPLICIT,
                "Bound by an explicit writai attach selection.",
                matches[0],
            )
        # An attachment that was READ but resolves to nothing is a failure, not
        # an absence. Returning unbound here would pass by default, so writing
        # one junk `.writai/attach` would be a way to opt a branch out of a
        # required check. `unresolved_explicit` makes `evaluate` fail it
        # regardless of --require-binding.
        return Binding(
            SOURCE_UNBOUND,
            ".writai/attach does not name one available live assignment.",
            unresolved_explicit=True,
        )

    branch_matches = [
        item for item in candidates if branch_mentions_task(branch, item.task_id)
    ]
    if len(branch_matches) == 1:
        return Binding(
            SOURCE_BRANCH,
            "Bound by an exact task ID in the Git branch name.",
            branch_matches[0],
        )
    if len(branch_matches) > 1:
        return Binding(
            SOURCE_UNBOUND,
            "The Git branch name matches more than one assignment.",
        )

    task_id = read_marker_file(repo_root, "task")
    if task_id is not None:
        task_matches = [item for item in candidates if item.task_id == task_id]
        if len(task_matches) == 1:
            return Binding(
                SOURCE_TASK_FILE,
                "Bound by the repository .writai/task file.",
                task_matches[0],
            )
        if len(task_matches) > 1:
            return Binding(
                SOURCE_UNBOUND,
                ".writai/task matches assignments in multiple workspaces.",
            )
        return Binding(
            SOURCE_UNBOUND,
            ".writai/task does not name an available live assignment.",
        )

    return Binding(
        SOURCE_UNBOUND,
        "No explicit attachment, branch task ID, or .writai/task binding was "
        "found; this branch is visibly unbound.",
    )


def branch_mentions_task(branch: str, task_id: str) -> bool:
    """Exact token match, byte-for-byte the registry's rule.

    The boundaries matter: ``TASK-10`` must not bind a branch named for
    ``TASK-102``.
    """

    if not branch or not task_id:
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(task_id) + r"(?![A-Za-z0-9])"
    return re.search(pattern, branch) is not None


def read_marker_file(repo_root: Path, name: str) -> Optional[str]:
    """``.writai/<name>``: one non-empty line, small, a regular file.

    A symlink is refused for the same reason the service refuses one - the
    marker names a binding, and a link makes the named thing unverifiable.
    """

    path = repo_root / ".writai" / name
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return None
        if metadata.st_size > MAX_MARKER_FILE_BYTES:
            return None
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0]) > MAX_MARKER_VALUE_CHARS:
        return None
    return lines[0]


# --------------------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------------------


class Outcome(object):
    """A deterministic check result plus everything needed to explain it."""

    def __init__(
        self,
        ok: bool,
        code: str,
        headline: str,
        detail: str = "",
        exit_code: int = EXIT_OK,
        still_valid: Optional[List[str]] = None,
        no_longer: Optional[List[str]] = None,
        now_required: Optional[List[str]] = None,
        scopes: Optional[List[str]] = None,
        provenance: Optional[List[str]] = None,
        evidence_ref: str = "",
        attribution: str = "",
        decision_text: str = "",
        notes: Optional[List[str]] = None,
        binding: Optional[Binding] = None,
    ) -> None:
        self.ok = ok
        self.code = code
        self.headline = headline
        self.detail = detail
        self.exit_code = exit_code
        self.still_valid = still_valid or []
        self.no_longer = no_longer or []
        self.now_required = now_required or []
        self.scopes = scopes or []
        self.provenance = provenance or []
        self.evidence_ref = evidence_ref
        self.attribution = attribution
        self.decision_text = decision_text
        self.notes = notes or []
        self.binding = binding

    def as_dict(self) -> Dict[str, Any]:
        binding = self.binding
        candidate = binding.candidate if binding is not None else None
        return {
            "ok": self.ok,
            "code": self.code,
            "headline": self.headline,
            "detail": self.detail,
            "exit_code": self.exit_code,
            "binding_source": binding.source if binding is not None else None,
            "binding_detail": binding.detail if binding is not None else None,
            "workspace_id": candidate.workspace_id if candidate else None,
            "assignment_id": candidate.assignment_id if candidate else None,
            "task_id": candidate.task_id if candidate else None,
            "assignment_state": candidate.state if candidate else None,
            "still_valid": self.still_valid,
            "no_longer": self.no_longer,
            "now_required": self.now_required,
            "affected_scopes": self.scopes,
            "provenance_path": self.provenance,
            "evidence_ref": self.evidence_ref or None,
            "notes": self.notes,
        }


def evaluate(
    binding: Binding,
    config: Config,
    now: Optional[datetime] = None,
) -> Outcome:
    """The gate. Ordering here is load-bearing and mirrors the service exactly."""

    moment = now or datetime.now(timezone.utc)

    if not binding.is_bound:
        # An explicit attachment that resolves to nothing fails whatever
        # --require-binding says. Passing it would make `.writai/attach` an
        # opt-out from a required check: write a junk assignment id, resolve to
        # unbound, merge.
        if binding.unresolved_explicit:
            return Outcome(
                ok=False,
                code="UNRESOLVED_ATTACHMENT",
                headline="this branch names an assignment that does not exist",
                detail=binding.detail,
                exit_code=EXIT_UNAUTHORIZED,
                now_required=[
                    "Correct .writai/attach to name a live assignment, or "
                    "delete it and let the branch name or .writai/task bind."
                ],
                binding=binding,
            )
        if config.require_binding:
            return Outcome(
                ok=False,
                code="UNBOUND",
                headline="this branch resolves to no writ.ai assignment",
                detail=binding.detail,
                exit_code=EXIT_UNAUTHORIZED,
                now_required=[
                    "Name the task in the branch, or run `writai dev attach "
                    "<assignment-id>` and commit .writai/task."
                ],
                binding=binding,
            )
        return Outcome(
            ok=True,
            code="UNBOUND",
            headline="no writ.ai assignment governs this branch",
            detail=binding.detail,
            binding=binding,
            notes=[
                "Nothing was invalidated because nothing was bound. Run with "
                "--require-binding to treat an unbound branch as a failure."
            ],
        )

    candidate = binding.candidate
    assert candidate is not None
    workspace = candidate.workspace
    report = invalidation_report(workspace)
    narrative = build_narrative(workspace, report, candidate)

    # 1. Interrupted. The graph traversal already decided that this task's
    #    scopes intersect an approved change.
    if candidate.state == STATE_INTERRUPTED:
        return Outcome(
            ok=False,
            code="ASSIGNMENT_INTERRUPTED",
            headline="the decision behind this branch changed",
            detail=text_of(candidate.assignment.get("interrupt_reason"))
            or "writ.ai invalidated this task through the recorded provenance path.",
            exit_code=EXIT_UNAUTHORIZED,
            binding=binding,
            **narrative
        )

    # 2. Preserved. CONTINUING is checked BEFORE the snapshot comparison, and
    #    must stay that way: `_apply_supervisor_invalidation` moves a preserved
    #    sibling RUNNING -> CONTINUING *without* advancing its decision
    #    snapshot, so a snapshot-first order would fail exactly the tasks the
    #    scope intersection deliberately spared. Out-of-scope siblings survive;
    #    invalidation is never blanket-propagated.
    if candidate.state == STATE_CONTINUING:
        return Outcome(
            ok=True,
            code="PRESERVED",
            headline="this task's scopes do not intersect the approved change",
            detail=(
                "writ.ai preserved this assignment; its snapshot is intentionally "
                "behind the workspace graph version."
            ),
            binding=binding,
            still_valid=narrative["still_valid"],
            scopes=narrative["scopes"],
        )

    # 3. Snapshot staleness on the assignment itself.
    if candidate.decision_snapshot != candidate.graph_version:
        return Outcome(
            ok=False,
            code="STALE_ASSIGNMENT_SNAPSHOT",
            headline="this branch is bound to a superseded decision snapshot",
            detail=(
                "The assignment is bound to snapshot %s; the workspace is now at %s."
                % (
                    candidate.decision_snapshot or "(none)",
                    candidate.graph_version or "(none)",
                )
            ),
            exit_code=EXIT_UNAUTHORIZED,
            binding=binding,
            **narrative
        )

    # 4. Grant staleness. The grant binds the ticket and the plan, not one
    #    assignment (`orchestrator` authorizes with `task_id=ticket.id` and the
    #    workspace run id, while assignments carry per-task run ids), so it is
    #    matched at the workspace level and never by comparing run IDs.
    stale_grant = evaluate_grant(binding, workspace, narrative, moment, config)
    if stale_grant is not None:
        return stale_grant

    return Outcome(
        ok=True,
        code="AUTHORIZED",
        headline="this branch is bound to the current approved decision snapshot",
        detail="Assignment %s (task %s) is current at %s."
        % (candidate.assignment_id, candidate.task_id, candidate.graph_version),
        binding=binding,
        scopes=candidate.scopes,
    )


def evaluate_grant(
    binding: Binding,
    workspace: Dict[str, Any],
    narrative: Dict[str, Any],
    moment: datetime,
    config: Config,
) -> Optional[Outcome]:
    """Fail on a stale, expired, or non-ALLOW grant. ``None`` means the grant is fine."""

    grant = operative_grant(workspace)
    if grant is None:
        if config.require_grant:
            return Outcome(
                ok=False,
                code="NO_GRANT",
                headline="no authorization grant is on record for this workspace",
                detail=(
                    "The workspace has issued no grant, so nothing authorizes this "
                    "run. Pass --allow-missing-grant to treat that as acceptable."
                ),
                exit_code=EXIT_UNAUTHORIZED,
                binding=binding,
                now_required=["Authorize the workspace, then re-run this check."],
                **without(narrative, "now_required")
            )
        return None

    graph_version = text_of(workspace.get("graph_version"))
    snapshot = text_of(grant.get("decision_snapshot"))
    if snapshot != graph_version:
        return Outcome(
            ok=False,
            code="STALE_GRANT_SNAPSHOT",
            headline="the grant covering this branch is bound to a superseded snapshot",
            detail=(
                "Grant %s is bound to snapshot %s; the workspace is now at %s. A "
                "snapshot-bound grant does not survive the decision it was issued "
                "under."
                % (
                    text_of(grant.get("authorization_id")) or "(unknown)",
                    snapshot or "(none)",
                    graph_version or "(none)",
                )
            ),
            exit_code=EXIT_UNAUTHORIZED,
            binding=binding,
            **narrative
        )

    verdict = text_of(grant.get("verdict"))
    if verdict != VERDICT_ALLOW:
        return Outcome(
            ok=False,
            code="GRANT_NOT_ALLOW",
            headline="the grant covering this branch is not an ALLOW",
            detail="The operative grant carries verdict %s." % (verdict or "(none)"),
            exit_code=EXIT_UNAUTHORIZED,
            binding=binding,
            **narrative
        )

    expires_at = parse_datetime(grant.get("expires_at"))
    if expires_at is None:
        return Outcome(
            ok=False,
            code="GRANT_UNREADABLE",
            headline="the grant covering this branch has no readable expiry",
            detail=(
                "expires_at was missing or unparseable, so the grant cannot be "
                "shown to be current. Failing closed."
            ),
            exit_code=EXIT_UNAUTHORIZED,
            binding=binding,
            **narrative
        )
    if expires_at <= moment:
        return Outcome(
            ok=False,
            code="GRANT_EXPIRED",
            headline="the grant covering this branch has expired",
            detail="The operative grant expired at %s." % iso(expires_at),
            exit_code=EXIT_UNAUTHORIZED,
            binding=binding,
            **narrative
        )
    return None


def operative_grant(workspace: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The grant currently answering for this workspace: the most recently issued.

    Ties break toward the later stage - a replacement grant issued in the same
    second as the initial one is the one that answers.
    """

    ranked = []  # type: List[Tuple[datetime, int, Dict[str, Any]]]
    stages = (
        "initial_authorization",
        "conflict_authorization",
        "replacement_authorization",
    )
    for rank, key in enumerate(stages):
        view = workspace.get(key)
        if not isinstance(view, dict):
            continue
        grant = view.get("grant")
        if not isinstance(grant, dict):
            continue
        issued = parse_datetime(grant.get("issued_at"))
        if issued is None:
            issued = datetime.min.replace(tzinfo=timezone.utc)
        ranked.append((issued, rank, grant))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]))
    return ranked[-1][2]


def build_narrative(
    workspace: Dict[str, Any],
    report: Dict[str, Any],
    candidate: Candidate,
) -> Dict[str, Any]:
    """What is still valid, what is not, what is now required - and why.

    ``invalidated_task_ids`` feeds the explanation only. It is never a gate:
    nothing clears a downgraded validity, so a task that was invalidated,
    redirected and re-authorized still appears in that list forever. The gate is
    snapshot equality; this is the prose.
    """

    still_valid = string_list(report.get("preserved_task_ids"))
    still_valid += [
        item
        for item in string_list(report.get("preserved_artifact_ids"))
        if item not in still_valid
    ]
    no_longer = string_list(report.get("invalidated_task_ids"))
    no_longer += [
        item
        for item in string_list(report.get("affected_artifact_ids"))
        if item not in no_longer
    ]

    now_required = []  # type: List[str]
    redirect = text_of(candidate.assignment.get("redirect_instruction"))
    if redirect:
        now_required.append(redirect)

    scopes = sorted(string_list(report.get("affected_scopes")))
    intersecting = sorted(set(scopes) & set(candidate.scopes))
    scope_row = intersecting or scopes or candidate.scopes

    evidence_refs = string_list(report.get("evidence_refs"))
    if evidence_refs:
        evidence_ref = evidence_refs[0]
    else:
        evidence_ref = "writai://workspaces/%s/assignments/%s" % (
            candidate.workspace_id,
            candidate.assignment_id,
        )

    attribution, decision_text = approval_attribution(workspace)
    return {
        "still_valid": still_valid,
        "no_longer": no_longer,
        "now_required": now_required,
        "scopes": scope_row,
        "provenance": provenance_path(workspace, report, candidate),
        "evidence_ref": evidence_ref,
        "attribution": attribution,
        "decision_text": decision_text,
    }


def provenance_path(
    workspace: Dict[str, Any],
    report: Dict[str, Any],
    candidate: Candidate,
) -> List[str]:
    """The recorded traversal, preferred in the order the product records it."""

    recorded = string_list(candidate.assignment.get("provenance_path"))
    if recorded:
        return recorded

    paths = report.get("paths")
    if isinstance(paths, list):
        for entry in paths:
            if not isinstance(entry, dict):
                continue
            if text_of(entry.get("artifact_id")) == candidate.task_id:
                nodes = string_list(entry.get("node_ids"))
                if nodes:
                    return nodes

    stages = (
        "replacement_authorization",
        "conflict_authorization",
        "initial_authorization",
    )
    for key in stages:
        view = workspace.get(key)
        if isinstance(view, dict):
            nodes = string_list(view.get("invalidation_path"))
            if nodes:
                return nodes
    return []


def approval_attribution(workspace: Dict[str, Any]) -> Tuple[str, str]:
    """``Approved by <role>`` and the decision text - never invented.

    A fabricated approver on the most-read line in the product is worse than a
    missing one, so anything absent stays absent.
    """

    mutations = workspace.get("approved_mutations")
    if not isinstance(mutations, list) or not mutations:
        return "", ""
    latest = mutations[-1]
    if not isinstance(latest, dict):
        return "", ""

    role = text_of(latest.get("actor_role"))
    when = ""
    evidence = latest.get("approval_evidence")
    if isinstance(evidence, dict):
        approved_at = parse_datetime(evidence.get("approved_at"))
        if approved_at is not None:
            when = iso(approved_at)

    decision_text = ""
    mutation = latest.get("mutation")
    if isinstance(mutation, dict):
        decision = mutation.get("decision")
        if isinstance(decision, dict):
            decision_text = text_of(decision.get("title")) or text_of(
                decision.get("text")
            )

    if role and when:
        attribution = "Approved by %s · %s:" % (role, when)
    elif role:
        attribution = "Approved by %s:" % role
    elif when:
        attribution = "Approved %s:" % when
    else:
        attribution = ""
    return attribution, decision_text


def invalidation_report(workspace: Dict[str, Any]) -> Dict[str, Any]:
    report = workspace.get("invalidation_report")
    return report if isinstance(report, dict) else {}


def without(mapping: Dict[str, Any], key: str) -> Dict[str, Any]:
    return dict((name, value) for name, value in mapping.items() if name != key)


# --------------------------------------------------------------------------------------
# Rendering - `docs/TERMINAL_OUTPUT_SPEC.md` section 1, the block the hook prints
# --------------------------------------------------------------------------------------


def render(outcome: Outcome, config: Config) -> str:
    lines = []  # type: List[str]
    leader = LEADER_OK if outcome.ok else LEADER_STOPPED
    lines.append(INDENT + leader + "  WRITAI — " + outcome.headline)

    candidate = outcome.binding.candidate if outcome.binding else None
    lines.append("")
    lines.append(BODY + "Branch".ljust(LABEL_WIDTH) + config.branch)
    if candidate is not None:
        lines.append(
            BODY
            + "Task".ljust(LABEL_WIDTH)
            + "%s · assignment %s · workspace %s"
            % (candidate.task_id, candidate.assignment_id, candidate.workspace_id)
        )
    if outcome.binding is not None:
        lines.append(
            BODY
            + "Bound via".ljust(LABEL_WIDTH)
            + "%s — %s" % (outcome.binding.source, outcome.binding.detail)
        )

    if outcome.attribution or outcome.decision_text:
        lines.append("")
        if outcome.attribution:
            lines.append(BODY + outcome.attribution)
        if outcome.decision_text:
            lines.append(BODY + '"' + one_line(outcome.decision_text, 400) + '"')

    # Still valid comes first, deliberately: leading with what survived is the
    # difference between an agent that adapts and one that starts over.
    rows = [
        ("Still valid", outcome.still_valid, MAX_ROW_ITEM_CHARS),
        ("No longer", outcome.no_longer, MAX_ROW_ITEM_CHARS),
        ("Now required", outcome.now_required, MAX_INSTRUCTION_CHARS),
        ("Scopes", outcome.scopes, MAX_ROW_ITEM_CHARS),
    ]
    if any(values for _label, values, _limit in rows):
        lines.append("")
        for label, values, limit in rows:
            if values:
                lines.append(
                    BODY + label.ljust(LABEL_WIDTH) + row_text(values, limit)
                )

    # The binding detail is already printed on the "Bound via" row; repeating it
    # verbatim under "Detail" is noise on the unbound path.
    binding_detail = outcome.binding.detail if outcome.binding is not None else ""
    if outcome.detail and outcome.detail != binding_detail:
        lines.append("")
        lines.append(
            BODY + "Detail".ljust(LABEL_WIDTH) + one_line(outcome.detail, 600)
        )

    if outcome.provenance:
        lines.append("")
        lines.append(
            BODY
            + "Why".ljust(5)
            + compact_provenance(outcome.provenance)
            + ARROW
            + "this branch"
        )
        lines.append(BODY + "".ljust(5) + "writai dev why")
    if outcome.evidence_ref and not outcome.ok:
        lines.append(BODY + "".ljust(5) + one_line(outcome.evidence_ref, 200))

    for note in outcome.notes:
        lines.append("")
        lines.append(BODY + one_line(note, 400))

    if not outcome.ok:
        lines.append("")
        lines.append(
            BODY
            + "This check is the backstop for a hook that fails open. It does not "
            "pass until the branch is re-authorized against the current decision "
            "snapshot."
        )
    return "\n".join(lines)


def row_text(values: Sequence[str], limit: int = MAX_ROW_ITEM_CHARS) -> str:
    shown = [one_line(value, limit) for value in values[:MAX_ROW_ITEMS]]
    hidden = len(values) - len(shown)
    if hidden > 0:
        shown.append("...(%d more)" % hidden)
    return ", ".join(shown)


def compact_provenance(nodes: Sequence[str]) -> str:
    cleaned = [one_line(node, 120) for node in nodes if node]
    if len(cleaned) <= MAX_PROVENANCE_NODES:
        return ARROW.join(cleaned)
    head = cleaned[: MAX_PROVENANCE_NODES - 5]
    tail = cleaned[-5:]
    hidden = len(cleaned) - len(head) - len(tail)
    return ARROW.join(head + ["...(%d more truncated)..." % hidden] + tail)


def one_line(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 14)] + " ...[truncated]"


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def text_of(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def string_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            item.strip() for item in value if isinstance(item, str) and item.strip()
        ]
    return []


def parse_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def write_step_summary(text: str, env: Dict[str, str]) -> None:
    """Mirror the block into the GitHub job summary. Never changes the verdict."""

    path = (env.get("GITHUB_STEP_SUMMARY") or "").strip()
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("## writ.ai authorization\n\n```\n")
            handle.write(text)
            handle.write("\n```\n")
    except Exception:
        pass


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="writai-ci-check",
        description=(
            "Fail a pull request whose branch is bound to an invalidated task or "
            "a stale grant. Fails closed: an unreachable service is a failure."
        ),
    )
    parser.add_argument(
        "--agent-url", default=None, help="Base URL of the agent service."
    )
    parser.add_argument("--branch", default=None, help="Branch under review.")
    parser.add_argument(
        "--repo-root", default=None, help="Checkout to read markers from."
    )
    parser.add_argument(
        "--timeout", type=float, default=None, help="HTTP timeout, seconds."
    )
    parser.add_argument(
        "--require-binding",
        action="store_true",
        help="Fail when the branch resolves to no assignment (default: pass).",
    )
    parser.add_argument(
        "--require-grant",
        action="store_true",
        help=argparse.SUPPRESS,  # now the default; kept so existing callers still parse.
    )
    parser.add_argument(
        "--allow-missing-grant",
        action="store_true",
        help=(
            "Pass a bound branch whose workspace has issued no grant at all "
            "(default: fail). Only reaches a branch that resolves to a live "
            "assignment; an unbound branch passes either way."
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON."
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    env: Optional[Dict[str, str]] = None,
) -> int:
    environment = dict(os.environ if env is None else env)
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    try:
        config = build_config(args, environment)
    except UsageError as exc:
        sys.stderr.write("writai: %s\n" % exc)
        return EXIT_USAGE

    try:
        workspaces = fetch_workspaces(config)
        binding = resolve_binding(
            config.branch,
            config.repo_root,
            live_claude_candidates(workspaces),
        )
        outcome = evaluate(binding, config)
    except MalformedServiceResponse as exc:
        # The service answered, so "could not be reached" would be misleading.
        # It still fails: a malformed workspace or assignment silently dropped
        # can empty the candidate set and PASS a branch nobody evaluated.
        outcome = Outcome(
            ok=False,
            code="MALFORMED_SERVICE_RESPONSE",
            headline="the agent service returned a response this check cannot trust",
            detail=str(exc),
            exit_code=EXIT_UNREACHABLE,
            now_required=[
                "Fix the agent service response, then re-run this check. "
                "Dropping the malformed part and carrying on would resolve this "
                "branch to unbound, which PASSES — so it fails instead."
            ],
        )
    except ServiceUnreachable as exc:
        outcome = Outcome(
            ok=False,
            code="SERVICE_UNREACHABLE",
            headline="the authority service could not be reached — failing closed",
            detail=str(exc),
            exit_code=EXIT_UNREACHABLE,
            now_required=[
                "Restore the writ.ai agent service, then re-run this check. "
                "Unlike the PreToolUse hook, this gate has no cached-verdict "
                "fallback: an outage must not authorize a merge."
            ],
        )
    except Exception as exc:  # a bug here must not become a silent pass
        outcome = Outcome(
            ok=False,
            code="CHECK_ERROR",
            headline="the authorization check itself failed — failing closed",
            detail="%s: %s" % (type(exc).__name__, exc),
            exit_code=EXIT_UNREACHABLE,
        )

    rendered = render(outcome, config)
    if args.json:
        sys.stdout.write(json.dumps(outcome.as_dict(), indent=2) + "\n")
    else:
        sys.stdout.write(rendered + "\n")
    write_step_summary(rendered, environment)
    return EXIT_OK if outcome.ok else outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
