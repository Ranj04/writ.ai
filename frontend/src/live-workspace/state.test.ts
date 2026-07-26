import { describe, expect, it } from "vitest";
import { mapLiveWorkspace } from "./api";
import {
  correctedPlanDocument,
  initialChangeDocument,
  SAMPLE_WORKSPACE,
} from "./sample";
import {
  activeWorkspaceStage,
  editWorkspaceDocument,
  workspaceGuide,
  workspaceReadiness,
  workspaceStageProgress,
  workspaceVerificationReport,
} from "./state";
import { RAW_WORKSPACE } from "./test-fixtures";

describe("Live Workspace state helpers", () => {
  it("preserves YAML parsing mode while the imported document is edited", () => {
    expect(
      editWorkspaceDocument(
        { content: "id: first", format: "yaml" },
        "id: edited",
      ),
    ).toEqual({ content: "id: edited", format: "yaml" });
  });

  it("derives the five guided stages from backend-owned status", () => {
    expect(activeWorkspaceStage()).toBe("import");
    expect(activeWorkspaceStage("imported")).toBe("approve-baseline");
    expect(activeWorkspaceStage("baseline-approved")).toBe("authorize-plan");
    expect(activeWorkspaceStage("authorized")).toBe("apply-change");
    expect(activeWorkspaceStage("initial-grant-rejected")).toBe(
      "verify-update",
    );
    expect(workspaceStageProgress("import", "authorized")).toBe("complete");
    expect(workspaceStageProgress("apply-change", "authorized")).toBe(
      "current",
    );
    expect(
      workspaceStageProgress("verify-update", "initial-grant-rejected"),
    ).toBe("attention");
    expect(workspaceStageProgress("verify-update", "complete")).toBe(
      "complete",
    );
  });

  it("returns concise current-step guidance and explicit wait copy", () => {
    expect(workspaceGuide()).toMatchObject({
      step: 1,
      totalSteps: 5,
      title: "Add your workspace",
      stateLabel: "Waiting for a file",
    });
    expect(workspaceGuide("change-applied")).toMatchObject({
      step: 4,
      title: "Check the original authorization",
      busyMessage:
        "The independent executor is checking the original authorization…",
    });
    expect(workspaceGuide("complete")).toMatchObject({
      step: 5,
      title: "Workspace verified",
      tone: "complete",
    });
  });

  it("requires the decision, ticket/tasks, scoped plan, and authority policy", () => {
    expect(workspaceReadiness(SAMPLE_WORKSPACE)).toEqual({
      approvedDecision: true,
      ticketAndTasks: true,
      scopedPlan: true,
      authorityRoles: true,
      ready: true,
    });
    expect(
      workspaceReadiness({
        ...SAMPLE_WORKSPACE,
        authority_policy: {},
      }).ready,
    ).toBe(false);
  });

  it("replaces changed-scope actions even when they have no task_id", () => {
    const workspace = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      current_plan: {
        ...RAW_WORKSPACE.current_plan,
        actions: [
          {
            id: "ACTION-EXTERNAL",
            description: "External action without task metadata",
            scopes: ["refund.execution"],
            attributes: { mode: "automatic" },
          },
          {
            id: "ACTION-SAFE",
            description: "Safe calculation",
            scopes: ["refund.calculation"],
            attributes: { method: "standard" },
          },
        ],
      },
    });
    const corrected = correctedPlanDocument(workspace) as {
      actions: Array<{
        id: string;
        scopes: string[];
        attributes: Record<string, unknown>;
      }>;
    };
    expect(corrected.actions.map((action) => action.id)).toEqual([
      "ACTION-SAFE",
      "ACTION-CORRECTED-1",
    ]);
    expect(corrected.actions[1]?.attributes).toEqual({
      mode: "human_approval_over_500",
    });
  });

  it("keeps the starter correction copy after assigning a fresh workspace ID", () => {
    const workspace = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      id: "refund-operations-run-rehearsal-001",
    });
    const corrected = correctedPlanDocument(workspace) as {
      objective: string;
      actions: Array<{ description: string }>;
    };

    expect(corrected.objective).toBe(
      "Execute refund operations with the updated approval rule",
    );
    expect(corrected.actions.at(-1)?.description).toBe(
      "Escalate qualifying refunds for human approval",
    );
  });

  it("chooses the refund execution scope independent of API set ordering", () => {
    const workspace = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      baseline_decision: {
        ...RAW_WORKSPACE.baseline_decision,
        scopes: [
          "refund.execution",
          "refund.identity",
          "refund.calculation",
        ],
      },
      authority_policy: {
        "refund.execution": ["finance-admin"],
        "refund.identity": ["finance-admin"],
        "refund.calculation": ["finance-admin"],
      },
    });
    const change = initialChangeDocument(workspace) as {
      affected_scopes: string[];
      decision: {
        attributes: {
          requirements: Record<string, Record<string, unknown>>;
        };
      };
    };

    expect(change.affected_scopes).toEqual(["refund.execution"]);
    expect(change.decision.attributes.requirements).toEqual({
      "refund.execution": { mode: "human_approval_over_500" },
    });
  });

  it("uses a reviewed Jira change as a proposal instead of inventing a generic review rule", () => {
    const workspace = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      id: "crm-208-build-customer-synchronization",
      baseline_decision: {
        ...RAW_WORKSPACE.baseline_decision,
        id: "CRM-208-DECISION-BASELINE",
        scopes: [
          "integration.authentication",
          "integration.read",
          "integration.write",
        ],
        authority_role: "engineering-admin",
        attributes: {
          suggested_change: {
            id: "CRM-208-DECISION-READ-ONLY",
            title: "CRM integration must be read-only",
            text: "The CRM integration must be read-only.",
            affected_scopes: ["integration.write"],
            requirements: {
              "integration.write": { write_access: false },
            },
            source_ref: "document://jira-ticket/change",
          },
        },
      },
      authority_policy: {
        "integration.authentication": ["engineering-admin"],
        "integration.read": ["engineering-admin"],
        "integration.write": ["engineering-admin"],
      },
    });
    const change = initialChangeDocument(workspace) as {
      affected_scopes: string[];
      decision: {
        id: string;
        title: string;
        text: string;
        approval_status: string;
        source_ref: string;
        attributes: {
          requirements: Record<string, Record<string, unknown>>;
        };
      };
    };

    expect(change).toMatchObject({
      affected_scopes: ["integration.write"],
      decision: {
        id: "CRM-208-DECISION-READ-ONLY",
        title: "CRM integration must be read-only",
        text: "The CRM integration must be read-only.",
        approval_status: "proposal",
        source_ref: "document://jira-ticket/change",
        attributes: {
          requirements: {
            "integration.write": { write_access: false },
          },
        },
      },
    });
  });

  it("removes all invalidated CRM writes and proposes a plain-language read-only correction", () => {
    const workspace = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      id: "crm-208-build-customer-synchronization",
      baseline_decision: {
        ...RAW_WORKSPACE.baseline_decision,
        id: "CRM-208-DECISION-BASELINE",
        scopes: [
          "integration.authentication",
          "integration.read",
          "integration.write",
        ],
      },
      ticket: {
        ...RAW_WORKSPACE.ticket,
        id: "CRM-208",
        title: "Build customer synchronization",
      },
      current_plan: {
        id: "CRM-208-PLAN",
        ticket_id: "CRM-208",
        objective: "Build customer synchronization",
        actions: [
          {
            id: "ACTION-AUTH",
            description: "Authenticate with the CRM",
            scopes: ["integration.authentication"],
            attributes: { task_id: "TASK-AUTH", authenticated: true },
          },
          {
            id: "ACTION-READ",
            description: "Read customer records",
            scopes: ["integration.read"],
            attributes: { task_id: "TASK-READ", read_access: true },
          },
          ...["CREATE", "UPDATE", "DELETE"].map((verb) => ({
            id: `ACTION-${verb}`,
            description: `${verb.toLowerCase()} customer records`,
            scopes: ["integration.write"],
            attributes: {
              task_id: `TASK-${verb}`,
              write_access: true,
            },
          })),
        ],
      },
      latest_approved_mutation: {
        decision: {
          ...RAW_WORKSPACE.latest_approved_mutation!.decision,
          id: "CRM-208-DECISION-READ-ONLY",
          attributes: {
            requirements: {
              "integration.write": { write_access: false },
            },
          },
        },
        supersedes_id: "CRM-208-DECISION-BASELINE",
        affected_scopes: ["integration.write"],
      },
      conflict_authorization: {
        ...RAW_WORKSPACE.conflict_authorization!,
        affected_scopes: ["integration.write"],
        current_requirements: {
          "integration.write": { write_access: false },
        },
      },
      invalidation_report: {
        ...RAW_WORKSPACE.invalidation_report!,
        affected_scopes: ["integration.write"],
        invalidated_task_ids: [
          "TASK-CREATE",
          "TASK-UPDATE",
          "TASK-DELETE",
        ],
        stopped_work_artifact_ids: [
          "TASK-CREATE",
          "TASK-UPDATE",
          "TASK-DELETE",
        ],
      },
    });
    const corrected = correctedPlanDocument(workspace) as {
      objective: string;
      actions: Array<{
        id: string;
        description: string;
        attributes: Record<string, unknown>;
      }>;
    };

    expect(corrected.objective).toBe(
      "Continue customer synchronization with read-only CRM access",
    );
    expect(corrected.actions.map((action) => action.id)).toEqual([
      "ACTION-AUTH",
      "ACTION-READ",
      "ACTION-CORRECTED-1",
    ]);
    expect(corrected.actions.at(-1)).toMatchObject({
      description:
        "Remove CRM create, update, and delete operations; keep synchronization read-only",
      attributes: { write_access: false },
    });
  });

  it("builds a useful verification report without raw authorization tokens", () => {
    const workspace = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      status: "complete",
      replacement_verification: {
        applied: true,
        reason: "Grant verified; simulated Callwright call submitted.",
        verification_code: "VALID",
        execution_mode: "simulated",
        call_receipt: {
          provider: "voyagr-callwright-fixture",
          call_id: "CALL-FIXTURE-018",
          status: "submitted",
          evidence_ref: "callwright://fixture/CALL-FIXTURE-018",
        },
      },
    });
    const report = workspaceVerificationReport(workspace);
    const serialized = JSON.stringify(report);
    expect(serialized).toContain("STALE_SNAPSHOT");
    expect(serialized).toContain("TASK-003");
    expect(serialized).toContain("DEC-002");
    expect(report).toMatchObject({
      outcome: {
        execution_receipt: {
          execution_mode: "simulated",
          provider: "voyagr-callwright-fixture",
          call_id: "CALL-FIXTURE-018",
          status: "submitted",
          evidence_ref: "callwright://fixture/CALL-FIXTURE-018",
        },
      },
    });
    expect(serialized).not.toContain("signed_token");
    expect(serialized).not.toContain("grant_token");
    expect(serialized).not.toContain("token");
  });
});
