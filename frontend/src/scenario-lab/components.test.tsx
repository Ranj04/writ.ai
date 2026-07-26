import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type {
  ScenarioDefinition,
  ScenarioRunState,
  ScenarioRunSummary,
} from "./model";
import { ScenarioNarrativeRail } from "./components/ScenarioNarrativeRail";
import { RunReport } from "./components/RunReport";
import { ScenarioRunView } from "./components/ScenarioRunView";
import { AppShell } from "./components/AppShell";
import { KnowledgeGraphView } from "./components/KnowledgeGraphView";

const scenario: ScenarioDefinition = {
  id: "csv-exports-admin-only",
  name: "CSV exports become admin-only",
  category: "compliance",
  description: "CSV exports narrow from all users to administrators.",
  riskLevel: "high",
  originalDecision: {
    id: "DEC-004",
    text: "CSV exports are available to all users.",
    graphSnapshot: "graph-v17",
  },
  newDecision: {
    id: "DEC-018",
    text: "CSV exports are restricted to administrators.",
    graphSnapshot: "graph-v18",
    reason: "Reduce exposure of account data.",
  },
  specification: {
    id: "SPEC-009",
    title: "CSV export",
    description: "Export account data.",
    scopes: ["exports.csv"],
  },
  ticket: {
    id: "TICKET-100",
    title: "Implement CSV export",
    description: "Build the export workflow.",
    scopes: ["exports.csv"],
  },
  tasks: [],
  initialPlan: {
    id: "PLAN-027",
    objective: "Ship CSV export",
    steps: ["Generate CSV files"],
    scope: ["exports.csv"],
    source: "agent",
  },
  riskIfContinued: "Standard users would retain access.",
  expectedOutcomes: [],
  expectedCorrectedBehavior: "Require administrator access.",
};

const run: ScenarioRunState = {
  runId: "RUN-27",
  scenarioId: scenario.id,
  status: "passed",
  activeStage: "reauthorized",
  graphSnapshot: "graph-v18",
  agentLoopState: "COMPLETE",
  provenancePath: {
    nodes: [
      {
        id: "DEC-018",
        kind: "decision",
        title: "Admin-only exports",
        status: "changed",
      },
      {
        id: "SPEC-009",
        kind: "specification",
        title: "CSV export",
        status: "needs-review",
      },
      {
        id: "TICKET-100",
        kind: "ticket",
        title: "Implement CSV export",
        status: "needs-review",
      },
      {
        id: "TASK-104",
        kind: "task",
        title: "Display exports to standard users",
        status: "stopped",
      },
    ],
    edges: [
      {
        sourceId: "DEC-018",
        targetId: "SPEC-009",
        relation: "GOVERNS",
      },
    ],
  },
  outcomes: [
    {
      id: "TASK-101",
      label: "Generate CSV files",
      kind: "preserved",
      basis: "actual",
      representation: "task",
    },
    {
      id: "TASK-104",
      label: "Display exports to standard users",
      kind: "stopped",
      basis: "actual",
      representation: "task",
    },
    {
      id: "ACTION-201",
      label: "Add administrator role check",
      kind: "newly-required",
      basis: "actual",
      source: "fixture",
      representation: "plan-action",
      persistedAsGraphArtifact: false,
      lifecycle: "authorized-plan-action",
    },
  ],
  originalGrant: {
    id: "AUTH-17",
    graphSnapshot: "graph-v17",
    planId: "PLAN-027",
    scope: ["exports.csv"],
    status: "rejected",
    verificationCode: "STALE_SNAPSHOT",
  },
  replacementGrant: {
    id: "AUTH-18",
    graphSnapshot: "graph-v18",
    planId: "PLAN-028",
    scope: ["exports.csv"],
    status: "applied",
    verificationCode: "VALID",
  },
  originalPlan: {
    ...scenario.initialPlan,
  },
  correctedPlan: {
    id: "PLAN-028",
    objective: "Ship CSV export for administrators",
    steps: ["Generate CSV files", "Require administrator access"],
    scope: ["exports.csv"],
    source: "fixture",
  },
  outcomeSummary: {
    preservedTaskIds: ["TASK-101", "TASK-102", "TASK-103"],
    invalidatedTaskIds: ["TASK-104", "TASK-105"],
    needsReviewArtifactIds: ["PLAN-027"],
    originalPlanId: "PLAN-027",
    originalPlanStatus: "NEEDS_REVIEW",
    correctiveActions: [],
    oldGrantVerificationCode: "STALE_SNAPSHOT",
    replacementAuthorizationVerdict: "ALLOW",
    replacementGrantVerificationCode: "VALID",
    mayContinue: true,
    primaryProvenancePath: [],
    historyScope: "session",
  },
  evidence: [],
  events: [
    {
      sequence: 1,
      stage: "authorized",
      eventType: "authorization.issued",
      label: "Original authorization issued",
      detail: "Bound to the original graph.",
      createdAt: "2026-07-23T19:41:02-07:00",
    },
    {
      sequence: 2,
      stage: "authorized",
      eventType: "agent.work.started",
      label: "Agent begins work",
      detail: "The plan is active.",
      createdAt: "2026-07-23T19:41:03-07:00",
    },
    {
      sequence: 3,
      stage: "decision-changed",
      eventType: "decision.received",
      label: "New decision received",
      detail: "Admin-only exports.",
      createdAt: "2026-07-23T19:42:10-07:00",
    },
    {
      sequence: 4,
      stage: "decision-changed",
      eventType: "graph.impact.identified",
      label: "Impacted nodes identified",
      detail: "Related work was found.",
      createdAt: "2026-07-23T19:42:11-07:00",
    },
    {
      sequence: 5,
      stage: "decision-changed",
      eventType: "graph.work.invalidated",
      label: "Conflicting work invalidated",
      detail: "One task stopped.",
      createdAt: "2026-07-23T19:42:12-07:00",
    },
    {
      sequence: 6,
      stage: "work-stopped",
      eventType: "executor.rejected",
      label: "Executor rejects old grant",
      detail: "The authorization is stale.",
      createdAt: "2026-07-23T19:42:13-07:00",
    },
    {
      sequence: 7,
      stage: "reauthorized",
      eventType: "executor.resumed",
      label: "Execution resumes",
      detail: "The corrected plan may continue.",
      createdAt: "2026-07-23T19:42:14-07:00",
    },
  ],
};

const summary: ScenarioRunSummary = {
  runId: run.runId,
  scenarioId: scenario.id,
  scenarioName: scenario.name,
  category: scenario.category,
  riskLevel: scenario.riskLevel,
  status: "passed",
  preservedExpected: 3,
  preservedActual: 3,
  preservedExpectedIds: ["TASK-101", "TASK-102", "TASK-103"],
  preservedActualIds: ["TASK-101", "TASK-102", "TASK-103"],
  stoppedExpected: 2,
  stoppedActual: 2,
  stoppedExpectedIds: ["TASK-104", "TASK-105"],
  stoppedActualIds: ["TASK-104", "TASK-105"],
  falsePositiveInvalidations: [],
  missedInvalidations: [],
  oldGrantRejectedExpected: true,
  oldGrantRejected: true,
  reauthorizationExpected: true,
  reauthorizationSucceeded: true,
  planStatus: "NEEDS_REVIEW",
  needsReviewArtifactIds: ["PLAN-027"],
  oldGrantVerificationCode: "STALE_SNAPSHOT",
  replacementAuthorizationVerdict: "ALLOW",
  replacementGrantVerificationCode: "VALID",
  historyScope: "session",
  runtimeMs: 91,
  failureReasons: [],
  completedAt: "2026-07-23T12:00:00Z",
  inspectable: true,
};

describe("Examples executive story components", () => {
  it("uses the simplified primary navigation with Workspace active", () => {
    const html = renderToStaticMarkup(
      <AppShell activeView="workspace" onNavigate={() => undefined}>
        <p>Workspace</p>
      </AppShell>,
    );
    expect(html).toContain('aria-label="Primary navigation"');
    expect(html).toContain('href="/" aria-current="page"');
    expect(html).toContain(">Examples</button>");
    expect(html).not.toContain("Guided Proof");
    expect(html).not.toContain("Live Workspace");
    expect(html).not.toContain(">Scenario Lab</button>");
    expect(html).not.toContain("Run report");
  });

  it("keeps Examples active while preserving Workspace as the home route", () => {
    const html = renderToStaticMarkup(
      <AppShell activeView="catalog" onNavigate={() => undefined}>
        <p>Catalog</p>
      </AppShell>,
    );
    expect(html).toContain(
      'class="sl-nav-link" aria-current="page">Examples',
    );
    expect(html).not.toContain(
      'href="/" aria-current="page"',
    );
  });

  it("links to /approvals without changing how the other views behave", () => {
    // The audit item Lane D left to an integrator: a photogenic screen with no
    // way in reads as a mockup. The link is a plain anchor, so this shell never
    // becomes responsible for rendering the approvals route.
    const workspace = renderToStaticMarkup(
      <AppShell activeView="workspace" onNavigate={() => undefined}>
        <p>Workspace</p>
      </AppShell>,
    );
    expect(workspace).toContain('href="/approvals"');
    expect(workspace).toContain(">Approvals</a>");
    // Workspace is still the current page; the new entry is not.
    expect(workspace).toContain('href="/" aria-current="page"');
    expect(workspace).not.toContain('href="/approvals" aria-current="page"');

    const approvals = renderToStaticMarkup(
      <AppShell activeView="approvals" onNavigate={() => undefined}>
        <p>Approvals</p>
      </AppShell>,
    );
    expect(approvals).toContain('href="/approvals" aria-current="page"');
    // ...and neither sibling steals the marker.
    expect(approvals).not.toContain('href="/" aria-current="page"');
    expect(approvals).not.toContain(
      'class="sl-nav-link" aria-current="page">Examples',
    );
  });

  it("disables the approvals link with the rest of the nav", () => {
    const html = renderToStaticMarkup(
      <AppShell
        activeView="workspace"
        onNavigate={() => undefined}
        navigationDisabled
      >
        <p>Workspace</p>
      </AppShell>,
    );
    expect(html).toContain('href="/approvals" aria-disabled="true"');
  });

  it("renders the semantic Run All columns and session-only label", () => {
    const html = renderToStaticMarkup(
      <RunReport
        runs={[summary]}
        onInspect={() => undefined}
        onRunAll={() => undefined}
      />,
    );
    expect(html).toContain("Invalidated tasks");
    expect(html).toContain("Plan status");
    expect(html).toContain("Replacement grant");
    expect(html).toContain("Session-only history");
    expect(html).toContain("Invalidation recall");
    expect(html).toContain("Preservation recall");
    expect(html).not.toContain("Stopped E");
    expect(html).not.toContain("Invalidation accuracy");
  });

  it("does not call an unattempted grant verification allowed", () => {
    const html = renderToStaticMarkup(
      <RunReport
        runs={[
          {
            ...summary,
            status: "failed",
            oldGrantRejected: false,
            reauthorizationSucceeded: false,
            oldGrantVerificationCode: null,
            replacementAuthorizationVerdict: null,
            replacementGrantVerificationCode: null,
          },
        ]}
        onInspect={() => undefined}
        onRunAll={() => undefined}
      />,
    );
    expect(html).toContain("Not reached");
    expect(html).not.toContain(">Allowed<");
  });

  it("renders five plain-language narrative steps", () => {
    const html = renderToStaticMarkup(
      <ScenarioNarrativeRail
        activeStep="impact"
        runStatus="running"
      />,
    );
    expect(html.match(/class="sl-narrative-step /g)).toHaveLength(5);
    expect(html).toContain(">Before<");
    expect(html).toContain(">Decision approved<");
    expect(html).toContain(">Impact found<");
    expect(html).toContain(">Work stopped<");
    expect(html).toContain(">Corrected<");
    expect(html).toContain('aria-current="step"');
  });

  it("shows only the active impact narrative with human-readable service updates", () => {
    const html = renderToStaticMarkup(
      <ScenarioRunView
        scenario={scenario}
        run={{ ...run, status: "running", activeStage: "decision-changed" }}
        narrativeStep="impact"
        onBack={() => undefined}
        onReset={() => undefined}
        onOpenEvidence={() => undefined}
        onDetailLayerChange={() => undefined}
        primaryAction={{
          label: "Check old authorization",
          onClick: () => undefined,
        }}
      />,
    );
    expect(html).toContain("Step 3 of 5");
    expect(html).toContain("writ.ai found the affected work");
    expect(html).toContain("How writ.ai found the work");
    expect(html).toContain("Affected work discovered");
    expect(html).toContain("Check old authorization");
    expect(html).toContain("Open technical evidence");
    expect(html).not.toContain("Corrected work may continue");
    expect(html).not.toContain("executor.rejected");
    expect(html).not.toContain("Next demo step");
    expect(html).not.toContain("Run remaining steps");
  });

  it("renders a state-aware impact map from backend-returned graph data", () => {
    const html = renderToStaticMarkup(
      <KnowledgeGraphView
        scenario={scenario}
        run={run}
        activeStep="impact"
        onOpenTechnicalEvidence={() => undefined}
        primaryAction={{
          label: "Check old authorization",
          onClick: () => undefined,
        }}
      />,
    );
    expect(html).toContain("Decision knowledge graph");
    expect(html).toContain("graph-v17 → graph-v18");
    expect(html).toContain("View technical proof");
    expect(html).toContain("Check old authorization");
    expect(html).toContain("sl-knowledge-node--stopped");
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain(
      "Examples use isolated in-memory graphs",
    );
    expect(html).toContain("supports Neo4j for persistent deployments");
    expect(html).not.toContain("Neo4j connected");
  });

  it("keeps impact hidden until the audience reveals it", () => {
    const html = renderToStaticMarkup(
      <KnowledgeGraphView
        scenario={scenario}
        run={{ ...run, status: "running", activeStage: "decision-changed" }}
        activeStep="decision"
        onOpenTechnicalEvidence={() => undefined}
      />,
    );
    expect(html).toContain("Approved change");
    expect(html).not.toContain("sl-knowledge-node--stopped");
    expect(html).not.toContain("sl-knowledge-node--needs-review");
  });

  it("shows the completed before-and-after result without repeating evidence controls", () => {
    const html = renderToStaticMarkup(
      <ScenarioRunView
        scenario={scenario}
        run={run}
        narrativeStep="corrected"
        onBack={() => undefined}
        onReset={() => undefined}
        onOpenEvidence={() => undefined}
        onDetailLayerChange={() => undefined}
        primaryAction={{
          label: "Start over",
          onClick: () => undefined,
        }}
      />,
    );
    expect(html).toContain("Step 5 of 5");
    expect(html).toContain("Corrected work may continue");
    expect(html).toContain("Fresh authorization accepted");
    expect(html).toContain("Start over");
    expect(html).toContain("Original plan authorized");
    expect(html).toContain("Corrected work may continue");
    expect(html).not.toContain("View complete graph");
    expect(html).not.toContain("Show 7-event timeline");
    expect(html).not.toContain("View Evidence");
  });

  it("offers Guided story and Impact map as the two primary scenario views", () => {
    const html = renderToStaticMarkup(
      <ScenarioRunView
        scenario={scenario}
        run={run}
        narrativeStep="impact"
        detailLayer="graph"
        onBack={() => undefined}
        onReset={() => undefined}
        onOpenEvidence={() => undefined}
        onDetailLayerChange={() => undefined}
        primaryAction={{
          label: "Check old authorization",
          onClick: () => undefined,
        }}
      />,
    );
    expect(html).toContain(">Guided story<");
    expect(html).toContain(">Impact map<");
    expect(html).toContain('aria-current="page">Impact map');
    expect(html).toContain('id="scenario-graph-panel"');
    expect(html).not.toContain(">Evidence</button>");
  });

  it("keeps technical evidence on one deliberate secondary surface", () => {
    const html = renderToStaticMarkup(
      <ScenarioRunView
        scenario={scenario}
        run={run}
        detailLayer="evidence"
        evidenceSection="graph"
        onBack={() => undefined}
        onReset={() => undefined}
        onOpenEvidence={() => undefined}
        onDetailLayerChange={() => undefined}
      />,
    );
    expect(html).toContain("Back to guided story");
    expect(html).toContain('id="technical-evidence-title"');
    expect(html).toContain('id="scenario-evidence-graph"');
    expect(html).toContain('id="scenario-evidence-timeline"');
    expect(html).toContain(
      'id="scenario-evidence-graph" class="sl-disclosure" open=""',
    );
  });

  it("returns technical proof to the Impact map when it was opened there", () => {
    const html = renderToStaticMarkup(
      <ScenarioRunView
        scenario={scenario}
        run={run}
        detailLayer="evidence"
        evidenceReturnLayer="graph"
        onBack={() => undefined}
        onReset={() => undefined}
        onOpenEvidence={() => undefined}
        onDetailLayerChange={() => undefined}
      />,
    );
    expect(html).toContain("Back to impact map");
    expect(html).not.toContain("Back to guided story");
  });
});
