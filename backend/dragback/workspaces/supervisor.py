from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from dragback.domain import (
    AgentPlan,
    Artifact,
    PlanAction,
    ValidityStatus,
)

_MAX_AUTHORIZED_ACTIONS = 20
_MAX_AUTHORIZED_ACTION_LENGTH = 1000


class SupervisorExecutionMode(StrEnum):
    """SIMULATED never controls a provider process; LIVE records state a real hook reads."""

    SIMULATED = "simulated"
    LIVE = "live"


class SupervisorRuntimeProvider(StrEnum):
    GENERIC = "generic"
    CODEX = "codex"
    CLAUDE_CODE = "claude-code"


class SupervisorAssignmentState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CONTINUING = "continuing"
    INTERRUPTED = "interrupted"
    REDIRECTED = "redirected"
    RESUMED = "resumed"
    COMPLETED = "completed"


class SupervisorLifecycleState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTING = "interrupting"
    REDIRECTING = "redirecting"
    RESUMED = "resumed"
    COMPLETED = "completed"


class SupervisorAssignment(BaseModel):
    id: str
    task_id: str
    task_title: str
    agent_name: str
    runtime_provider: SupervisorRuntimeProvider = SupervisorRuntimeProvider.GENERIC
    execution_mode: SupervisorExecutionMode = SupervisorExecutionMode.SIMULATED
    run_id: str
    state: SupervisorAssignmentState = SupervisorAssignmentState.QUEUED
    scopes: set[str] = Field(default_factory=set)
    action_ids: list[str] = Field(default_factory=list)
    authorized_actions: list[str] = Field(
        default_factory=list,
        max_length=_MAX_AUTHORIZED_ACTIONS,
    )
    plan_id: str
    decision_snapshot: str
    redirected_from_run_id: str | None = None
    interrupt_reason: str | None = None
    redirect_instruction: str | None = None
    provenance_path: list[str] = Field(default_factory=list)
    interrupt_enforced: bool = False


class AppliedInterrupt(BaseModel):
    """Durable record of one approved decision's interrupt, for idempotent replay.

    A webhook may be delivered twice, and the second delivery may arrive after a
    restart. Keeping the answer next to the state it produced means a replay
    returns the original partition instead of recomputing one from state the
    first delivery already changed — which would otherwise re-interrupt a session
    that had already taken the redirect and resumed.
    """

    decision_id: str
    interrupted_assignment_ids: list[str] = Field(default_factory=list)
    preserved_assignment_ids: list[str] = Field(default_factory=list)
    #: The scopes this decision actually changed. Kept so a later preview can
    #: account for work already partially invalidated, and so a retry carrying
    #: DIFFERENT scopes is recognised as a different request rather than
    #: silently replaying this answer.
    affected_scopes: list[str] = Field(default_factory=list)


class WorkspaceSupervisor(BaseModel):
    id: str
    name: str = "Dragback Supervisor"
    state: SupervisorLifecycleState = SupervisorLifecycleState.QUEUED
    adapter: Literal[
        "fixture-agent-runtime",
        "claude-code-hook-runtime",
    ] = "fixture-agent-runtime"
    execution_mode: SupervisorExecutionMode = SupervisorExecutionMode.SIMULATED
    assignments: list[SupervisorAssignment] = Field(default_factory=list)
    applied_interrupts: list[AppliedInterrupt] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_assignments(self) -> WorkspaceSupervisor:
        task_ids = [assignment.task_id for assignment in self.assignments]
        assignment_ids = [assignment.id for assignment in self.assignments]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("supervisor assignments must have unique task IDs")
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("supervisor assignments must have unique IDs")
        return self


class SupervisorRuntimeAdapter(Protocol):
    """Agent-service runtime boundary; it never returns an authority verdict."""

    @property
    def adapter_name(self) -> str: ...

    @property
    def execution_mode(self) -> SupervisorExecutionMode: ...

    def create_supervisor(
        self,
        *,
        workspace_id: str,
        supervisor_run_id: str,
        tasks: list[Artifact],
        plan: AgentPlan,
        decision_snapshot: str,
    ) -> WorkspaceSupervisor: ...

    def transition(
        self,
        assignment: SupervisorAssignment,
        *,
        state: SupervisorAssignmentState,
        plan: AgentPlan | None = None,
        plan_actions: list[PlanAction] | None = None,
        decision_snapshot: str | None = None,
        create_replacement_run: bool = False,
        interrupt_reason: str | None = None,
        redirect_instruction: str | None = None,
        provenance_path: list[str] | None = None,
        interrupt_enforced: bool | None = None,
    ) -> SupervisorAssignment: ...


class FixtureSupervisorRuntime:
    """Deterministic state adapter for the demo; no provider process is launched."""

    adapter_name: Literal["fixture-agent-runtime"] = "fixture-agent-runtime"
    execution_mode: Literal[SupervisorExecutionMode.SIMULATED] = (
        SupervisorExecutionMode.SIMULATED
    )

    def create_supervisor(
        self,
        *,
        workspace_id: str,
        supervisor_run_id: str,
        tasks: list[Artifact],
        plan: AgentPlan,
        decision_snapshot: str,
    ) -> WorkspaceSupervisor:
        resolved_actions = resolve_plan_actions_for_tasks(tasks, plan)
        assignments: list[SupervisorAssignment] = []
        for task in tasks:
            task_actions = resolved_actions.get(task.id, [])
            assignments.append(
                SupervisorAssignment(
                    id=f"ASSIGNMENT-{task.id}",
                    task_id=task.id,
                    task_title=task.title,
                    agent_name=_agent_name(task),
                    runtime_provider=_runtime_provider(task),
                    run_id=_initial_run_id(supervisor_run_id, task.id),
                    scopes=set(task.scopes),
                    action_ids=[action.id for action in task_actions],
                    authorized_actions=_authorized_actions(
                        actions=task_actions,
                    ),
                    plan_id=plan.id,
                    decision_snapshot=decision_snapshot,
                )
            )
        return WorkspaceSupervisor(
            id=f"SUPERVISOR-{workspace_id.upper()}",
            adapter=self.adapter_name,
            execution_mode=self.execution_mode,
            assignments=assignments,
        )

    def transition(
        self,
        assignment: SupervisorAssignment,
        *,
        state: SupervisorAssignmentState,
        plan: AgentPlan | None = None,
        plan_actions: list[PlanAction] | None = None,
        decision_snapshot: str | None = None,
        create_replacement_run: bool = False,
        interrupt_reason: str | None = None,
        redirect_instruction: str | None = None,
        provenance_path: list[str] | None = None,
        interrupt_enforced: bool | None = None,
    ) -> SupervisorAssignment:
        updates: dict[str, object] = {"state": state}
        if plan is not None:
            actions = (
                plan_actions
                if plan_actions is not None
                else _direct_actions_for_assignment(assignment, plan)
            )
            updates["plan_id"] = plan.id
            updates["action_ids"] = [action.id for action in actions]
            updates["authorized_actions"] = _authorized_actions(
                actions=actions,
            )
        if decision_snapshot is not None:
            updates["decision_snapshot"] = decision_snapshot
        if interrupt_reason is not None:
            updates["interrupt_reason"] = interrupt_reason
        if redirect_instruction is not None:
            updates["redirect_instruction"] = redirect_instruction
        if provenance_path is not None:
            updates["provenance_path"] = list(provenance_path)
        if interrupt_enforced is not None:
            updates["interrupt_enforced"] = interrupt_enforced
        if create_replacement_run:
            updates["redirected_from_run_id"] = assignment.run_id
            updates["run_id"] = _replacement_run_id(assignment.run_id)
        return assignment.model_copy(update=updates, deep=True)


def apply_assignment_transition(
    assignment: SupervisorAssignment,
    *,
    state: SupervisorAssignmentState,
    decision_snapshot: str | None = None,
    interrupt_reason: str | None = None,
    redirect_instruction: str | None = None,
    provenance_path: list[str] | None = None,
    interrupt_enforced: bool | None = None,
) -> SupervisorAssignment:
    """Record a state change. It decides nothing; callers supply the decision."""

    updates: dict[str, object] = {"state": state}
    if decision_snapshot is not None:
        updates["decision_snapshot"] = decision_snapshot
    if interrupt_reason is not None:
        updates["interrupt_reason"] = interrupt_reason
    if redirect_instruction is not None:
        updates["redirect_instruction"] = redirect_instruction
    if provenance_path is not None:
        updates["provenance_path"] = list(provenance_path)
    if interrupt_enforced is not None:
        updates["interrupt_enforced"] = interrupt_enforced
    return assignment.model_copy(update=updates, deep=True)


def _runtime_provider(task: Artifact) -> SupervisorRuntimeProvider:
    raw = task.attributes.get("runtime_provider")
    if isinstance(raw, str):
        normalized = raw.strip().casefold().replace("_", "-")
        if normalized == "claude":
            normalized = SupervisorRuntimeProvider.CLAUDE_CODE.value
        try:
            return SupervisorRuntimeProvider(normalized)
        except ValueError:
            pass
    return SupervisorRuntimeProvider.GENERIC


def _agent_name(task: Artifact) -> str:
    configured = task.attributes.get("agent_name")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return f"{task.title} Subagent"


def _direct_actions_for_assignment(
    assignment: SupervisorAssignment,
    plan: AgentPlan,
) -> list[PlanAction]:
    return [
        action
        for action in plan.actions
        if action.attributes.get("task_id") == assignment.task_id
    ]


def resolve_plan_actions_for_tasks(
    tasks: list[Artifact],
    plan: AgentPlan,
) -> dict[str, list[PlanAction]]:
    return _resolve_plan_actions(
        [
            (task.id, task.scopes)
            for task in tasks
            if task.validity is ValidityStatus.VALID
        ],
        plan,
    )


def resolve_plan_actions_for_assignments(
    assignments: list[SupervisorAssignment],
    plan: AgentPlan,
) -> dict[str, list[PlanAction]]:
    return _resolve_plan_actions(
        [
            (assignment.task_id, assignment.scopes)
            for assignment in assignments
        ],
        plan,
    )


def _resolve_plan_actions(
    tasks: list[tuple[str, set[str]]],
    plan: AgentPlan,
) -> dict[str, list[PlanAction]]:
    resolved: dict[str, list[PlanAction]] = {
        task_id: [] for task_id, _scopes in tasks
    }
    scopes_by_task = dict(tasks)
    for action in plan.actions:
        task_id = action.attributes.get("task_id")
        if isinstance(task_id, str):
            if task_id in resolved:
                resolved[task_id].append(action)
            continue
        matches = [
            candidate_id
            for candidate_id, scopes in scopes_by_task.items()
            if bool(action.scopes & scopes)
        ]
        if len(matches) > 1:
            raise ValueError(
                f"unbound AgentPlan action {action.id!r} matches multiple "
                f"Tasks: {', '.join(sorted(matches))}; set attributes.task_id"
            )
        if matches:
            resolved[matches[0]].append(action)
    return resolved


def _authorized_actions(
    *,
    actions: list[PlanAction],
) -> list[str]:
    values = [
        f"Plan action {action.id}: {action.description}"
        for action in actions
    ]
    bounded: list[str] = []
    for value in values:
        normalized = " ".join(value.replace("\x00", "").split())
        if not normalized or normalized in bounded:
            continue
        if len(normalized) > _MAX_AUTHORIZED_ACTION_LENGTH:
            normalized = (
                normalized[: _MAX_AUTHORIZED_ACTION_LENGTH - 3].rstrip()
                + "..."
            )
        bounded.append(normalized)
        if len(bounded) == _MAX_AUTHORIZED_ACTIONS:
            break
    return bounded


def _initial_run_id(supervisor_run_id: str, task_id: str) -> str:
    prefix = supervisor_run_id.removesuffix("-RUN")
    return f"{prefix}-{task_id}-RUN-1"


def _replacement_run_id(run_id: str) -> str:
    match = re.fullmatch(r"(?P<prefix>.+-RUN-)(?P<revision>[1-9]\d*)", run_id)
    if match is None:
        return f"{run_id}-REDIRECT-1"
    revision = int(match.group("revision")) + 1
    return f"{match.group('prefix')}{revision}"
