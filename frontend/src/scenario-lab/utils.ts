import type {
  ScenarioDefinition,
  ScenarioFiltersValue,
  ScenarioNarrativeStepId,
  ScenarioResultStatus,
  ScenarioRunState,
  ScenarioRunSummary,
  StageProgress,
} from "./model";

export const DEFAULT_SCENARIO_FILTERS: ScenarioFiltersValue = {
  query: "",
  category: "all",
  riskLevel: "all",
  result: "all",
};

export const SCENARIO_NARRATIVE_STEPS: readonly {
  id: ScenarioNarrativeStepId;
  label: string;
}[] = [
  { id: "before", label: "Before" },
  { id: "decision", label: "Decision approved" },
  { id: "impact", label: "Impact found" },
  { id: "stopped", label: "Work stopped" },
  { id: "corrected", label: "Corrected" },
];

const NARRATIVE_STEP_ORDER = SCENARIO_NARRATIVE_STEPS.map(
  (step) => step.id,
);

export function narrativeStepForRun(
  run: ScenarioRunState | null,
  impactRevealed = false,
): ScenarioNarrativeStepId {
  if (!run || run.activeStage === "authorized") return "before";
  if (run.activeStage === "decision-changed") {
    return impactRevealed ? "impact" : "decision";
  }
  if (run.activeStage === "work-stopped") return "stopped";
  return "corrected";
}

export function narrativeProgress(
  stepId: ScenarioNarrativeStepId,
  activeStep: ScenarioNarrativeStepId,
  runStatus: ScenarioResultStatus = "running",
): StageProgress {
  const stepIndex = NARRATIVE_STEP_ORDER.indexOf(stepId);
  const activeIndex = NARRATIVE_STEP_ORDER.indexOf(activeStep);
  if (runStatus === "not-run") {
    return stepId === "before" ? "current" : "upcoming";
  }
  if (stepIndex < activeIndex) return "complete";
  if (stepIndex === activeIndex) {
    if (runStatus === "passed") return "complete";
    if (runStatus === "failed") return "failed";
    return "current";
  }
  return "upcoming";
}

export function filterScenarios(
  scenarios: readonly ScenarioDefinition[],
  filters: ScenarioFiltersValue,
): ScenarioDefinition[] {
  return scenarios.filter((scenario) => {
    const result = scenario.lastResult ?? "not-run";
    const normalizedQuery = filters.query.trim().toLocaleLowerCase();
    const matchesQuery =
      normalizedQuery.length === 0
      || [
        scenario.name,
        scenario.description,
        formatCategory(scenario.category),
        scenario.newDecision.text,
      ].some((value) => value.toLocaleLowerCase().includes(normalizedQuery));
    return (
      matchesQuery
      && (filters.category === "all" || scenario.category === filters.category)
      && (filters.riskLevel === "all" || scenario.riskLevel === filters.riskLevel)
      && (filters.result === "all" || result === filters.result)
    );
  });
}

export function formatCategory(category: string): string {
  return category
    .split(/[-_]/)
    .map((part) => {
      const normalized =
        part === part.toUpperCase() ? part.toLocaleLowerCase() : part;
      return normalized.charAt(0).toUpperCase() + normalized.slice(1);
    })
    .join(" ");
}

export function formatResult(status: ScenarioResultStatus): string {
  switch (status) {
    case "not-run":
      return "Not run";
    case "running":
      return "Running";
    case "passed":
      return "Passed";
    case "failed":
      return "Failed";
  }
}

export interface RunReportMetrics {
  completed: number;
  passed: number;
  failed: number;
  grantChecksPassed: number;
  reauthorizationsPassed: number;
  invalidationRecall: number | null;
  preservationRecall: number | null;
  grantRejectionRate: number | null;
  reauthorizationRate: number | null;
  falsePositiveInvalidations: number;
  averageRuntimeMs: number;
}

function matchedExpectedIds(
  expected: readonly string[],
  actual: readonly string[],
): number {
  const actualIds = new Set(actual);
  return expected.filter((id) => actualIds.has(id)).length;
}

export function summarizeRuns(
  runs: readonly ScenarioRunSummary[],
): RunReportMetrics {
  if (runs.length === 0) {
    return {
      completed: 0,
      passed: 0,
      failed: 0,
      grantChecksPassed: 0,
      reauthorizationsPassed: 0,
      invalidationRecall: null,
      preservationRecall: null,
      grantRejectionRate: null,
      reauthorizationRate: null,
      falsePositiveInvalidations: 0,
      averageRuntimeMs: 0,
    };
  }

  const passed = runs.filter((run) => run.status === "passed").length;
  const failed = runs.filter((run) => run.status === "failed").length;
  const rejected = runs.filter((run) => run.oldGrantRejected).length;
  const reauthorized = runs.filter((run) => run.reauthorizationSucceeded).length;
  const grantAttempts = runs.filter(
    (run) =>
      (run.oldGrantVerificationCode !== null &&
        run.oldGrantVerificationCode !== undefined) ||
      run.oldGrantRejected,
  ).length;
  const reauthorizationAttempts = runs.filter(
    (run) =>
      (run.replacementGrantVerificationCode !== null &&
        run.replacementGrantVerificationCode !== undefined) ||
      run.reauthorizationSucceeded,
  ).length;
  const expectedInvalidations = runs.reduce(
    (total, run) => total + run.stoppedExpectedIds.length,
    0,
  );
  const correctInvalidations = runs.reduce(
    (total, run) =>
      total + matchedExpectedIds(run.stoppedExpectedIds, run.stoppedActualIds),
    0,
  );
  const expectedPreserved = runs.reduce(
    (total, run) => total + run.preservedExpectedIds.length,
    0,
  );
  const correctlyPreserved = runs.reduce(
    (total, run) =>
      total
      + matchedExpectedIds(run.preservedExpectedIds, run.preservedActualIds),
    0,
  );

  return {
    completed: runs.length,
    passed,
    failed,
    grantChecksPassed: rejected,
    reauthorizationsPassed: reauthorized,
    invalidationRecall:
      expectedInvalidations === 0
        ? null
        : (correctInvalidations / expectedInvalidations) * 100,
    preservationRecall:
      expectedPreserved === 0
        ? null
        : (correctlyPreserved / expectedPreserved) * 100,
    grantRejectionRate:
      grantAttempts === 0 ? null : (rejected / grantAttempts) * 100,
    reauthorizationRate:
      reauthorizationAttempts === 0
        ? null
        : (reauthorized / reauthorizationAttempts) * 100,
    falsePositiveInvalidations: runs.reduce(
      (total, run) => total + run.falsePositiveInvalidations.length,
      0,
    ),
    averageRuntimeMs:
      runs.reduce((total, run) => total + run.runtimeMs, 0) / runs.length,
  };
}
