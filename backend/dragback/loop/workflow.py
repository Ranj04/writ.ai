from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict

from dragback.authority.engine import IntentAuthority
from dragback.domain import AgentPlan, AgentRun, AuthorizationResult, LoopState, Verdict


def replan_for_requirements(
    plan: AgentPlan, requirements: dict[str, dict[str, Any]]
) -> AgentPlan:
    corrected = plan.model_copy(deep=True)
    corrected.id = "PLAN-028"
    for action in corrected.actions:
        for scope in action.scopes:
            expected = requirements.get(scope)
            if not expected:
                continue
            action.attributes.update(deepcopy(expected))
            if scope == "export.authorization" and expected.get("audience") == "admin_only":
                action.description = "Expose the CSV export control to administrators only"
    return corrected


def apply_authorization_result(run: AgentRun, result: AuthorizationResult) -> None:
    """Apply an authority verdict to agent-owned loop state."""

    run.graph_snapshot = result.graph_version
    run.grant_token = result.grant.token if result.grant else None
    if result.verdict is Verdict.ALLOW:
        run.state = LoopState.ACT
    elif result.verdict is Verdict.REPLAN:
        run.state = LoopState.REPLAN
    elif result.verdict is Verdict.BLOCK:
        run.state = LoopState.BLOCKED
    else:
        run.state = LoopState.HUMAN_REVIEW


class AgentLoopController:
    def __init__(self, *, authority: IntentAuthority, run: AgentRun) -> None:
        self.authority = authority
        self.run = run.model_copy(deep=True)
        self.last_authorization: AuthorizationResult | None = None

    def start(self) -> AuthorizationResult:
        self.run.state = LoopState.VERIFY
        result = self.authority.evaluate_plan(
            run_id=self.run.run_id,
            task_id=self.run.ticket_id,
            plan=self.run.plan,
        )
        self._apply_result(result)
        self.run.history.append(f"Initial verification: {result.verdict.value}")
        return result

    def mark_tests_passed(self) -> None:
        self.run.tests_passed = True
        self.run.state = LoopState.ACT
        self.run.history.append("Implementation complete; tests passed.")

    def recheck(self) -> AuthorizationResult:
        self.run.state = LoopState.VERIFY
        result = self.authority.evaluate_plan(
            run_id=self.run.run_id,
            task_id=self.run.ticket_id,
            plan=self.run.plan,
        )
        self._apply_result(result)
        self.run.history.append(f"Reauthorization: {result.verdict.value}")
        return result

    def replan(self) -> AuthorizationResult:
        requirements = self.authority.current_requirements()
        self.run.plan = replan_for_requirements(self.run.plan, requirements)
        self.run.state = LoopState.VERIFY
        result = self.authority.evaluate_plan(
            run_id=self.run.run_id,
            task_id=self.run.ticket_id,
            plan=self.run.plan,
        )
        self._apply_result(result)
        self.run.history.append(f"Corrected plan verification: {result.verdict.value}")
        return result

    def _apply_result(self, result: AuthorizationResult) -> None:
        self.last_authorization = result
        apply_authorization_result(self.run, result)


class WorkflowState(TypedDict, total=False):
    run: AgentRun
    authorization: AuthorizationResult


def build_langgraph_workflow(authority: IntentAuthority):
    """Build the optional LangGraph version of the loop.

    The package is intentionally imported lazily so the deterministic demo works
    without the optional dependency.
    """

    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise RuntimeError('Install Dragback with `pip install -e ".[agent]"`') from exc

    def verify_node(state: WorkflowState) -> WorkflowState:
        run = state["run"]
        authorization = authority.evaluate_plan(
            run_id=run.run_id, task_id=run.ticket_id, plan=run.plan
        )
        return {**state, "authorization": authorization}

    def route(state: WorkflowState) -> str:
        return state["authorization"].verdict.value

    def act_node(state: WorkflowState) -> WorkflowState:
        run = state["run"].model_copy(deep=True)
        run.state = LoopState.ACT
        return {**state, "run": run}

    def replan_node(state: WorkflowState) -> WorkflowState:
        run = state["run"].model_copy(deep=True)
        run.plan = replan_for_requirements(run.plan, authority.current_requirements())
        run.state = LoopState.REPLAN
        return {**state, "run": run}

    builder = StateGraph(WorkflowState)
    builder.add_node("verify", verify_node)
    builder.add_node("act", act_node)
    builder.add_node("replan", replan_node)
    builder.add_edge(START, "verify")
    builder.add_conditional_edges(
        "verify",
        route,
        {
            Verdict.ALLOW.value: "act",
            Verdict.REPLAN.value: "replan",
            Verdict.BLOCK.value: END,
            Verdict.HUMAN_REVIEW.value: END,
        },
    )
    builder.add_edge("replan", "verify")
    builder.add_edge("act", END)
    return builder.compile()
