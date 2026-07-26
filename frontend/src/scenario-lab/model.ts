export type ScenarioCategory =
  | "compliance"
  | "security"
  | "product"
  | "infrastructure"
  | "privacy"
  | "finance"
  | "access-control"
  | "data-governance";

export type RiskLevel = "low" | "medium" | "high" | "critical";

export type ScenarioResultStatus =
  | "not-run"
  | "running"
  | "passed"
  | "failed";

export type ScenarioStageId =
  | "authorized"
  | "decision-changed"
  | "work-stopped"
  | "reauthorized";

export type StageProgress = "complete" | "current" | "upcoming" | "failed";

export type ScenarioNarrativeStepId =
  | "before"
  | "decision"
  | "impact"
  | "stopped"
  | "corrected";

export type OutcomeKind = "preserved" | "stopped" | "newly-required";

export type PlanValidity = "VALID" | "NEEDS_REVIEW" | "INVALIDATED";

export type VerificationCode =
  | "VALID"
  | "INVALID_TOKEN"
  | "NON_ALLOW_VERDICT"
  | "EXPIRED"
  | "BINDING_MISMATCH"
  | "PLAN_HASH_MISMATCH"
  | "STALE_SNAPSHOT"
  | "CURRENT_PLAN_REJECTED";

export type ProvenanceNodeStatus =
  | "valid"
  | "preserved"
  | "changed"
  | "superseded"
  | "needs-review"
  | "stopped"
  | "rejected"
  | "pending"
  | "reauthorized";

export type GrantDisplayStatus =
  | "active"
  | "issued"
  | "rejected"
  | "applied"
  | "pending"
  | "not-applied"
  | "inconsistent";

export interface DecisionSummary {
  id: string;
  text: string;
  graphSnapshot: string;
}

export interface ScenarioOutcome {
  id: string;
  label: string;
  detail?: string;
  kind: OutcomeKind;
  basis?: "expected" | "actual";
  source?: "authority" | "agent" | "fixture";
  representation?: "task" | "plan-action";
  persistedAsGraphArtifact?: boolean;
  lifecycle?: "fixture-preview" | "authorized-plan-action";
}

export interface CorrectiveActionView {
  id: string;
  description: string;
  scopes: readonly string[];
  source: "fixture";
  representation: "plan-action";
  graphArtifactId: null;
  persistedAsGraphArtifact: false;
  lifecycle: "fixture-preview" | "authorized-plan-action";
}

export interface ScenarioOutcomeSummary {
  preservedTaskIds: readonly string[];
  invalidatedTaskIds: readonly string[];
  needsReviewArtifactIds: readonly string[];
  originalPlanId: string;
  originalPlanStatus: PlanValidity | null;
  correctiveActions: readonly CorrectiveActionView[];
  oldGrantVerificationCode: VerificationCode | null;
  replacementAuthorizationVerdict: StageResult["verdict"] | null;
  replacementGrantVerificationCode: VerificationCode | null;
  mayContinue: boolean | null;
  primaryProvenancePath: readonly string[];
  historyScope: "session";
}

export interface ScenarioArtifactSummary {
  id: string;
  title: string;
  description: string;
  scopes: readonly string[];
}

export interface ScenarioTaskSummary extends ScenarioArtifactSummary {
  expectedStatus: "preserved" | "invalidated";
}

export interface ScenarioDefinition {
  id: string;
  name: string;
  category: ScenarioCategory;
  description: string;
  riskLevel: RiskLevel;
  originalDecision: DecisionSummary;
  newDecision: DecisionSummary & {
    reason: string;
  };
  specification: ScenarioArtifactSummary;
  ticket: ScenarioArtifactSummary;
  tasks: readonly ScenarioTaskSummary[];
  initialPlan: PlanView;
  riskIfContinued: string;
  expectedOutcomes: readonly ScenarioOutcome[];
  expectedCorrectedBehavior: string;
  lastResult?: ScenarioResultStatus;
  lastRunAt?: string;
}

export interface ProvenanceNode {
  id: string;
  kind: string;
  title: string;
  status: ProvenanceNodeStatus;
  scopes?: readonly string[];
  invalidatedScopes?: readonly string[];
  synthetic?: boolean;
}

export interface ProvenanceEdge {
  sourceId: string;
  targetId: string;
  relation: string;
  scopes?: readonly string[];
  evidenceRef?: string;
  synthetic?: boolean;
}

export interface ProvenancePath {
  nodes: readonly ProvenanceNode[];
  edges: readonly ProvenanceEdge[];
}

export interface GrantView {
  id: string;
  runId?: string;
  taskId?: string;
  graphSnapshot: string;
  planId: string;
  scope: readonly string[];
  allowedTaskIds?: readonly string[];
  status: GrantDisplayStatus;
  issuedAt?: string;
  expiresAt?: string;
  planHash?: string;
  verificationCode?: string;
}

export interface GrantRejection {
  code: string;
  message: string;
  reason: string;
  previousSnapshot: string;
  currentSnapshot: string;
  affectedTaskIds: readonly string[];
}

export interface StageResult {
  verdict: "ALLOW" | "REPLAN" | "BLOCK" | "HUMAN_REVIEW";
  reason: string;
  graphSnapshot: string;
  affectedScopes: readonly string[];
}

export interface PlanView {
  id: string;
  objective: string;
  steps: readonly string[];
  scope: readonly string[];
  source: "authority" | "agent" | "fixture";
}

export interface EvaluationCheck {
  id: string;
  label: string;
  expected: string;
  actual: string;
  passed: boolean;
}

export interface ScenarioEvaluation {
  status: "passed" | "failed";
  checks: readonly EvaluationCheck[];
  runtimeMs: number;
  falsePositiveInvalidations: number;
  missedInvalidations: number;
}

export interface EvidenceEntry {
  label: string;
  value: string;
  kind?: "text" | "code" | "timestamp";
}

export interface ScenarioEventView {
  sequence: number;
  stage: ScenarioStageId;
  eventType: string;
  label: string;
  detail: string;
  createdAt: string;
}

export interface ScenarioRunState {
  runId: string;
  scenarioId: string;
  status: ScenarioResultStatus;
  activeStage: ScenarioStageId;
  graphSnapshot: string;
  agentLoopState?: string;
  provenancePath: ProvenancePath;
  outcomes: readonly ScenarioOutcome[];
  originalGrant?: GrantView;
  replacementGrant?: GrantView;
  grantRejection?: GrantRejection;
  stageResult?: StageResult;
  originalPlan?: PlanView;
  correctedPlan?: PlanView;
  evaluation?: ScenarioEvaluation;
  outcomeSummary?: ScenarioOutcomeSummary;
  evidence: readonly EvidenceEntry[];
  events: readonly ScenarioEventView[];
  startedAt?: string;
  completedAt?: string;
}

export interface ScenarioRunSummary {
  runId: string;
  scenarioId: string;
  scenarioName: string;
  category: ScenarioCategory;
  riskLevel: RiskLevel;
  status: "passed" | "failed";
  preservedExpected: number;
  preservedActual: number;
  preservedExpectedIds: readonly string[];
  preservedActualIds: readonly string[];
  stoppedExpected: number;
  stoppedActual: number;
  stoppedExpectedIds: readonly string[];
  stoppedActualIds: readonly string[];
  falsePositiveInvalidations: readonly string[];
  missedInvalidations: readonly string[];
  oldGrantRejectedExpected: boolean;
  oldGrantRejected: boolean;
  reauthorizationExpected: boolean;
  reauthorizationSucceeded: boolean;
  planStatus?: PlanValidity;
  needsReviewArtifactIds?: readonly string[];
  oldGrantVerificationCode?: VerificationCode | null;
  replacementAuthorizationVerdict?: StageResult["verdict"] | null;
  replacementGrantVerificationCode?: VerificationCode | null;
  historyScope?: "session";
  runtimeMs: number;
  failureReasons: readonly string[];
  completedAt: string;
  inspectable: boolean;
}

export interface ScenarioFiltersValue {
  query: string;
  category: ScenarioCategory | "all";
  riskLevel: RiskLevel | "all";
  result: ScenarioResultStatus | "all";
}

export interface ScenarioLabData {
  scenarios: readonly ScenarioDefinition[];
  runs?: readonly ScenarioRunSummary[];
  graphSnapshot?: string;
  servicesOnline?: number;
  servicesTotal?: number;
}

export interface ScenarioActionOptions {
  signal?: AbortSignal;
}

export interface RunAllOptions extends ScenarioActionOptions {
  onProgress?: (result: ScenarioRunSummary, completed: number, total: number) => void;
}

/**
 * The UI only renders values returned by this client. In particular, it does
 * not mint grants, create signed tokens, infer verdicts, or simulate graph
 * traversal in the browser.
 */
export interface ScenarioLabClient {
  loadScenarioState?(
    scenarioId: string,
    options?: ScenarioActionOptions,
  ): Promise<ScenarioRunState | null>;
  loadRunState?(
    runId: string,
    scenarioId: string,
    options?: ScenarioActionOptions,
  ): Promise<ScenarioRunState | null>;
  startScenario(
    scenarioId: string,
    options?: ScenarioActionOptions,
  ): Promise<ScenarioRunState>;
  advanceScenario(
    runId: string,
    expectedStage: ScenarioStageId,
    options?: ScenarioActionOptions,
  ): Promise<ScenarioRunState>;
  resetScenario(
    scenarioId: string,
    options?: ScenarioActionOptions,
  ): Promise<ScenarioRunState | null>;
  loadRunSummaries?(
    options?: ScenarioActionOptions,
  ): Promise<readonly ScenarioRunSummary[]>;
  runAllScenarios(
    scenarioIds: readonly string[],
    options?: RunAllOptions,
  ): Promise<readonly ScenarioRunSummary[]>;
}
