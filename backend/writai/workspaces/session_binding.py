"""Deterministic Claude Code session-to-assignment binding."""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field

from writai.domain import utc_now
from writai.workspaces.supervisor import SupervisorAssignment

_MAX_TASK_FILE_BYTES = 512


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class SessionBindingSource(StrEnum):
    EXPLICIT = "explicit"
    BRANCH = "branch"
    TASK_FILE = "task-file"
    UNBOUND = "unbound"
    #: The session asked to be bound to a named assignment and that assignment
    #: does not exist, or names more than one. Distinct from UNBOUND: unbound is
    #: "no binding information, so nothing is enforced", which is permissive.
    #: This is "you asked for supervision and it could not be arranged", and
    #: answering that with a session allowed everything would make one junk
    #: marker file an off switch.
    UNRESOLVED_ATTACHMENT = "unresolved-attachment"


class SupervisorAssignmentTarget(_FrozenModel):
    """One assignment address plus the state used during binding."""

    workspace_id: str = Field(min_length=1, max_length=255)
    assignment: SupervisorAssignment

    @property
    def assignment_id(self) -> str:
        return self.assignment.id

    @property
    def task_id(self) -> str:
        return self.assignment.task_id


class AssignmentLocator(_FrozenModel):
    workspace_id: str = Field(min_length=1, max_length=255)
    assignment_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)

    @classmethod
    def from_target(
        cls,
        target: SupervisorAssignmentTarget,
    ) -> AssignmentLocator:
        return cls(
            workspace_id=target.workspace_id,
            assignment_id=target.assignment_id,
            task_id=target.task_id,
        )


class ExplicitSessionAttachment(_FrozenModel):
    workspace_id: str = Field(min_length=1, max_length=255)
    assignment_id: str = Field(min_length=1, max_length=255)


@dataclass(frozen=True)
class _AttachFileResult:
    present: bool
    assignment_id: str | None = None
    detail: str | None = None


class ClaudeCodeSessionBinding(_FrozenModel):
    """Visible binding state; unbound is an intentional successful outcome."""

    session_id: str = Field(min_length=1, max_length=255)
    cwd: str = Field(min_length=1, max_length=4096)
    branch: str = Field(default="", max_length=1024)
    source: SessionBindingSource
    assignment: AssignmentLocator | None = None
    detail: str = Field(min_length=1, max_length=1000)
    started_at: datetime = Field(default_factory=utc_now)

    @property
    def is_bound(self) -> bool:
        return self.assignment is not None


class ClaudeCodeSessionRegistry:
    """Thread-safe, process-local live-session registry.

    Binding never asks a model to infer a task. Resolution order is fixed:
    explicit attachment, task ID in the branch, ``.writai/task``, unbound.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ClaudeCodeSessionBinding] = {}
        self._explicit_by_cwd: dict[str, ExplicitSessionAttachment] = {}
        self._acknowledged_interrupts: set[tuple[str, str]] = set()
        self._lock = RLock()

    def attach(
        self,
        *,
        cwd: str | Path,
        workspace_id: str,
        assignment_id: str,
    ) -> ExplicitSessionAttachment:
        attachment = ExplicitSessionAttachment(
            workspace_id=workspace_id,
            assignment_id=assignment_id,
        )
        with self._lock:
            self._explicit_by_cwd[_canonical_cwd(cwd)] = attachment
        return attachment

    def detach(self, *, cwd: str | Path) -> bool:
        with self._lock:
            return self._explicit_by_cwd.pop(
                _canonical_cwd(cwd),
                None,
            ) is not None

    def register(
        self,
        *,
        session_id: str,
        cwd: str | Path,
        branch: str,
        candidates: list[SupervisorAssignmentTarget],
    ) -> ClaudeCodeSessionBinding:
        normalized_cwd = _canonical_cwd(cwd)
        unique_candidates = _unique_targets(candidates)
        with self._lock:
            attachment = self._explicit_by_cwd.get(normalized_cwd)
            file_attachment = _read_attach_file(normalized_cwd)
            # The in-memory seam wins when both exist: it is workspace-qualified
            # and explicitly installed by the embedding service, while the marker
            # file is attacker-adjacent input carrying only an assignment ID.
            binding = _resolve_binding(
                session_id=session_id,
                cwd=normalized_cwd,
                branch=branch.strip(),
                candidates=unique_candidates,
                attachment=attachment,
                file_attachment=file_attachment,
            )
            self._sessions[binding.session_id] = binding
            return binding.model_copy(deep=True)

    def get(self, session_id: str) -> ClaudeCodeSessionBinding | None:
        with self._lock:
            binding = self._sessions.get(session_id.strip())
            return binding.model_copy(deep=True) if binding is not None else None

    def list(self) -> tuple[ClaudeCodeSessionBinding, ...]:
        with self._lock:
            return tuple(
                binding.model_copy(deep=True)
                for binding in sorted(
                    self._sessions.values(),
                    key=lambda item: (item.cwd, item.session_id),
                )
            )

    def release(self, session_id: str) -> bool:
        normalized = session_id.strip()
        with self._lock:
            binding = self._sessions.pop(normalized, None)
            if binding is None:
                return False
            self._acknowledged_interrupts = {
                key
                for key in self._acknowledged_interrupts
                if key[0] != normalized
            }
            return True

    def acknowledge(self, *, session_id: str, interrupt_key: str) -> None:
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(session_id)
            self._acknowledged_interrupts.add((session_id, interrupt_key))

    def is_acknowledged(
        self,
        *,
        session_id: str,
        interrupt_key: str,
    ) -> bool:
        with self._lock:
            return (session_id, interrupt_key) in self._acknowledged_interrupts


def _resolve_binding(
    *,
    session_id: str,
    cwd: str,
    branch: str,
    candidates: list[SupervisorAssignmentTarget],
    attachment: ExplicitSessionAttachment | None,
    file_attachment: _AttachFileResult,
) -> ClaudeCodeSessionBinding:
    if attachment is not None:
        matches = [
            target
            for target in candidates
            if target.workspace_id == attachment.workspace_id
            and target.assignment_id == attachment.assignment_id
        ]
        if len(matches) == 1:
            return _bound(
                session_id=session_id,
                cwd=cwd,
                branch=branch,
                source=SessionBindingSource.EXPLICIT,
                target=matches[0],
                detail="Bound by an explicit writai attach selection.",
            )
        return _unresolved_attachment(
            session_id=session_id,
            cwd=cwd,
            branch=branch,
            detail=(
                "The explicit attachment does not name one available live "
                "Claude Code assignment."
            ),
        )

    # An UNREADABLE marker falls through to the remaining rules rather than
    # stopping here. Stopping would resolve to UNBOUND, and an unbound session
    # is allowed everything — so anyone who can write the working directory
    # could strip enforcement from a session by leaving a corrupt attach file,
    # which is a worse outcome than the marker being ignored. The reason is
    # carried into the binding detail instead of being swallowed, so
    # `writai dev why` says the marker was skipped and why. This also matches
    # `scripts/ci/writai_ci_check.py`, whose `read_marker_file` returns None
    # for the same cases and falls through; the two must not diverge.
    #
    # A marker that is READ successfully but names nothing usable still stops:
    # there the developer's instruction was understood and simply does not
    # resolve, and binding them to something they did not ask for would be a
    # guess.
    skipped_marker = (
        file_attachment.detail
        if file_attachment.present and file_attachment.assignment_id is None
        else None
    )

    if file_attachment.present and file_attachment.assignment_id is not None:
        matches = [
            target
            for target in candidates
            if target.assignment_id == file_attachment.assignment_id
        ]
        if len(matches) == 1:
            return _bound(
                session_id=session_id,
                cwd=cwd,
                branch=branch,
                source=SessionBindingSource.EXPLICIT,
                target=matches[0],
                detail="Bound by the repository .writai/attach file.",
            )
        if len(matches) > 1:
            return _unresolved_attachment(
                session_id=session_id,
                cwd=cwd,
                branch=branch,
                detail=(
                    ".writai/attach matches assignments in multiple "
                    "workspaces."
                ),
            )
        return _unresolved_attachment(
            session_id=session_id,
            cwd=cwd,
            branch=branch,
            detail=(
                ".writai/attach does not name an available live "
                "assignment."
            ),
        )

    binding = _resolve_by_branch_then_task_file(
        session_id=session_id,
        cwd=cwd,
        branch=branch,
        candidates=candidates,
    )
    if skipped_marker is None:
        return binding
    return binding.model_copy(
        update={"detail": _with_skipped_marker(binding.detail, skipped_marker)}
    )


def _with_skipped_marker(detail: str, reason: str) -> str:
    """Say that the attach marker was ignored, rather than swallowing it."""

    combined = f"{detail} (.writai/attach was ignored: {reason})"
    return combined if len(combined) <= 1000 else combined[:997] + "..."


def _resolve_by_branch_then_task_file(
    *,
    session_id: str,
    cwd: str,
    branch: str,
    candidates: list[SupervisorAssignmentTarget],
) -> ClaudeCodeSessionBinding:
    """The rules after the explicit slot: branch task ID, then ``.writai/task``."""

    branch_matches = [
        target
        for target in candidates
        if _branch_mentions_task(branch, target.task_id)
    ]
    if len(branch_matches) == 1:
        return _bound(
            session_id=session_id,
            cwd=cwd,
            branch=branch,
            source=SessionBindingSource.BRANCH,
            target=branch_matches[0],
            detail="Bound by an exact task ID in the Git branch name.",
        )
    if len(branch_matches) > 1:
        return _unbound(
            session_id=session_id,
            cwd=cwd,
            branch=branch,
            detail="The Git branch name matches more than one assignment.",
        )

    task_id = _read_task_file(cwd)
    if task_id is not None:
        task_matches = [
            target for target in candidates if target.task_id == task_id
        ]
        if len(task_matches) == 1:
            return _bound(
                session_id=session_id,
                cwd=cwd,
                branch=branch,
                source=SessionBindingSource.TASK_FILE,
                target=task_matches[0],
                detail="Bound by the repository .writai/task file.",
            )
        if len(task_matches) > 1:
            return _unbound(
                session_id=session_id,
                cwd=cwd,
                branch=branch,
                detail=".writai/task matches assignments in multiple workspaces.",
            )
        return _unbound(
            session_id=session_id,
            cwd=cwd,
            branch=branch,
            detail=".writai/task does not name an available live assignment.",
        )

    return _unbound(
        session_id=session_id,
        cwd=cwd,
        branch=branch,
        detail=(
            "No explicit attachment, branch task ID, or .writai/task binding "
            "was found; this session is visibly unbound."
        ),
    )


def _bound(
    *,
    session_id: str,
    cwd: str,
    branch: str,
    source: SessionBindingSource,
    target: SupervisorAssignmentTarget,
    detail: str,
) -> ClaudeCodeSessionBinding:
    return ClaudeCodeSessionBinding(
        session_id=session_id,
        cwd=cwd,
        branch=branch,
        source=source,
        assignment=AssignmentLocator.from_target(target),
        detail=detail,
    )


def _unresolved_attachment(
    *,
    session_id: str,
    cwd: str,
    branch: str,
    detail: str,
) -> ClaudeCodeSessionBinding:
    """An explicit attachment that could not be resolved. `check` denies this.

    Deliberately NOT unbound. An unbound session is allowed everything, so
    returning unbound here would let anyone who can write the working directory
    switch enforcement off by naming an assignment that does not exist.
    """

    return ClaudeCodeSessionBinding(
        session_id=session_id,
        cwd=cwd,
        branch=branch,
        source=SessionBindingSource.UNRESOLVED_ATTACHMENT,
        detail=detail,
    )


def _unbound(
    *,
    session_id: str,
    cwd: str,
    branch: str,
    detail: str,
) -> ClaudeCodeSessionBinding:
    return ClaudeCodeSessionBinding(
        session_id=session_id,
        cwd=cwd,
        branch=branch,
        source=SessionBindingSource.UNBOUND,
        detail=detail,
    )


def _branch_mentions_task(branch: str, task_id: str) -> bool:
    if not branch:
        return False
    pattern = rf"(?<![A-Za-z0-9]){re.escape(task_id)}(?![A-Za-z0-9])"
    return re.search(pattern, branch) is not None


def _read_task_file(cwd: str) -> str | None:
    path = Path(cwd) / ".writai" / "task"
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return None
        if metadata.st_size > _MAX_TASK_FILE_BYTES:
            return None
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0]) > 255:
        return None
    return lines[0]


def _read_attach_file(cwd: str) -> _AttachFileResult:
    path = Path(cwd) / ".writai" / "attach"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _AttachFileResult(present=False)
    except OSError:
        return _AttachFileResult(
            present=True,
            detail=".writai/attach could not be read safely.",
        )
    if stat.S_ISLNK(metadata.st_mode):
        return _AttachFileResult(
            present=True,
            detail=".writai/attach must not be a symbolic link.",
        )
    if not stat.S_ISREG(metadata.st_mode):
        return _AttachFileResult(
            present=True,
            detail=".writai/attach must be a regular file.",
        )
    if metadata.st_size > _MAX_TASK_FILE_BYTES:
        return _AttachFileResult(
            present=True,
            detail=".writai/attach exceeds the safe size limit.",
        )
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return _AttachFileResult(
            present=True,
            detail=".writai/attach could not be read safely.",
        )
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) != 1:
        return _AttachFileResult(
            present=True,
            detail=".writai/attach must contain exactly one assignment ID.",
        )
    if len(lines[0]) > 255:
        return _AttachFileResult(
            present=True,
            detail=".writai/attach contains an invalid assignment ID.",
        )
    return _AttachFileResult(present=True, assignment_id=lines[0])


def _canonical_cwd(cwd: str | Path) -> str:
    raw = str(cwd).strip()
    if not raw:
        raise ValueError("cwd must not be blank")
    return str(Path(raw).expanduser().resolve(strict=False))


def _unique_targets(
    candidates: list[SupervisorAssignmentTarget],
) -> list[SupervisorAssignmentTarget]:
    unique: dict[tuple[str, str], SupervisorAssignmentTarget] = {}
    for target in candidates:
        key = (target.workspace_id, target.assignment_id)
        previous = unique.get(key)
        if previous is not None and previous != target:
            raise ValueError(
                "assignment catalog returned conflicting values for "
                f"{target.workspace_id}/{target.assignment_id}"
            )
        unique[key] = target
    return [
        unique[key]
        for key in sorted(unique)
    ]
