from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime
from typing import Any

from dragback.config import DEFAULT_AUTHORITY_THRESHOLD
from dragback.domain import (
    AgentPlan,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    AuthorizationResult,
    DecisionMutation,
    Edge,
    EdgeKind,
    GrantVerificationResult,
    InvalidationPath,
    InvalidationReport,
    MutationResult,
    PlanMismatch,
    ValidityStatus,
    Verdict,
    VerificationCode,
    utc_now,
)
from dragback.grants import GrantSigner
from dragback.graph.base import GraphStore
from dragback.hashing import stable_hash
from dragback.provenance import (
    AUTHORITY_DOWNSTREAM_EDGE_KINDS,
    authority_edge_sort_key,
    select_primary_invalidation_path,
)

DOWNSTREAM_EDGES = AUTHORITY_DOWNSTREAM_EDGE_KINDS

UPSTREAM_ARTIFACT_KINDS = {
    ArtifactKind.DECISION,
    ArtifactKind.SPECIFICATION,
    ArtifactKind.TICKET,
    ArtifactKind.EVIDENCE,
}

STOPPABLE_WORK_KINDS = {
    ArtifactKind.TASK,
    ArtifactKind.AGENT_PLAN,
    ArtifactKind.PULL_REQUEST,
    ArtifactKind.CODE_CHANGE,
    ArtifactKind.AGENT_RUN,
}


DEFAULT_AUTHORITY_POLICY: dict[str, set[str]] = {
    "export.authorization": {"compliance", "security", "product"},
    "export.generation": {"engineering", "product"},
}


class IntentAuthority:
    def __init__(
        self,
        *,
        graph: GraphStore,
        signer: GrantSigner,
        authority_threshold: float = DEFAULT_AUTHORITY_THRESHOLD,
        authority_policy: dict[str, set[str]] | None = None,
    ) -> None:
        self.graph = graph
        self.signer = signer
        self.authority_threshold = authority_threshold
        self.authority_policy = authority_policy or DEFAULT_AUTHORITY_POLICY
        self.last_report: InvalidationReport | None = None

    def apply_decision_change(self, mutation: DecisionMutation) -> MutationResult:
        decision = mutation.decision
        if decision.kind is not ArtifactKind.DECISION:
            return MutationResult(
                applied=False,
                reason="Only Decision artifacts can supersede company intent.",
                graph_version=self.graph.version_label,
                verdict=Verdict.HUMAN_REVIEW,
            )
        if decision.approval_status is not ApprovalStatus.APPROVED:
            return MutationResult(
                applied=False,
                reason=(
                    f"Decision status is {decision.approval_status}; "
                    "only approved decisions apply."
                ),
                graph_version=self.graph.version_label,
                verdict=Verdict.HUMAN_REVIEW,
            )
        if decision.confidence < self.authority_threshold:
            return MutationResult(
                applied=False,
                reason="Decision confidence is below the authority threshold.",
                graph_version=self.graph.version_label,
                verdict=Verdict.HUMAN_REVIEW,
            )
        if not mutation.affected_scopes:
            return MutationResult(
                applied=False,
                reason="A decision change must declare at least one affected scope.",
                graph_version=self.graph.version_label,
                verdict=Verdict.HUMAN_REVIEW,
            )

        raw_requirements = decision.attributes.get("requirements", {})
        if (
            not isinstance(raw_requirements, dict)
            or any(not isinstance(scope, str) for scope in raw_requirements)
            or any(not isinstance(requirement, dict) for requirement in raw_requirements.values())
        ):
            return MutationResult(
                applied=False,
                reason="Decision requirements must be an object keyed by scope.",
                graph_version=self.graph.version_label,
                verdict=Verdict.HUMAN_REVIEW,
            )
        requirement_scopes = set(raw_requirements)
        mismatched_scopes = decision.scopes ^ mutation.affected_scopes
        requirement_scope_mismatch = requirement_scopes ^ mutation.affected_scopes
        if mismatched_scopes or requirement_scope_mismatch:
            return MutationResult(
                applied=False,
                reason=(
                    "Decision scopes and requirement scopes must exactly match the "
                    "declared change: "
                    + ", ".join(sorted(mismatched_scopes | requirement_scope_mismatch))
                ),
                graph_version=self.graph.version_label,
                verdict=Verdict.HUMAN_REVIEW,
            )
        disallowed = {
            scope
            for scope in mutation.affected_scopes
            if decision.authority_role not in self.authority_policy.get(scope, set())
        }
        if disallowed:
            return MutationResult(
                applied=False,
                reason=(
                    f"Role {decision.authority_role!r} is not authoritative for scopes: "
                    + ", ".join(sorted(disallowed))
                ),
                graph_version=self.graph.version_label,
                verdict=Verdict.HUMAN_REVIEW,
            )

        try:
            self.graph.get_artifact(decision.id)
        except KeyError:
            pass
        else:
            return MutationResult(
                applied=False,
                reason=f"Decision artifact already exists: {decision.id}.",
                graph_version=self.graph.version_label,
                verdict=Verdict.HUMAN_REVIEW,
            )

        # Validate the supersession target before mutating anything.
        superseded = self.graph.get_artifact(mutation.supersedes_id)
        if superseded.kind is not ArtifactKind.DECISION:
            return MutationResult(
                applied=False,
                reason="A company decision can only supersede another Decision artifact.",
                graph_version=self.graph.version_label,
                verdict=Verdict.HUMAN_REVIEW,
            )
        if not mutation.affected_scopes <= superseded.scopes:
            return MutationResult(
                applied=False,
                reason="The declared change includes scopes absent from the superseded decision.",
                graph_version=self.graph.version_label,
                verdict=Verdict.HUMAN_REVIEW,
            )

        self.graph.add_artifact(decision)
        self.graph.add_edge(
            Edge(
                source_id=decision.id,
                target_id=mutation.supersedes_id,
                kind=EdgeKind.SUPERSEDES,
                scopes=mutation.affected_scopes,
                evidence_ref=decision.source_ref,
            )
        )
        graph_version = self.graph.increment_version()
        report = self._propagate_invalidation(
            changed_decision=decision,
            superseded_id=mutation.supersedes_id,
            affected_scopes=mutation.affected_scopes,
        )
        report.graph_version = graph_version
        self.last_report = report
        return MutationResult(
            applied=True,
            reason="Approved decision applied and affected work re-evaluated.",
            graph_version=graph_version,
            report=report,
        )

    def _mark_artifact(self, artifact: Artifact, affected_scopes: set[str]) -> set[str]:
        intersection = artifact.scopes & affected_scopes
        if not intersection:
            return set()
        artifact.invalidated_scopes |= intersection
        if artifact.scopes and artifact.invalidated_scopes >= artifact.scopes:
            artifact.validity = ValidityStatus.INVALIDATED
        else:
            artifact.validity = ValidityStatus.NEEDS_REVIEW
        self.graph.update_artifact(artifact)
        return intersection

    def _propagate_invalidation(
        self,
        *,
        changed_decision: Artifact,
        superseded_id: str,
        affected_scopes: set[str],
    ) -> InvalidationReport:
        affected: list[str] = []
        preserved: list[str] = []
        paths: list[InvalidationPath] = []
        evidence_refs: list[str] = []

        def record_evidence(reference: str | None) -> None:
            if reference and reference not in evidence_refs:
                evidence_refs.append(reference)

        superseded = self.graph.get_artifact(superseded_id)
        record_evidence(changed_decision.source_ref)
        record_evidence(superseded.source_ref)
        if self._mark_artifact(superseded, affected_scopes):
            affected.append(superseded.id)
            paths.append(
                InvalidationPath(
                    artifact_id=superseded.id,
                    node_ids=[changed_decision.id, superseded.id],
                )
            )

        queue: deque[tuple[str, list[str]]] = deque(
            [(superseded.id, [changed_decision.id, superseded.id])]
        )
        visited: set[str] = {superseded.id}

        while queue:
            current_id, current_path = queue.popleft()
            outgoing = sorted(
                self.graph.outgoing_edges(current_id, DOWNSTREAM_EDGES),
                key=authority_edge_sort_key,
            )
            for edge in outgoing:
                child = self.graph.get_artifact(edge.target_id)
                record_evidence(edge.evidence_ref)
                record_evidence(child.source_ref)
                intersection = child.scopes & affected_scopes
                if not intersection:
                    if child.id not in preserved:
                        preserved.append(child.id)
                    continue

                self._mark_artifact(child, affected_scopes)
                next_path = [*current_path, child.id]
                if child.id not in affected:
                    affected.append(child.id)
                    paths.append(InvalidationPath(artifact_id=child.id, node_ids=next_path))
                if child.id not in visited:
                    visited.add(child.id)
                    queue.append((child.id, next_path))

        affected_artifacts = [self.graph.get_artifact(artifact_id) for artifact_id in affected]
        primary_path = select_primary_invalidation_path(paths)
        upstream_chain: list[str] = []
        primary_path_ids = primary_path.node_ids if primary_path else [changed_decision.id]
        for artifact_id in primary_path_ids:
            artifact = (
                changed_decision
                if artifact_id == changed_decision.id
                else self.graph.get_artifact(artifact_id)
            )
            if artifact.kind in UPSTREAM_ARTIFACT_KINDS:
                upstream_chain.append(artifact.id)
        stopped_work = [
            artifact.id
            for artifact in affected_artifacts
            if artifact.kind in STOPPABLE_WORK_KINDS
        ]
        preserved_task_ids = [
            artifact_id
            for artifact_id in preserved
            if self.graph.get_artifact(artifact_id).kind is ArtifactKind.TASK
        ]
        invalidated_task_ids = [
            artifact.id
            for artifact in affected_artifacts
            if artifact.kind is ArtifactKind.TASK
            and artifact.validity is ValidityStatus.INVALIDATED
        ]
        needs_review_artifact_ids = [
            artifact.id
            for artifact in affected_artifacts
            if artifact.validity is ValidityStatus.NEEDS_REVIEW
        ]
        normalized_decision_text = changed_decision.text.casefold()
        directly_mentioned = [
            artifact.id
            for artifact in affected_artifacts
            if artifact.id.casefold() in normalized_decision_text
        ]

        return InvalidationReport(
            graph_version=self.graph.version_label,
            changed_decision_id=changed_decision.id,
            superseded_decision_id=superseded_id,
            affected_scopes=affected_scopes,
            affected_artifact_ids=affected,
            upstream_chain_artifact_ids=upstream_chain,
            preserved_task_ids=preserved_task_ids,
            invalidated_task_ids=invalidated_task_ids,
            needs_review_artifact_ids=needs_review_artifact_ids,
            stopped_work_artifact_ids=stopped_work,
            directly_mentioned_artifact_ids=directly_mentioned,
            preserved_artifact_ids=preserved,
            paths=paths,
            evidence_refs=evidence_refs,
        )

    def current_requirements(self) -> dict[str, dict[str, Any]]:
        candidates: list[tuple[datetime, Artifact, str, dict[str, Any]]] = []
        for artifact in self.graph.list_artifacts():
            if artifact.kind is not ArtifactKind.DECISION:
                continue
            if artifact.approval_status is not ApprovalStatus.APPROVED:
                continue
            requirements = artifact.attributes.get("requirements", {})
            for scope, requirement in requirements.items():
                if scope in artifact.invalidated_scopes:
                    continue
                effective_at = artifact.effective_at or datetime.min.replace(
                    tzinfo=utc_now().tzinfo
                )
                candidates.append((effective_at, artifact, scope, requirement))

        result: dict[str, dict[str, Any]] = {}
        for _, _, scope, requirement in sorted(candidates, key=lambda item: item[0]):
            result[scope] = deepcopy(requirement)
        return result

    def evaluate_plan(self, *, run_id: str, task_id: str, plan: AgentPlan) -> AuthorizationResult:
        requirements = self.current_requirements()
        mismatches: list[PlanMismatch] = []
        affected_scopes: set[str] = set()

        try:
            task = self.graph.get_artifact(task_id)
        except KeyError:
            return AuthorizationResult(
                verdict=Verdict.BLOCK,
                reason="The authorization task is not present in the current graph.",
                graph_version=self.graph.version_label,
                task_id=task_id,
                current_requirements=requirements,
            )
        if plan.ticket_id != task_id:
            return AuthorizationResult(
                verdict=Verdict.BLOCK,
                reason="The plan is bound to a different ticket than the authorization request.",
                graph_version=self.graph.version_label,
                task_id=task_id,
                current_requirements=requirements,
            )

        required_scopes = set(requirements) & task.scopes
        missing_scopes = required_scopes - plan.scopes
        for scope in sorted(missing_scopes):
            expected = requirements[scope]
            mismatches.append(
                PlanMismatch(
                    action_id=plan.id,
                    scope=scope,
                    expected=expected,
                    actual={key: None for key in expected},
                )
            )
            affected_scopes.add(scope)

        for action in plan.actions:
            if "task_id" in action.attributes:
                referenced_task_id = action.attributes.get("task_id")
                referenced_task: Artifact | None = None
                if isinstance(referenced_task_id, str):
                    try:
                        referenced_task = self.graph.get_artifact(referenced_task_id)
                    except KeyError:
                        pass
                reference_scopes = (
                    action.scopes
                    | (referenced_task.invalidated_scopes if referenced_task else set())
                )
                reference_scope = (
                    sorted(reference_scopes)[0] if reference_scopes else "task.reference"
                )
                reference_actual: dict[str, Any] | None = None
                if referenced_task is None:
                    reference_actual = {
                        "task_id": referenced_task_id,
                        "validity": "MISSING",
                    }
                elif referenced_task.kind is not ArtifactKind.TASK:
                    reference_actual = {
                        "task_id": referenced_task_id,
                        "kind": referenced_task.kind.value,
                    }
                elif referenced_task.validity is not ValidityStatus.VALID:
                    reference_actual = {
                        "task_id": referenced_task_id,
                        "validity": referenced_task.validity.value,
                    }
                if reference_actual is not None:
                    mismatches.append(
                        PlanMismatch(
                            action_id=action.id,
                            scope=reference_scope,
                            expected={
                                "task_id": referenced_task_id,
                                "validity": ValidityStatus.VALID.value,
                            },
                            actual=reference_actual,
                        )
                    )
                    affected_scopes.update(reference_scopes)

            for scope in action.scopes:
                scope_requirement = requirements.get(scope)
                if scope_requirement is None:
                    continue
                actual = {key: action.attributes.get(key) for key in scope_requirement}
                if actual != scope_requirement:
                    mismatches.append(
                        PlanMismatch(
                            action_id=action.id,
                            scope=scope,
                            expected=scope_requirement,
                            actual=actual,
                        )
                    )
                    affected_scopes.add(scope)

        invalidated_ids: list[str] = []
        preserved_ids: list[str] = []
        evidence_refs: list[str] = []
        path: list[str] = []
        if self.last_report is not None:
            invalidated_ids = list(self.last_report.affected_artifact_ids)
            preserved_ids = list(self.last_report.preserved_artifact_ids)
            evidence_refs = list(self.last_report.evidence_refs)
            plan_paths = [
                item.node_ids for item in self.last_report.paths if item.artifact_id == plan.id
            ]
            if plan_paths:
                path = plan_paths[0]

        if mismatches:
            return AuthorizationResult(
                verdict=Verdict.REPLAN,
                reason=(
                    "The plan conflicts with current approved requirements "
                    "or current graph validity."
                ),
                graph_version=self.graph.version_label,
                task_id=task_id,
                affected_scopes=affected_scopes,
                mismatches=mismatches,
                current_requirements=requirements,
                invalidation_path=path,
                invalidated_artifact_ids=invalidated_ids,
                preserved_artifact_ids=preserved_ids,
                evidence_refs=evidence_refs,
            )

        plan_hash = stable_hash(plan)
        grant = self.signer.issue(
            run_id=run_id,
            task_id=task_id,
            decision_snapshot=self.graph.version_label,
            plan_hash=plan_hash,
        )
        return AuthorizationResult(
            verdict=Verdict.ALLOW,
            reason="Plan matches current approved requirements.",
            graph_version=self.graph.version_label,
            task_id=task_id,
            current_requirements=requirements,
            invalidation_path=path,
            invalidated_artifact_ids=invalidated_ids,
            preserved_artifact_ids=preserved_ids,
            evidence_refs=evidence_refs,
            grant=grant,
        )

    def verify_grant(
        self,
        *,
        token: str,
        run_id: str,
        task_id: str,
        plan: AgentPlan,
    ) -> GrantVerificationResult:
        try:
            payload = self.signer.decode(token)
        except (ValueError, TypeError) as exc:
            return GrantVerificationResult(
                valid=False,
                code=VerificationCode.INVALID_TOKEN,
                reason=str(exc),
            )

        if payload.verdict is not Verdict.ALLOW:
            return GrantVerificationResult(
                valid=False,
                code=VerificationCode.NON_ALLOW_VERDICT,
                reason=f"Grant payload verdict is {payload.verdict.value}, not ALLOW.",
                payload=payload,
            )
        if payload.expires_at <= utc_now():
            return GrantVerificationResult(
                valid=False,
                code=VerificationCode.EXPIRED,
                reason="Grant has expired.",
                payload=payload,
            )
        if payload.run_id != run_id or payload.task_id != task_id:
            return GrantVerificationResult(
                valid=False,
                code=VerificationCode.BINDING_MISMATCH,
                reason="Grant is bound to a different run or task.",
                payload=payload,
            )
        current_hash = stable_hash(plan)
        if payload.plan_hash != current_hash:
            return GrantVerificationResult(
                valid=False,
                code=VerificationCode.PLAN_HASH_MISMATCH,
                reason="Grant plan hash does not match the proposed plan.",
                payload=payload,
            )
        if payload.decision_snapshot != self.graph.version_label:
            return GrantVerificationResult(
                valid=False,
                code=VerificationCode.STALE_SNAPSHOT,
                reason=(
                    f"Grant snapshot {payload.decision_snapshot} is stale; "
                    f"current graph is {self.graph.version_label}."
                ),
                payload=payload,
            )

        fresh = self.evaluate_plan(run_id=run_id, task_id=task_id, plan=plan)
        if fresh.verdict is not Verdict.ALLOW:
            return GrantVerificationResult(
                valid=False,
                code=VerificationCode.CURRENT_PLAN_REJECTED,
                reason=f"Current plan verdict is {fresh.verdict.value}, not ALLOW.",
                payload=payload,
            )
        return GrantVerificationResult(
            valid=True,
            code=VerificationCode.VALID,
            reason="Grant is valid.",
            payload=payload,
        )
