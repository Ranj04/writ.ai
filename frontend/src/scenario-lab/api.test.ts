import { afterEach, describe, expect, it, vi } from "vitest";
import type { ScenarioDefinition, ScenarioRunSummary } from "./model";
import { createScenarioLabClient, grantDisplayStatus } from "./api";

const SCENARIO: ScenarioDefinition = {
  id: "scenario-one",
  name: "Scenario one",
  category: "security",
  description: "A deterministic scenario.",
  riskLevel: "high",
  originalDecision: {
    id: "DEC-001",
    text: "Original decision",
    graphSnapshot: "graph-v17",
  },
  newDecision: {
    id: "DEC-002",
    text: "Replacement decision",
    graphSnapshot: "graph-v18",
    reason: "Approved intent changed.",
  },
  specification: {
    id: "SPEC-001",
    title: "Scenario specification",
    description: "Specification detail.",
    scopes: ["export"],
  },
  ticket: {
    id: "TICKET-001",
    title: "Scenario ticket",
    description: "Ticket detail.",
    scopes: ["export"],
  },
  tasks: [
    {
      id: "TASK-001",
      title: "Scenario task",
      description: "Task detail.",
      scopes: ["export"],
      expectedStatus: "invalidated",
    },
  ],
  initialPlan: {
    id: "PLAN-001",
    objective: "Execute the original plan.",
    steps: [],
    scope: [],
    source: "fixture",
  },
  riskIfContinued: "Unauthorized work could continue.",
  expectedOutcomes: [
    {
      id: "TASK-KEEP",
      label: "Keep sibling work",
      kind: "preserved",
      basis: "expected",
    },
    {
      id: "TASK-001",
      label: "Stop conflicting work",
      kind: "stopped",
      basis: "expected",
    },
    {
      id: "EXPECTED-ACTION",
      label: "Add approval gate",
      kind: "newly-required",
      basis: "expected",
    },
  ],
  expectedCorrectedBehavior: "Narrow the plan.",
};

function rawRun(runId: string, activeStage = "authorized") {
  return {
    run_id: runId,
    context_id: `context-${runId}`,
    scenario_id: SCENARIO.id,
    status: "running",
    active_stage: activeStage,
    graph_version: "graph-v17",
    artifacts: [],
    edges: [],
    started_at: "2026-07-23T12:00:00Z",
    completed_at: null,
    original_plan: {
      id: "PLAN-001",
      ticket_id: "TICKET-001",
      objective: "Execute the original plan.",
      actions: [],
    },
    corrected_plan: null,
    original_authorization: {
      verdict: "ALLOW",
      reason: "Current plan is authorized.",
      graph_version: "graph-v17",
      task_id: "TASK-001",
      affected_scopes: [],
      invalidation_path: [],
      invalidated_artifact_ids: [],
      preserved_artifact_ids: [],
      evidence_refs: [],
      grant: null,
    },
    conflict_authorization: null,
    corrected_authorization: null,
    original_grant: null,
    replacement_grant: null,
    invalidation_report: null,
    old_execution: null,
    new_execution: null,
    events: [],
    evaluation: null,
  };
}

function summary(runId: string): ScenarioRunSummary {
  return {
    runId,
    scenarioId: SCENARIO.id,
    scenarioName: SCENARIO.name,
    category: SCENARIO.category,
    riskLevel: SCENARIO.riskLevel,
    status: "passed",
    preservedExpected: 0,
    preservedActual: 0,
    preservedExpectedIds: [],
    preservedActualIds: [],
    stoppedExpected: 0,
    stoppedActual: 0,
    stoppedExpectedIds: [],
    stoppedActualIds: [],
    falsePositiveInvalidations: [],
    missedInvalidations: [],
    oldGrantRejectedExpected: true,
    oldGrantRejected: true,
    reauthorizationExpected: true,
    reauthorizationSucceeded: true,
    runtimeMs: 10,
    failureReasons: [],
    completedAt: "2026-07-23T12:00:01Z",
    inspectable: true,
  };
}

function rawSummary(runId: string) {
  const run = summary(runId);
  return {
    run_id: run.runId,
    scenario_id: run.scenarioId,
    scenario_name: run.scenarioName,
    category: run.category,
    risk_level: run.riskLevel,
    status: run.status,
    preserved_expected: run.preservedExpected,
    preserved_actual: run.preservedActual,
    preserved_expected_ids: run.preservedExpectedIds,
    preserved_actual_ids: run.preservedActualIds,
    invalidated_expected: run.stoppedExpected,
    invalidated_actual: run.stoppedActual,
    invalidated_expected_ids: run.stoppedExpectedIds,
    invalidated_actual_ids: run.stoppedActualIds,
    false_positive_invalidations: run.falsePositiveInvalidations,
    missed_invalidations: run.missedInvalidations,
    old_grant_rejected_expected: run.oldGrantRejectedExpected,
    old_grant_rejected: run.oldGrantRejected,
    reauthorization_expected: run.reauthorizationExpected,
    reauthorization_succeeded: run.reauthorizationSucceeded,
    runtime_ms: run.runtimeMs,
    failure_reasons: run.failureReasons,
    completed_at: run.completedAt,
    inspectable: run.inspectable,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Scenario Lab grant presentation", () => {
  it("uses both executor application state and verification code", () => {
    expect(grantDisplayStatus(null, "active")).toBe("active");
    expect(
      grantDisplayStatus(
        { applied: true, verification_code: "VALID" },
        "pending",
      ),
    ).toBe("applied");
    expect(
      grantDisplayStatus(
        { applied: false, verification_code: "STALE_SNAPSHOT" },
        "active",
      ),
    ).toBe("rejected");
    expect(
      grantDisplayStatus(
        { applied: false, verification_code: "VALID" },
        "pending",
      ),
    ).toBe("not-applied");
    expect(
      grantDisplayStatus(
        { applied: true, verification_code: "BINDING_MISMATCH" },
        "pending",
      ),
    ).toBe("inconsistent");
  });
});

describe("Scenario Lab API client", () => {
  it("maps the additive semantic outcome contract without merging plan review into task counts", async () => {
    const semanticRun = {
      ...rawRun("run-semantic", "reauthorized"),
      status: "passed",
      graph_version: "graph-v18",
      artifacts: [
        {
          id: "TASK-KEEP",
          kind: "Task",
          title: "Keep sibling work",
          text: "Safe sibling.",
          scopes: ["safe"],
          validity: "VALID",
          invalidated_scopes: [],
        },
        {
          id: "TASK-001",
          kind: "Task",
          title: "Stop conflicting work",
          text: "Conflicting sibling.",
          scopes: ["export"],
          validity: "INVALIDATED",
          invalidated_scopes: ["export"],
        },
        {
          id: "PLAN-001",
          kind: "AgentPlan",
          title: "Original plan",
          text: "Original plan.",
          scopes: ["export", "safe"],
          validity: "NEEDS_REVIEW",
          invalidated_scopes: ["export"],
        },
      ],
      invalidation_report: {
        graph_version: "graph-v18",
        changed_decision_id: "DEC-002",
        superseded_decision_id: "DEC-001",
        affected_scopes: ["export"],
        affected_artifact_ids: ["TASK-001", "PLAN-001"],
        upstream_chain_artifact_ids: ["DEC-002", "DEC-001"],
        stopped_work_artifact_ids: ["TASK-001", "PLAN-001"],
        directly_mentioned_artifact_ids: [],
        preserved_artifact_ids: ["TASK-KEEP"],
        preserved_task_ids: ["TASK-KEEP"],
        invalidated_task_ids: ["TASK-001"],
        needs_review_artifact_ids: ["PLAN-001"],
        paths: [
          {
            artifact_id: "TASK-001",
            node_ids: [
              "DEC-002",
              "DEC-001",
              "TASK-001",
              "PLAN-001",
            ],
          },
        ],
        evidence_refs: ["fixture://decision"],
      },
      outcome_summary: {
        preserved_task_ids: ["TASK-KEEP"],
        invalidated_task_ids: ["TASK-001"],
        needs_review_artifact_ids: ["PLAN-001"],
        original_plan_id: "PLAN-001",
        original_plan_status: "NEEDS_REVIEW",
        corrective_actions: [
          {
            id: "ACTION-NEW",
            description: "Add an approval gate.",
            scopes: ["export"],
            source: "fixture",
            representation: "plan-action",
            graph_artifact_id: null,
            persisted_as_graph_artifact: false,
            lifecycle: "authorized-plan-action",
          },
        ],
        old_grant_verification_code: "STALE_SNAPSHOT",
        replacement_authorization_verdict: "ALLOW",
        replacement_grant_verification_code: "VALID",
        may_continue: true,
        primary_provenance_path: [
          "DEC-002",
          "DEC-001",
          "TASK-001",
          "PLAN-001",
        ],
        history_scope: "session",
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        return new Response(JSON.stringify(semanticRun), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    const client = createScenarioLabClient([SCENARIO]);

    const state = await client.startScenario(SCENARIO.id);

    expect(state.outcomeSummary).toMatchObject({
      preservedTaskIds: ["TASK-KEEP"],
      invalidatedTaskIds: ["TASK-001"],
      needsReviewArtifactIds: ["PLAN-001"],
      originalPlanStatus: "NEEDS_REVIEW",
      oldGrantVerificationCode: "STALE_SNAPSHOT",
      replacementGrantVerificationCode: "VALID",
      mayContinue: true,
      historyScope: "session",
    });
    expect(
      state.outcomes.filter((outcome) => outcome.kind === "stopped"),
    ).toHaveLength(1);
    expect(
      state.outcomes.find((outcome) => outcome.id === "ACTION-NEW"),
    ).toMatchObject({
      representation: "plan-action",
      persistedAsGraphArtifact: false,
      lifecycle: "authorized-plan-action",
    });
  });

  it("keeps expected newly required work visible before a corrected plan exists", async () => {
    const stoppedRun = {
      ...rawRun("run-stopped", "work-stopped"),
      graph_version: "graph-v18",
      artifacts: [
        {
          id: "TASK-KEEP",
          kind: "Task",
          title: "Keep sibling work",
          text: "Safe sibling.",
          scopes: ["safe"],
          validity: "VALID",
          invalidated_scopes: [],
        },
        {
          id: "TASK-001",
          kind: "Task",
          title: "Stop conflicting work",
          text: "Conflicting sibling.",
          scopes: ["export"],
          validity: "INVALIDATED",
          invalidated_scopes: ["export"],
        },
      ],
      invalidation_report: {
        graph_version: "graph-v18",
        changed_decision_id: "DEC-002",
        superseded_decision_id: "DEC-001",
        affected_scopes: ["export"],
        affected_artifact_ids: ["TASK-001"],
        upstream_chain_artifact_ids: ["DEC-002", "DEC-001"],
        stopped_work_artifact_ids: ["TASK-001"],
        directly_mentioned_artifact_ids: [],
        preserved_artifact_ids: ["TASK-KEEP"],
        paths: [
          {
            artifact_id: "TASK-001",
            node_ids: ["DEC-002", "DEC-001", "TASK-001"],
          },
        ],
        evidence_refs: ["fixture://decision"],
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        return new Response(JSON.stringify(stoppedRun), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    const client = createScenarioLabClient([SCENARIO]);

    const state = await client.startScenario(SCENARIO.id);

    expect(
      state.outcomes.find((outcome) => outcome.kind === "newly-required"),
    ).toMatchObject({
      id: "EXPECTED-ACTION",
      basis: "expected",
    });
    expect(state.outcomes.map((outcome) => outcome.kind)).toEqual([
      "preserved",
      "stopped",
      "newly-required",
    ]);
  });

  it("maps the complete branched graph, payload links, and ordered event ledger", async () => {
    const completeRun = {
      ...rawRun("run-complete", "reauthorized"),
      status: "passed",
      graph_version: "graph-v18",
      artifacts: [
        {
          id: "DEC-001",
          kind: "Decision",
          title: "Original decision",
          text: "Original.",
          scopes: ["export", "safe"],
          validity: "VALID",
          invalidated_scopes: [],
        },
        {
          id: "DEC-002",
          kind: "Decision",
          title: "Replacement decision",
          text: "Replacement.",
          scopes: ["export"],
          validity: "VALID",
          invalidated_scopes: [],
        },
        {
          id: "SPEC-001",
          kind: "Specification",
          title: "Specification",
          text: "Specification.",
          scopes: ["export", "safe"],
          validity: "NEEDS_REVIEW",
          invalidated_scopes: ["export"],
        },
        {
          id: "TICKET-001",
          kind: "Ticket",
          title: "Ticket",
          text: "Ticket.",
          scopes: ["export", "safe"],
          validity: "NEEDS_REVIEW",
          invalidated_scopes: ["export"],
        },
        {
          id: "TASK-KEEP",
          kind: "Task",
          title: "Keep sibling",
          text: "Safe.",
          scopes: ["safe"],
          validity: "VALID",
          invalidated_scopes: [],
        },
        {
          id: "TASK-001",
          kind: "Task",
          title: "Stop sibling",
          text: "Conflict.",
          scopes: ["export"],
          validity: "INVALIDATED",
          invalidated_scopes: ["export"],
        },
        {
          id: "PLAN-001",
          kind: "AgentPlan",
          title: "Original plan",
          text: "Original plan.",
          scopes: ["export", "safe"],
          validity: "NEEDS_REVIEW",
          invalidated_scopes: ["export"],
        },
      ],
      edges: [
        {
          source_id: "DEC-002",
          target_id: "DEC-001",
          kind: "SUPERSEDES",
          scopes: ["export"],
          evidence_ref: "fixture://decision",
        },
        {
          source_id: "DEC-001",
          target_id: "SPEC-001",
          kind: "BASIS_FOR",
          scopes: ["export", "safe"],
          evidence_ref: "fixture://spec",
        },
        {
          source_id: "SPEC-001",
          target_id: "TICKET-001",
          kind: "CREATES",
          scopes: ["export", "safe"],
          evidence_ref: "fixture://ticket",
        },
        {
          source_id: "TICKET-001",
          target_id: "TASK-KEEP",
          kind: "DECOMPOSES_TO",
          scopes: ["safe"],
          evidence_ref: "fixture://task-keep",
        },
        {
          source_id: "TICKET-001",
          target_id: "TASK-001",
          kind: "DECOMPOSES_TO",
          scopes: ["export"],
          evidence_ref: "fixture://task-stop",
        },
        {
          source_id: "TASK-KEEP",
          target_id: "PLAN-001",
          kind: "CURRENTLY_DRIVES",
          scopes: ["safe"],
          evidence_ref: "fixture://plan",
        },
        {
          source_id: "TASK-001",
          target_id: "PLAN-001",
          kind: "CURRENTLY_DRIVES",
          scopes: ["export"],
          evidence_ref: "fixture://plan",
        },
      ],
      original_plan: {
        id: "PLAN-001",
        ticket_id: "TICKET-001",
        objective: "Execute the original plan.",
        actions: [
          {
            id: "ACTION-OLD",
            description: "Perform export.",
            scopes: ["export"],
            attributes: { task_id: "TASK-001" },
          },
        ],
      },
      corrected_plan: {
        id: "PLAN-002",
        ticket_id: "TICKET-001",
        objective: "Execute a corrected plan.",
        actions: [
          {
            id: "ACTION-OLD",
            description: "Perform narrowed export.",
            scopes: ["export"],
            attributes: { task_id: "TASK-001" },
          },
          {
            id: "ACTION-NEW",
            description: "Add approval gate.",
            scopes: ["export"],
            attributes: { task_id: "TASK-001" },
          },
        ],
      },
      original_grant: {
        authorization_id: "GRANT-OLD",
        run_id: "run-complete",
        task_id: "TICKET-001",
        decision_snapshot: "graph-v17",
        plan_hash: "old-hash",
        verdict: "ALLOW",
        issued_at: "2026-07-23T12:00:00Z",
        expires_at: "2026-07-23T13:00:00Z",
      },
      replacement_grant: {
        authorization_id: "GRANT-NEW",
        run_id: "run-complete",
        task_id: "TICKET-001",
        decision_snapshot: "graph-v18",
        plan_hash: "new-hash",
        verdict: "ALLOW",
        issued_at: "2026-07-23T12:00:03Z",
        expires_at: "2026-07-23T13:00:03Z",
      },
      corrected_authorization: {
        verdict: "ALLOW",
        reason: "Corrected plan is current.",
        graph_version: "graph-v18",
        task_id: "TICKET-001",
        affected_scopes: [],
        invalidation_path: [],
        invalidated_artifact_ids: [],
        preserved_artifact_ids: ["TASK-KEEP"],
        evidence_refs: [],
        grant: null,
      },
      old_execution: {
        applied: false,
        reason: "Grant snapshot is stale.",
        verification_code: "STALE_SNAPSHOT",
      },
      new_execution: {
        applied: true,
        reason: "Grant is valid.",
        verification_code: "VALID",
      },
      invalidation_report: {
        graph_version: "graph-v18",
        changed_decision_id: "DEC-002",
        superseded_decision_id: "DEC-001",
        affected_scopes: ["export"],
        affected_artifact_ids: ["SPEC-001", "TICKET-001", "TASK-001", "PLAN-001"],
        upstream_chain_artifact_ids: ["DEC-002", "DEC-001", "SPEC-001", "TICKET-001"],
        stopped_work_artifact_ids: ["TASK-001", "PLAN-001"],
        directly_mentioned_artifact_ids: [],
        preserved_artifact_ids: ["TASK-KEEP"],
        paths: [
          {
            artifact_id: "TASK-001",
            node_ids: [
              "DEC-002",
              "DEC-001",
              "SPEC-001",
              "TICKET-001",
              "TASK-001",
              "PLAN-001",
            ],
          },
        ],
        evidence_refs: ["fixture://decision"],
      },
      events: [
        {
          sequence: 2,
          stage: "decision-changed",
          event_type: "decision.changed",
          label: "Decision changed",
          detail: "Graph moved to graph-v18.",
          created_at: "2026-07-23T12:00:02Z",
          data: {},
        },
        {
          sequence: 1,
          stage: "authorized",
          event_type: "grant.issued",
          label: "Grant issued",
          detail: "Original grant issued.",
          created_at: "2026-07-23T12:00:01Z",
          data: {},
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        return new Response(JSON.stringify(completeRun), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    const client = createScenarioLabClient([SCENARIO]);

    const state = await client.startScenario(SCENARIO.id);
    const statusById = new Map(
      state.provenancePath.nodes.map((node) => [node.id, node.status]),
    );

    expect(statusById.get("TASK-KEEP")).toBe("preserved");
    expect(statusById.get("TASK-001")).toBe("stopped");
    expect(statusById.get("PLAN-001")).toBe("needs-review");
    expect(statusById.get("GRANT-OLD")).toBe("rejected");
    expect(statusById.get("PLAN-002")).toBe("reauthorized");
    expect(statusById.get("GRANT-NEW")).toBe("reauthorized");
    expect(
      state.provenancePath.nodes.find((node) => node.id === "TASK-001"),
    ).toMatchObject({
      scopes: ["export"],
      invalidatedScopes: ["export"],
    });
    expect(
      state.provenancePath.edges.find(
        (edge) =>
          edge.sourceId === "TICKET-001" &&
          edge.targetId === "TASK-001",
      ),
    ).toMatchObject({
      scopes: ["export"],
      evidenceRef: "fixture://task-stop",
    });
    expect(
      state.provenancePath.edges.some(
        (edge) =>
          edge.sourceId === "PLAN-002" &&
          edge.targetId === "GRANT-NEW" &&
          edge.synthetic,
      ),
    ).toBe(true);
    expect(
      state.outcomes.find((outcome) => outcome.id === "ACTION-NEW"),
    ).toMatchObject({
      kind: "newly-required",
      basis: "actual",
    });
    expect(state.events.map((event) => event.sequence)).toEqual([1, 2]);
  });

  it("loads and caches report details by the exact run ID", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        requests.push(String(input));
        return new Response(JSON.stringify(rawRun("run-exact")), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    const client = createScenarioLabClient([SCENARIO]);

    const first = await client.loadRunState?.("run-exact", SCENARIO.id);
    const second = await client.loadRunState?.("run-exact", SCENARIO.id);

    expect(first?.runId).toBe("run-exact");
    expect(second?.runId).toBe("run-exact");
    expect(requests).toEqual([
      "http://localhost:8002/scenario-lab/runs/run-exact",
    ]);
  });

  it("invalidates scenario run caches after run-all", async () => {
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input);
        requests.push(url);
        if (url.endsWith("/scenario-lab/run-all")) {
          return new Response(
            JSON.stringify({ runs: [rawSummary("run-new")] }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        return new Response(JSON.stringify(rawRun("run-old")), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }),
    );
    const client = createScenarioLabClient([SCENARIO], [summary("run-old")]);

    await client.loadRunState?.("run-old", SCENARIO.id);
    await client.runAllScenarios([SCENARIO.id]);
    await client.loadRunState?.("run-old", SCENARIO.id);

    expect(
      requests.filter((url) => url.endsWith("/scenario-lab/runs/run-old")),
    ).toHaveLength(2);
  });

  it("sends the current stage as the advance precondition", async () => {
    let requestBody = "";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
        requestBody = String(init?.body);
        return new Response(
          JSON.stringify(rawRun("run-advance", "decision-changed")),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }),
    );
    const client = createScenarioLabClient([SCENARIO]);

    await client.advanceScenario("run-advance", "authorized");

    expect(JSON.parse(requestBody)).toEqual({ expected_stage: "authorized" });
  });
});
