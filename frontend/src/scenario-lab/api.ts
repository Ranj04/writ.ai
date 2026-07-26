import type {
  AgentPlan,
  Artifact,
  Edge,
  InvalidationReport,
  Verdict,
} from "../types";
import type {
  EvidenceEntry,
  GrantView,
  PlanValidity,
  PlanView,
  ProvenanceEdge,
  ProvenanceNode,
  ProvenanceNodeStatus,
  ScenarioDefinition,
  ScenarioEvaluation,
  ScenarioLabClient,
  ScenarioOutcome,
  ScenarioRunState,
  ScenarioRunSummary,
  ScenarioTaskSummary,
  StageResult,
  VerificationCode,
} from "./model";

import { serviceBaseUrl } from "../api";

const AGENT = serviceBaseUrl(import.meta.env.VITE_AGENT_URL, "http://localhost:8002");

interface RawCatalogItem {
  id: string;
  name: string;
  category: ScenarioDefinition["category"];
  description: string;
  risk_level: ScenarioDefinition["riskLevel"];
  original_decision_id: string;
  original_decision_text: string;
  original_graph_version: string;
  new_decision_id: string;
  new_decision_text: string;
  new_graph_version: string;
  why_changed: string;
  risk_if_old_authorization_continues: string;
  expected_corrected_behavior: string;
  selector_summary: string;
  preserved_work: string[];
  invalidated_work: string[];
  newly_required_work: string[];
  specification: {
    id: string;
    title: string;
    description: string;
    scopes: string[];
  };
  ticket: {
    id: string;
    title: string;
    description: string;
    scopes: string[];
  };
  tasks: Array<{
    id: string;
    title: string;
    description: string;
    scopes: string[];
    expected_status: "preserved" | "invalidated";
  }>;
  initial_plan: AgentPlan;
  last_result: ScenarioDefinition["lastResult"];
  last_run_at: string | null;
}

interface RawRunSummary {
  run_id: string;
  scenario_id: string;
  scenario_name: string;
  category: ScenarioRunSummary["category"];
  risk_level: ScenarioRunSummary["riskLevel"];
  status: ScenarioRunSummary["status"];
  preserved_expected: number;
  preserved_actual: number;
  preserved_expected_ids?: string[];
  preserved_actual_ids?: string[];
  invalidated_expected: number;
  invalidated_actual: number;
  invalidated_expected_ids?: string[];
  invalidated_actual_ids?: string[];
  false_positive_invalidations?: string[];
  missed_invalidations?: string[];
  old_grant_rejected_expected: boolean;
  old_grant_rejected: boolean;
  reauthorization_expected: boolean;
  reauthorization_succeeded: boolean;
  plan_status?: PlanValidity;
  needs_review_artifact_ids?: string[];
  old_grant_verification_code?: VerificationCode | null;
  replacement_authorization_verdict?: Verdict | null;
  replacement_grant_verification_code?: VerificationCode | null;
  history_scope?: "session";
  runtime_ms: number;
  failure_reasons: string[];
  completed_at: string;
  inspectable?: boolean;
}

interface RawCatalogResponse {
  scenarios: RawCatalogItem[];
  latest_runs: RawRunSummary[];
}

interface RawGrantPayload {
  authorization_id: string;
  run_id: string;
  task_id: string;
  decision_snapshot: string;
  plan_hash: string;
  verdict: Verdict;
  issued_at: string;
  expires_at: string;
}

interface RawAuthorization {
  verdict: Verdict;
  reason: string;
  graph_version: string;
  task_id: string;
  affected_scopes: string[];
  invalidation_path: string[];
  invalidated_artifact_ids: string[];
  preserved_artifact_ids: string[];
  evidence_refs: string[];
  grant: RawGrantPayload | null;
}

interface RawExecution {
  applied: boolean;
  reason: string;
  verification_code: string;
  pull_request_url?: string | null;
}

interface RawEvent {
  sequence: number;
  stage: ScenarioRunState["activeStage"];
  event_type: string;
  label: string;
  detail: string;
  created_at: string;
  data: Record<string, unknown>;
}

interface RawEvaluation {
  status: "passed" | "failed";
  checks: Array<{
    id: string;
    label: string;
    expected: string;
    actual: string;
    passed: boolean;
  }>;
  runtime_ms: number;
  actual_preserved_task_ids: string[];
  actual_invalidated_task_ids: string[];
  actual_needs_review_artifact_ids: string[];
  actual_newly_required_action_ids: string[];
  false_positive_invalidations: string[];
  missed_invalidations: string[];
  failure_reasons: string[];
}

interface RawCorrectiveAction {
  id: string;
  description: string;
  scopes: string[];
  source: "fixture";
  representation: "plan-action";
  graph_artifact_id: null;
  persisted_as_graph_artifact: false;
  lifecycle: "fixture-preview" | "authorized-plan-action";
}

interface RawOutcomeSummary {
  preserved_task_ids: string[];
  invalidated_task_ids: string[];
  needs_review_artifact_ids: string[];
  original_plan_id: string;
  original_plan_status: PlanValidity;
  corrective_actions: RawCorrectiveAction[];
  old_grant_verification_code: VerificationCode | null;
  replacement_authorization_verdict: Verdict | null;
  replacement_grant_verification_code: VerificationCode | null;
  may_continue: boolean;
  primary_provenance_path: string[];
  history_scope: "session";
}

interface RawRun {
  run_id: string;
  context_id: string;
  scenario_id: string;
  status: ScenarioRunState["status"];
  active_stage: ScenarioRunState["activeStage"];
  graph_version: string;
  agent_loop_state?: string;
  agent_history?: string[];
  artifacts: Artifact[];
  edges: Edge[];
  started_at: string;
  completed_at: string | null;
  original_plan: AgentPlan;
  corrected_plan: AgentPlan | null;
  original_authorization: RawAuthorization;
  conflict_authorization: RawAuthorization | null;
  corrected_authorization: RawAuthorization | null;
  original_grant: RawGrantPayload | null;
  replacement_grant: RawGrantPayload | null;
  invalidation_report: InvalidationReport | null;
  old_execution: RawExecution | null;
  new_execution: RawExecution | null;
  events: RawEvent[];
  evaluation: RawEvaluation | null;
  outcome_summary?: RawOutcomeSummary | null;
}

interface RawRunAllReport {
  runs: RawRunSummary[];
}

async function scenarioJson<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${AGENT}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init.headers ?? {}),
      },
    });
  } catch (caught) {
    init.signal?.throwIfAborted();
    throw new Error(
      caught instanceof Error
        ? `Agent service: ${caught.message}`
        : "Agent service: network request failed.",
    );
  }
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as {
        error?: { message?: unknown };
      };
      if (typeof body.error?.message === "string") {
        message = body.error.message;
      }
    } catch {
      // Keep the status fallback for a non-JSON upstream response.
    }
    throw new Error(`Agent service: ${message}`);
  }
  return response.json() as Promise<T>;
}

function expectedOutcomes(item: RawCatalogItem): ScenarioOutcome[] {
  const taskOutcomes = item.tasks.map((task) => ({
    id: task.id,
    label: task.title,
    detail: task.description,
    kind:
      task.expected_status === "preserved"
        ? ("preserved" as const)
        : ("stopped" as const),
    basis: "expected" as const,
  }));
  return [
    ...taskOutcomes,
    ...item.newly_required_work.map((label, index) => ({
      id: `${item.id}-expected-new-${index + 1}`,
      label,
      detail: "Expected corrective work after the decision is applied.",
      kind: "newly-required" as const,
      basis: "expected" as const,
    })),
  ];
}

function mapScenario(item: RawCatalogItem): ScenarioDefinition {
  const tasks: ScenarioTaskSummary[] = item.tasks.map((task) => ({
    id: task.id,
    title: task.title,
    description: task.description,
    scopes: task.scopes,
    expectedStatus: task.expected_status,
  }));
  return {
    id: item.id,
    name: item.name,
    category: item.category,
    description: item.selector_summary || item.description,
    riskLevel: item.risk_level,
    originalDecision: {
      id: item.original_decision_id,
      text: item.original_decision_text,
      graphSnapshot: item.original_graph_version,
    },
    newDecision: {
      id: item.new_decision_id,
      text: item.new_decision_text,
      graphSnapshot: item.new_graph_version,
      reason: item.why_changed,
    },
    specification: item.specification,
    ticket: item.ticket,
    tasks,
    initialPlan: planView(item.initial_plan, "fixture")!,
    riskIfContinued: item.risk_if_old_authorization_continues,
    expectedOutcomes: expectedOutcomes(item),
    expectedCorrectedBehavior: item.expected_corrected_behavior,
    lastResult: item.last_result ?? "not-run",
    lastRunAt: item.last_run_at ?? undefined,
  };
}

function uniqueScopes(plan: AgentPlan): string[] {
  return Array.from(
    new Set(plan.actions.flatMap((action) => action.scopes)),
  ).sort();
}

function planView(
  plan: AgentPlan | null,
  source: PlanView["source"],
): PlanView | undefined {
  if (!plan) return undefined;
  return {
    id: plan.id,
    objective: plan.objective,
    steps: plan.actions.map((action) => action.description),
    scope: uniqueScopes(plan),
    source,
  };
}

export function grantDisplayStatus(
  execution: Pick<RawExecution, "applied" | "verification_code"> | null,
  unverifiedStatus: "active" | "issued" | "pending",
): GrantView["status"] {
  if (!execution) return unverifiedStatus;
  const verified = execution.verification_code === "VALID";
  if (execution.applied && verified) return "applied";
  if (execution.applied) return "inconsistent";
  if (verified) return "not-applied";
  return "rejected";
}

function grantView(
  grant: RawGrantPayload | null,
  plan: AgentPlan,
  execution: RawExecution | null,
  unverifiedStatus: "active" | "issued" | "pending",
): GrantView | undefined {
  if (!grant) return undefined;
  return {
    id: grant.authorization_id,
    runId: grant.run_id,
    taskId: grant.task_id,
    graphSnapshot: grant.decision_snapshot,
    planId: plan.id,
    scope: uniqueScopes(plan),
    allowedTaskIds: Array.from(
      new Set(
        plan.actions.flatMap((action) =>
          typeof action.attributes.task_id === "string"
            ? [action.attributes.task_id]
            : [],
        ),
      ),
    ).sort(),
    status: grantDisplayStatus(execution, unverifiedStatus),
    issuedAt: grant.issued_at,
    expiresAt: grant.expires_at,
    planHash: grant.plan_hash,
    verificationCode: execution?.verification_code,
  };
}

function nodeStatus(
  artifact: Artifact | undefined,
  nodeId: string,
  report: InvalidationReport | null,
): ProvenanceNodeStatus {
  if (!report) return "valid";
  if (nodeId === report.changed_decision_id) return "changed";
  if (nodeId === report.superseded_decision_id) return "superseded";
  if (artifact?.validity === "INVALIDATED") return "stopped";
  if (artifact?.validity === "NEEDS_REVIEW") return "needs-review";
  if (report.preserved_artifact_ids.includes(nodeId)) return "preserved";
  return "valid";
}

function buildProvenance(raw: RawRun, scenario: ScenarioDefinition) {
  const report = raw.invalidation_report;
  const artifactById = new Map(
    raw.artifacts.map((artifact) => [artifact.id, artifact]),
  );
  const nodes: ProvenanceNode[] = raw.artifacts.map((artifact) => ({
    id: artifact.id,
    kind: artifact.kind,
    title: artifact.title,
    status: nodeStatus(artifact, artifact.id, report),
    scopes: [...artifact.scopes].sort(),
    invalidatedScopes: [...artifact.invalidated_scopes].sort(),
  }));
  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges: ProvenanceEdge[] = raw.edges.map((edge) => ({
    sourceId: edge.source_id,
    targetId: edge.target_id,
    relation: edge.kind,
    scopes: [...(edge.scopes ?? [])].sort(),
    evidenceRef: edge.evidence_ref ?? undefined,
  }));
  const edgeKeys = new Set(
    edges.map((edge) => `${edge.sourceId}:${edge.targetId}:${edge.relation}`),
  );

  const addNode = (
    node: ProvenanceNode,
  ) => {
    if (nodeIds.has(node.id)) return;
    nodes.push(node);
    nodeIds.add(node.id);
  };
  const addDisplayEdge = (
    sourceId: string,
    targetId: string,
    relation: string,
    evidenceRef?: string,
  ) => {
    const key = `${sourceId}:${targetId}:${relation}`;
    if (
      !nodeIds.has(sourceId) ||
      !nodeIds.has(targetId) ||
      edgeKeys.has(key)
    ) {
      return;
    }
    edges.push({
      sourceId,
      targetId,
      relation,
      evidenceRef,
      synthetic: true,
    });
    edgeKeys.add(key);
  };

  if (report && !nodeIds.has(report.changed_decision_id)) {
    addNode({
      id: report.changed_decision_id,
      kind: "Decision",
      title: scenario.newDecision.text,
      status: "changed",
      synthetic: true,
    });
  }
  if (
    report &&
    !edges.some(
      (edge) =>
        edge.sourceId === report.changed_decision_id &&
        edge.targetId === report.superseded_decision_id,
    )
  ) {
    addDisplayEdge(
      report.changed_decision_id,
      report.superseded_decision_id,
      "SUPERSEDES",
      report.evidence_refs[0],
    );
  }

  const addPlan = (
    plan: AgentPlan | null,
    status: ProvenanceNodeStatus,
  ) => {
    if (!plan) return;
    addNode({
      id: plan.id,
      kind: "AgentPlan",
      title: plan.objective,
      status,
      scopes: uniqueScopes(plan),
      synthetic: true,
    });
    const hasIncoming = edges.some((edge) => edge.targetId === plan.id);
    if (!hasIncoming) {
      for (const action of plan.actions) {
        const taskId = action.attributes.task_id;
        if (typeof taskId === "string") {
          addDisplayEdge(
            taskId,
            plan.id,
            "PLAN_ACTION_FOR",
            `plan-payload://${plan.id}/${action.id}`,
          );
        }
      }
      if (!edges.some((edge) => edge.targetId === plan.id)) {
        addDisplayEdge(
          plan.ticket_id,
          plan.id,
          "PLAN_FOR",
          `plan-payload://${plan.id}`,
        );
      }
    }
  };

  addPlan(
    raw.original_plan,
    nodeStatus(
      artifactById.get(raw.original_plan.id),
      raw.original_plan.id,
      report,
    ),
  );
  addPlan(
    raw.corrected_plan,
    raw.corrected_authorization?.verdict === "ALLOW"
      ? "reauthorized"
      : "pending",
  );

  const addGrant = (
    grant: RawGrantPayload | null,
    plan: AgentPlan | null,
    execution: RawExecution | null,
    replacement: boolean,
  ) => {
    if (!grant || !plan) return;
    let status: ProvenanceNodeStatus = replacement ? "pending" : "valid";
    if (execution) {
      status =
        execution.applied && execution.verification_code === "VALID"
          ? replacement
            ? "reauthorized"
            : "valid"
          : "rejected";
    }
    addNode({
      id: grant.authorization_id,
      kind: "Grant",
      title: `${grant.decision_snapshot} authorization for ${plan.id}`,
      status,
      scopes: uniqueScopes(plan),
      synthetic: true,
    });
    addDisplayEdge(
      plan.id,
      grant.authorization_id,
      "ISSUED_FOR",
      `grant-payload://${grant.authorization_id}`,
    );
  };
  addGrant(raw.original_grant, raw.original_plan, raw.old_execution, false);
  addGrant(
    raw.replacement_grant,
    raw.corrected_plan,
    raw.new_execution,
    true,
  );

  return {
    nodes,
    edges,
  };
}

function buildOutcomes(
  raw: RawRun,
  scenario: ScenarioDefinition,
): ScenarioOutcome[] {
  const report = raw.invalidation_report;
  if (!report) {
    return scenario.expectedOutcomes.map((outcome) => ({
      ...outcome,
      basis: "expected",
    }));
  }
  const artifactById = new Map(
    raw.artifacts.map((artifact) => [artifact.id, artifact]),
  );
  const preservedTaskIds =
    raw.outcome_summary?.preserved_task_ids ??
    report.preserved_task_ids ??
    report.preserved_artifact_ids;
  const invalidatedTaskIds =
    raw.outcome_summary?.invalidated_task_ids ??
    report.invalidated_task_ids ??
    report.stopped_work_artifact_ids;
  const preserved = preservedTaskIds.flatMap((artifactId) => {
    const artifact = artifactById.get(artifactId);
    return artifact?.kind === "Task"
      ? [
          {
            id: artifact.id,
            label: artifact.title,
            detail: artifact.scopes.join(", "),
            kind: "preserved" as const,
            basis: "actual" as const,
          },
        ]
      : [];
  });
  const stopped = invalidatedTaskIds.flatMap((artifactId) => {
    const artifact = artifactById.get(artifactId);
    return artifact?.kind === "Task"
      ? [
          {
            id: artifact.id,
            label: artifact.title,
            detail: artifact.validity,
            kind: "stopped" as const,
            basis: "actual" as const,
            representation: "task" as const,
          },
        ]
      : [];
  });
  const originalActionIds = new Set(
    raw.original_plan.actions.map((action) => action.id),
  );
  const newlyRequired = raw.outcome_summary
    ? raw.outcome_summary.corrective_actions.map((action) => ({
        id: action.id,
        label: action.description,
        detail: action.scopes.join(", "),
        kind: "newly-required" as const,
        basis: "actual" as const,
        source: action.source,
        representation: action.representation,
        persistedAsGraphArtifact: action.persisted_as_graph_artifact,
        lifecycle: action.lifecycle,
      }))
    : raw.corrected_plan
    ? raw.corrected_plan.actions.flatMap((action) =>
        originalActionIds.has(action.id)
          ? []
          : [
              {
                id: action.id,
                label: action.description,
                detail: action.scopes.join(", "),
                kind: "newly-required" as const,
                basis: "actual" as const,
                source: "fixture" as const,
                representation: "plan-action" as const,
                persistedAsGraphArtifact: false,
                lifecycle:
                  raw.corrected_authorization?.verdict === "ALLOW"
                    ? ("authorized-plan-action" as const)
                    : ("fixture-preview" as const),
              },
            ],
      )
    : scenario.expectedOutcomes
        .filter((outcome) => outcome.kind === "newly-required")
        .map((outcome) => ({
          ...outcome,
          detail:
            outcome.detail ??
            "Expected corrective work after the plan is replanned.",
          basis: "expected" as const,
          source: "fixture" as const,
          representation: "plan-action" as const,
          persistedAsGraphArtifact: false,
          lifecycle: "fixture-preview" as const,
        }));
  return [...preserved, ...stopped, ...newlyRequired];
}

function primaryPath(raw: RawRun): string[] {
  const paths = raw.invalidation_report?.paths ?? [];
  const toPlan = paths.find(
    (path) =>
      path.artifact_id === raw.original_plan.id ||
      path.node_ids[path.node_ids.length - 1] === raw.original_plan.id,
  );
  if (toPlan) return [...toPlan.node_ids];
  const first = [...paths].sort(
    (left, right) =>
      left.node_ids.length - right.node_ids.length ||
      left.artifact_id.localeCompare(right.artifact_id),
  )[0];
  return first ? [...first.node_ids] : [];
}

function mapOutcomeSummary(raw: RawRun): ScenarioRunState["outcomeSummary"] {
  if (raw.outcome_summary) {
    return {
      preservedTaskIds: raw.outcome_summary.preserved_task_ids,
      invalidatedTaskIds: raw.outcome_summary.invalidated_task_ids,
      needsReviewArtifactIds:
        raw.outcome_summary.needs_review_artifact_ids,
      originalPlanId: raw.outcome_summary.original_plan_id,
      originalPlanStatus: raw.outcome_summary.original_plan_status,
      correctiveActions: raw.outcome_summary.corrective_actions.map(
        (action) => ({
          id: action.id,
          description: action.description,
          scopes: action.scopes,
          source: action.source,
          representation: action.representation,
          graphArtifactId: action.graph_artifact_id,
          persistedAsGraphArtifact: action.persisted_as_graph_artifact,
          lifecycle: action.lifecycle,
        }),
      ),
      oldGrantVerificationCode:
        raw.outcome_summary.old_grant_verification_code,
      replacementAuthorizationVerdict:
        raw.outcome_summary.replacement_authorization_verdict,
      replacementGrantVerificationCode:
        raw.outcome_summary.replacement_grant_verification_code,
      mayContinue: raw.outcome_summary.may_continue,
      primaryProvenancePath:
        raw.outcome_summary.primary_provenance_path,
      historyScope: raw.outcome_summary.history_scope,
    };
  }

  const artifactById = new Map(
    raw.artifacts.map((artifact) => [artifact.id, artifact]),
  );
  const report = raw.invalidation_report;
  const preservedTaskIds =
    report?.preserved_task_ids ??
    report?.preserved_artifact_ids.filter(
      (artifactId) => artifactById.get(artifactId)?.kind === "Task",
    ) ??
    [];
  const invalidatedTaskIds =
    report?.invalidated_task_ids ??
    report?.stopped_work_artifact_ids.filter(
      (artifactId) =>
        artifactById.get(artifactId)?.kind === "Task" &&
        artifactById.get(artifactId)?.validity === "INVALIDATED",
    ) ??
    [];
  const needsReviewArtifactIds =
    report?.needs_review_artifact_ids ??
    report?.stopped_work_artifact_ids.filter(
      (artifactId) =>
        artifactById.get(artifactId)?.validity === "NEEDS_REVIEW",
    ) ??
    [];
  const originalActionIds = new Set(
    raw.original_plan.actions.map((action) => action.id),
  );
  const correctedActions =
    raw.corrected_plan?.actions
      .filter((action) => !originalActionIds.has(action.id))
      .map((action) => ({
        id: action.id,
        description: action.description,
        scopes: action.scopes,
        source: "fixture" as const,
        representation: "plan-action" as const,
        graphArtifactId: null,
        persistedAsGraphArtifact: false as const,
        lifecycle:
          raw.corrected_authorization?.verdict === "ALLOW"
            ? ("authorized-plan-action" as const)
            : ("fixture-preview" as const),
      })) ?? [];
  return {
    preservedTaskIds,
    invalidatedTaskIds,
    needsReviewArtifactIds,
    originalPlanId: raw.original_plan.id,
    originalPlanStatus:
      artifactById.get(raw.original_plan.id)?.validity ?? null,
    correctiveActions: correctedActions,
    oldGrantVerificationCode:
      (raw.old_execution?.verification_code as VerificationCode | undefined) ??
      null,
    replacementAuthorizationVerdict:
      raw.corrected_authorization?.verdict ?? null,
    replacementGrantVerificationCode:
      (raw.new_execution?.verification_code as VerificationCode | undefined) ??
      null,
    mayContinue: null,
    primaryProvenancePath: primaryPath(raw),
    historyScope: "session",
  };
}

function stageResult(raw: RawRun): StageResult {
  const authorization =
    raw.active_stage === "reauthorized"
      ? raw.corrected_authorization ?? raw.original_authorization
      : raw.active_stage === "work-stopped"
        ? raw.conflict_authorization ?? raw.original_authorization
        : raw.original_authorization;
  return {
    verdict: authorization.verdict,
    reason: authorization.reason,
    graphSnapshot: authorization.graph_version,
    affectedScopes: authorization.affected_scopes,
  };
}

function mapEvaluation(
  evaluation: RawEvaluation | null,
): ScenarioEvaluation | undefined {
  if (!evaluation) return undefined;
  return {
    status: evaluation.status,
    checks: evaluation.checks,
    runtimeMs: evaluation.runtime_ms,
    falsePositiveInvalidations:
      evaluation.false_positive_invalidations.length,
    missedInvalidations: evaluation.missed_invalidations.length,
  };
}

function evidence(raw: RawRun): EvidenceEntry[] {
  const loopEntries: EvidenceEntry[] = [
    ...(raw.agent_loop_state
      ? [
          {
            label: "Agent loop state",
            value: raw.agent_loop_state,
            kind: "code" as const,
          },
        ]
      : []),
    ...(raw.agent_history ?? []).map((entry, index) => ({
      label: `Agent loop ${index + 1}`,
      value: entry,
      kind: "text" as const,
    })),
  ];
  const eventEntries: EvidenceEntry[] = raw.events.map((event) => ({
    label: `${event.sequence}. ${event.label}`,
    value: event.detail,
    kind: "text",
  }));
  const references =
    raw.invalidation_report?.evidence_refs.map((reference, index) => ({
      label: `Evidence reference ${index + 1}`,
      value: reference,
      kind: "code" as const,
    })) ?? [];
  return [...loopEntries, ...eventEntries, ...references];
}

function mapRun(raw: RawRun, scenario: ScenarioDefinition): ScenarioRunState {
  const affectedTaskIds =
    raw.evaluation?.actual_invalidated_task_ids ??
    raw.invalidation_report?.stopped_work_artifact_ids ??
    [];
  return {
    runId: raw.run_id,
    scenarioId: raw.scenario_id,
    status: raw.status,
    activeStage: raw.active_stage,
    graphSnapshot: raw.graph_version,
    agentLoopState: raw.agent_loop_state,
    provenancePath: buildProvenance(raw, scenario),
    outcomes: buildOutcomes(raw, scenario),
    originalGrant: grantView(
      raw.original_grant,
      raw.original_plan,
      raw.old_execution,
      raw.original_grant?.decision_snapshot === raw.graph_version
        ? "active"
        : "issued",
    ),
    replacementGrant: raw.corrected_plan
      ? grantView(
          raw.replacement_grant,
          raw.corrected_plan,
          raw.new_execution,
          "pending",
        )
      : undefined,
    grantRejection:
      raw.old_execution &&
      !raw.old_execution.applied &&
      raw.old_execution.verification_code !== "VALID"
      ? {
          code: raw.old_execution.verification_code,
          message:
            "Execution rejected. The executor did not apply the original authorization.",
          reason: raw.old_execution.reason,
          previousSnapshot:
            raw.original_grant?.decision_snapshot ??
            scenario.originalDecision.graphSnapshot,
          currentSnapshot: raw.graph_version,
          affectedTaskIds,
        }
      : undefined,
    stageResult: stageResult(raw),
    originalPlan: planView(raw.original_plan, "agent"),
    correctedPlan: planView(raw.corrected_plan, "fixture"),
    evaluation: mapEvaluation(raw.evaluation),
    outcomeSummary: mapOutcomeSummary(raw),
    evidence: evidence(raw),
    events: [...raw.events]
      .sort((left, right) => left.sequence - right.sequence)
      .map((event) => ({
        sequence: event.sequence,
        stage: event.stage,
        eventType: event.event_type,
        label: event.label,
        detail: event.detail,
        createdAt: event.created_at,
      })),
    startedAt: raw.started_at,
    completedAt: raw.completed_at ?? undefined,
  };
}

function mapSummary(raw: RawRunSummary): ScenarioRunSummary {
  return {
    runId: raw.run_id,
    scenarioId: raw.scenario_id,
    scenarioName: raw.scenario_name,
    category: raw.category,
    riskLevel: raw.risk_level,
    status: raw.status,
    preservedExpected: raw.preserved_expected,
    preservedActual: raw.preserved_actual,
    preservedExpectedIds: raw.preserved_expected_ids ?? [],
    preservedActualIds: raw.preserved_actual_ids ?? [],
    stoppedExpected: raw.invalidated_expected,
    stoppedActual: raw.invalidated_actual,
    stoppedExpectedIds: raw.invalidated_expected_ids ?? [],
    stoppedActualIds: raw.invalidated_actual_ids ?? [],
    falsePositiveInvalidations: raw.false_positive_invalidations ?? [],
    missedInvalidations: raw.missed_invalidations ?? [],
    oldGrantRejectedExpected: raw.old_grant_rejected_expected,
    oldGrantRejected: raw.old_grant_rejected,
    reauthorizationExpected: raw.reauthorization_expected,
    reauthorizationSucceeded: raw.reauthorization_succeeded,
    planStatus: raw.plan_status,
    needsReviewArtifactIds: raw.needs_review_artifact_ids ?? [],
    oldGrantVerificationCode:
      raw.old_grant_verification_code ?? null,
    replacementAuthorizationVerdict:
      raw.replacement_authorization_verdict ?? null,
    replacementGrantVerificationCode:
      raw.replacement_grant_verification_code ?? null,
    historyScope: raw.history_scope ?? "session",
    runtimeMs: raw.runtime_ms,
    failureReasons: raw.failure_reasons,
    completedAt: raw.completed_at,
    inspectable: raw.inspectable ?? true,
  };
}

export async function loadScenarioCatalog(
  signal?: AbortSignal,
): Promise<{
  scenarios: ScenarioDefinition[];
  runs: ScenarioRunSummary[];
}> {
  const response = await scenarioJson<RawCatalogResponse>(
    "/scenario-lab/scenarios",
    { signal },
  );
  return {
    scenarios: response.scenarios.map(mapScenario),
    runs: response.latest_runs.map(mapSummary),
  };
}

export function createScenarioLabClient(
  scenarios: readonly ScenarioDefinition[],
  existingRuns: readonly ScenarioRunSummary[] = [],
): ScenarioLabClient {
  const scenarioById = new Map(
    scenarios.map((scenario) => [scenario.id, scenario]),
  );
  const runById = new Map<string, ScenarioRunState>();
  const latestRunIdByScenario = new Map(
    existingRuns
      .filter((run) => run.inspectable)
      .map((run) => [run.scenarioId, run.runId]),
  );

  function scenarioFor(id: string): ScenarioDefinition {
    const scenario = scenarioById.get(id);
    if (!scenario) throw new Error(`Unknown scenario: ${id}`);
    return scenario;
  }

  function cacheRun(run: ScenarioRunState): ScenarioRunState {
    runById.set(run.runId, run);
    latestRunIdByScenario.set(run.scenarioId, run.runId);
    return run;
  }

  async function loadRun(
    runId: string,
    scenarioId: string,
    signal?: AbortSignal,
  ): Promise<ScenarioRunState> {
    const cached = runById.get(runId);
    if (cached) {
      if (cached.scenarioId !== scenarioId) {
        throw new Error(`Run ${runId} does not belong to scenario ${scenarioId}.`);
      }
      return cached;
    }
    const raw = await scenarioJson<RawRun>(
      `/scenario-lab/runs/${encodeURIComponent(runId)}`,
      { signal },
    );
    if (raw.scenario_id !== scenarioId) {
      throw new Error(`Run ${runId} does not belong to scenario ${scenarioId}.`);
    }
    return cacheRun(mapRun(raw, scenarioFor(scenarioId)));
  }

  return {
    loadScenarioState: async (scenarioId, options) => {
      const runId = latestRunIdByScenario.get(scenarioId);
      if (!runId) return null;
      return loadRun(runId, scenarioId, options?.signal);
    },
    loadRunState: async (runId, scenarioId, options) => {
      return loadRun(runId, scenarioId, options?.signal);
    },
    startScenario: async (scenarioId, options) => {
      const raw = await scenarioJson<RawRun>("/scenario-lab/runs", {
        method: "POST",
        signal: options?.signal,
        body: JSON.stringify({ scenario_id: scenarioId }),
      });
      return cacheRun(mapRun(raw, scenarioFor(scenarioId)));
    },
    advanceScenario: async (runId, expectedStage, options) => {
      const raw = await scenarioJson<RawRun>(
        `/scenario-lab/runs/${encodeURIComponent(runId)}/advance`,
        {
          method: "POST",
          signal: options?.signal,
          body: JSON.stringify({ expected_stage: expectedStage }),
        },
      );
      return cacheRun(mapRun(raw, scenarioFor(raw.scenario_id)));
    },
    resetScenario: async (scenarioId, options) => {
      await scenarioJson(
        `/scenario-lab/scenarios/${encodeURIComponent(scenarioId)}/reset`,
        {
          method: "POST",
          signal: options?.signal,
          body: "{}",
        },
      );
      for (const [runId, cached] of runById) {
        if (cached.scenarioId === scenarioId) runById.delete(runId);
      }
      latestRunIdByScenario.delete(scenarioId);
      return null;
    },
    loadRunSummaries: async (options) => {
      const response = await scenarioJson<RawRunAllReport>(
        "/scenario-lab/results",
        { signal: options?.signal },
      );
      const runs = response.runs.map(mapSummary);
      runs.forEach((run) => {
        if (run.inspectable) {
          latestRunIdByScenario.set(run.scenarioId, run.runId);
        }
      });
      return runs;
    },
    runAllScenarios: async (scenarioIds, options) => {
      const report = await scenarioJson<RawRunAllReport>(
        "/scenario-lab/run-all",
        {
          method: "POST",
          signal: options?.signal,
          body: JSON.stringify({ scenario_ids: scenarioIds }),
        },
      );
      const runs = report.runs.map(mapSummary);
      const invalidatedScenarios = new Set(scenarioIds);
      for (const [runId, cached] of runById) {
        if (invalidatedScenarios.has(cached.scenarioId)) {
          runById.delete(runId);
        }
      }
      scenarioIds.forEach((scenarioId) => {
        latestRunIdByScenario.delete(scenarioId);
      });
      runs.forEach((run, index) => {
        if (run.inspectable) {
          latestRunIdByScenario.set(run.scenarioId, run.runId);
        }
        options?.onProgress?.(run, index + 1, runs.length);
      });
      return runs;
    },
  };
}
