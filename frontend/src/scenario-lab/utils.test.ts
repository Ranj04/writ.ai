import { describe, expect, it } from "vitest";
import type {
  ScenarioDefinition,
  ScenarioRunState,
  ScenarioRunSummary,
} from "./model";
import {
  DEFAULT_SCENARIO_FILTERS,
  filterScenarios,
  narrativeProgress,
  narrativeStepForRun,
  summarizeRuns,
} from "./utils";

function scenario(
  id: string,
  category: ScenarioDefinition["category"],
  riskLevel: ScenarioDefinition["riskLevel"],
  lastResult: ScenarioDefinition["lastResult"],
): ScenarioDefinition {
  return {
    id,
    name: id,
    category,
    description: "Scenario description",
    riskLevel,
    originalDecision: {
      id: "DEC-001",
      text: "Original decision",
      graphSnapshot: "graph-v17",
    },
    newDecision: {
      id: "DEC-002",
      text: "New decision",
      graphSnapshot: "graph-v18",
      reason: "Approved intent changed.",
    },
    specification: {
      id: "SPEC-001",
      title: "Specification",
      description: "Specification description",
      scopes: ["scope"],
    },
    ticket: {
      id: "TICKET-001",
      title: "Ticket",
      description: "Ticket description",
      scopes: ["scope"],
    },
    tasks: [],
    initialPlan: {
      id: "PLAN-001",
      objective: "Initial objective",
      steps: [],
      scope: ["scope"],
      source: "fixture",
    },
    riskIfContinued: "The agent could perform unauthorized work.",
    expectedOutcomes: [],
    expectedCorrectedBehavior: "The corrected plan narrows scope.",
    lastResult,
  };
}

function run(
  scenarioId: string,
  status: ScenarioRunSummary["status"],
  oldGrantRejected: boolean,
  reauthorizationSucceeded: boolean,
): ScenarioRunSummary {
  return {
    runId: `run-${scenarioId}`,
    scenarioId,
    scenarioName: scenarioId,
    category: "security",
    riskLevel: "high",
    status,
    preservedExpected: 1,
    preservedActual: 1,
    preservedExpectedIds: ["TASK-PRESERVED"],
    preservedActualIds: ["TASK-PRESERVED"],
    stoppedExpected: 1,
    stoppedActual: status === "passed" ? 1 : 0,
    stoppedExpectedIds: ["TASK-STOPPED"],
    stoppedActualIds: status === "passed" ? ["TASK-STOPPED"] : [],
    falsePositiveInvalidations: [],
    missedInvalidations: status === "passed" ? [] : ["TASK-STOPPED"],
    oldGrantRejectedExpected: true,
    oldGrantRejected,
    reauthorizationExpected: true,
    reauthorizationSucceeded,
    oldGrantVerificationCode: oldGrantRejected
      ? "STALE_SNAPSHOT"
      : "VALID",
    replacementGrantVerificationCode: reauthorizationSucceeded
      ? "VALID"
      : "EXPIRED",
    runtimeMs: 120,
    failureReasons: status === "failed" ? ["Mismatch"] : [],
    completedAt: "2026-07-23T12:00:00Z",
    inspectable: true,
  };
}

describe("Scenario Lab utilities", () => {
  const narrativeRun: ScenarioRunState = {
    runId: "RUN-1",
    scenarioId: "scenario",
    status: "running",
    activeStage: "authorized",
    graphSnapshot: "graph-v17",
    provenancePath: { nodes: [], edges: [] },
    outcomes: [],
    evidence: [],
    events: [],
  };

  it("filters without mutating the input collection", () => {
    const scenarios = [
      scenario("compliance", "compliance", "high", "passed"),
      scenario("security", "security", "critical", "failed"),
    ];

    const filtered = filterScenarios(scenarios, {
      ...DEFAULT_SCENARIO_FILTERS,
      category: "security",
      result: "failed",
    });

    expect(filtered.map((item) => item.id)).toEqual(["security"]);
    expect(scenarios).toHaveLength(2);
  });

  it("searches scenario names, descriptions, categories, and decisions", () => {
    const scenarios = [
      scenario("csv-admin", "compliance", "high", "not-run"),
      scenario("regional-data", "data-governance", "critical", "not-run"),
    ];
    scenarios[0] = {
      ...scenarios[0],
      name: "CSV exports become admin-only",
    };
    scenarios[1] = {
      ...scenarios[1],
      newDecision: {
        ...scenarios[1].newDecision,
        text: "Customer records must stay in the United States.",
      },
    };

    expect(
      filterScenarios(scenarios, {
        ...DEFAULT_SCENARIO_FILTERS,
        query: "admin",
      }).map((item) => item.id),
    ).toEqual(["csv-admin"]);
    expect(
      filterScenarios(scenarios, {
        ...DEFAULT_SCENARIO_FILTERS,
        query: "united states",
      }).map((item) => item.id),
    ).toEqual(["regional-data"]);
  });

  it("maps real backend stages onto the five-step narrative", () => {
    expect(narrativeStepForRun(null)).toBe("before");
    expect(narrativeStepForRun(narrativeRun)).toBe("before");
    expect(
      narrativeStepForRun({
        ...narrativeRun,
        activeStage: "decision-changed",
      }),
    ).toBe("decision");
    expect(
      narrativeStepForRun(
        { ...narrativeRun, activeStage: "decision-changed" },
        true,
      ),
    ).toBe("impact");
    expect(
      narrativeStepForRun({
        ...narrativeRun,
        activeStage: "work-stopped",
      }),
    ).toBe("stopped");
    expect(
      narrativeStepForRun({
        ...narrativeRun,
        activeStage: "reauthorized",
      }),
    ).toBe("corrected");
  });

  it("derives five-step narrative progress without changing backend status", () => {
    expect(narrativeProgress("before", "before", "not-run")).toBe(
      "current",
    );
    expect(narrativeProgress("decision", "before", "not-run")).toBe(
      "upcoming",
    );
    expect(narrativeProgress("before", "impact")).toBe("complete");
    expect(narrativeProgress("impact", "impact")).toBe("current");
    expect(narrativeProgress("stopped", "impact")).toBe("upcoming");
    expect(narrativeProgress("corrected", "corrected", "passed")).toBe(
      "complete",
    );
  });

  it("summarizes only measured counts and never invents rates", () => {
    const metrics = summarizeRuns([
      run("one", "passed", true, true),
      run("two", "failed", true, false),
    ]);

    expect(metrics).toEqual({
      completed: 2,
      passed: 1,
      failed: 1,
      grantChecksPassed: 2,
      reauthorizationsPassed: 1,
      invalidationRecall: 50,
      preservationRecall: 100,
      grantRejectionRate: 100,
      reauthorizationRate: 50,
      falsePositiveInvalidations: 0,
      averageRuntimeMs: 120,
    });
  });

  it("returns zero counts when no scenario has run", () => {
    expect(summarizeRuns([])).toEqual({
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
    });
  });
});
