import { describe, expect, it } from "vitest";
import {
  appliedPartition,
  formatRequirement,
  initialsOf,
  pendingDecisionId,
  previewMatches,
  toPendingChange,
  unwrapPreview,
  type ServerPreview,
  type ServerWorkspace,
} from "./live";

/**
 * Payloads shaped from Lane B's Pydantic models as of `lane-a`:
 * `LiveWorkspaceView` (workspaces/models.py:670), `WorkspaceApprovalPreview`
 * (:486), `PendingApproval` (intake/approval.py:45), `SupervisorAssignment`
 * (workspaces/supervisor.py:53). The HTTP round trip is NOT exercised — see
 * ASSUMPTIONS.md A6.
 */
const WORKSPACE: ServerWorkspace = {
  id: "csv-exports",
  name: "CSV exports",
  baseline_decision: {
    id: "DEC-004",
    kind: "Decision",
    title: "Exports are available to all users",
    attributes: {
      requirements: { "export.authorization": { audience: "all_users" } },
    },
  },
  specification: {
    id: "SPEC-009",
    kind: "Specification",
    title: "Export specification",
  },
  ticket: { id: "TICKET-100", kind: "Ticket", title: "Implementation ticket" },
  tasks: [
    { id: "TASK-101", kind: "Task", title: "TASK-101 · generate CSV files" },
    {
      id: "TASK-102",
      kind: "Task",
      title: "TASK-102 · expose export to all users",
    },
  ],
  current_plan: { id: "PLAN-027", kind: "AgentPlan", title: "Active plan" },
  pending_mutation: {
    decision: { id: "DEC-018", kind: "Decision", title: "Admin-only exports" },
    supersedes_id: "DEC-004",
    affected_scopes: ["export.authorization"],
  },
  approved_mutations: [],
  supervisor: {
    assignments: [
      { id: "ASSIGNMENT-1", task_id: "TASK-102", agent_name: "Priya Raman" },
      { id: "ASSIGNMENT-2", task_id: "TASK-102", agent_name: "Marcus Obi" },
      { id: "ASSIGNMENT-3", task_id: "TASK-102", agent_name: "Dan Levy" },
      { id: "ASSIGNMENT-4", task_id: "TASK-101", agent_name: "Ana Silva" },
      { id: "ASSIGNMENT-5", task_id: "TASK-101", agent_name: "Jonas Tan" },
    ],
  },
};

const PATH = ["DEC-018", "DEC-004", "SPEC-009", "TICKET-100", "TASK-102"];

const PREVIEW: ServerPreview = {
  pending: {
    workspace_id: "csv-exports",
    decision_id: "DEC-018",
    supersedes_id: "DEC-004",
    affected_scopes: ["export.authorization"],
    permission_id: "approve_compliance",
    source_ref: "slack://compliance/decision-018",
    title: "Dana Kaur",
    text: "Approved — exports must be admin-only, effective immediately.",
    effective_at: "2026-07-24T14:41:00Z",
    requirements: { "export.authorization": { audience: "admin_only" } },
    proposal_fingerprint: `sha256:${"a".repeat(64)}`,
    proposal_instance_id: "proposal-1",
  },
  interrupted_assignment_ids: ["ASSIGNMENT-1", "ASSIGNMENT-2", "ASSIGNMENT-3"],
  preserved_assignment_ids: ["ASSIGNMENT-4", "ASSIGNMENT-5"],
  assignment_provenance_paths: {
    "ASSIGNMENT-1": PATH,
    "ASSIGNMENT-2": PATH,
    "ASSIGNMENT-3": PATH,
  },
};

describe("toPendingChange", () => {
  const change = toPendingChange(WORKSPACE, PREVIEW);
  if (!change) {
    throw new Error("expected a change");
  }

  it("keys the change by workspace and decision", () => {
    expect(change.id).toBe("csv-exports:DEC-018");
    expect(change.decision.id).toBe("DEC-018");
    expect(change.decision.supersedes).toBe("DEC-004");
    expect(change.approverPermission).toBe("approve_compliance");
  });

  it("renders the blast radius the server partitioned, and nothing else", () => {
    expect(change.blastRadius.interrupted.map((p) => p.name)).toEqual([
      "Priya Raman",
      "Marcus Obi",
      "Dan Levy",
    ]);
    expect(change.blastRadius.preserved.map((p) => p.name)).toEqual([
      "Ana Silva",
      "Jonas Tan",
    ]);
    expect(change.blastRadius.interrupted[0].initials).toBe("PR");
    expect(change.blastRadius.interrupted[0].taskId).toBe("TASK-102");
  });

  it("joins the requirement delta from the two server-owned values", () => {
    expect(change.decision.scope).toBe("export.authorization");
    expect(change.decision.was).toBe("audience=all_users");
    expect(change.decision.now).toBe("audience=admin_only");
  });

  it("says so rather than guessing when no prior requirement covers the scope", () => {
    const uncovered = toPendingChange(
      { ...WORKSPACE, baseline_decision: undefined },
      PREVIEW,
    );
    expect(uncovered?.decision.was).toBe("not previously constrained");
  });

  it("renders one rail and keeps the surviving branch on it", () => {
    const ids = change.provenancePath.map((node) => node.id);
    expect(ids).toEqual([...PATH, "TASK-101"]);
    expect(change.provenancePath.at(-1)?.affected).toBe(false);
    expect(change.provenancePath.at(-1)?.title).toBe(
      "TASK-101 · generate CSV files",
    );
    expect(change.provenancePath.slice(0, -1).every((n) => n.affected)).toBe(true);
  });

  it("counts affected sessions per task node from the server partition", () => {
    const task = change.provenancePath.find((node) => node.id === "TASK-102");
    expect(task?.detail).toBe("3 sessions affected");
  });

  it("titles nodes from the workspace artifacts, falling back to the id", () => {
    const spec = change.provenancePath.find((node) => node.id === "SPEC-009");
    expect(spec?.title).toBe("Export specification");
    const orphan = toPendingChange(
      { ...WORKSPACE, specification: undefined },
      PREVIEW,
    );
    expect(
      orphan?.provenancePath.find((node) => node.id === "SPEC-009")?.title,
    ).toBe("SPEC-009");
  });

  it("never invents an author, and reads the channel off the source ref", () => {
    expect(change.source.channel).toBe("compliance");
    expect(change.source.text).toBe(
      "Approved — exports must be admin-only, effective immediately.",
    );
    const anonymous = toPendingChange(
      WORKSPACE,
      {
        ...PREVIEW,
        pending: { ...PREVIEW.pending, title: "", source_ref: "notion://policy/9" },
      },
    );
    expect(anonymous?.source.author).toBe("notion://policy/9");
    expect(anonymous?.source.channel).toBe("notion://policy/9");
  });

  it("never presents the decision title as the author", () => {
    // `PendingApproval.title` is the decision's own title, not a person.
    expect(change.source.author).not.toBe("Dana Kaur");
    expect(change.source.author).toBe("slack://compliance/decision-018");
  });

  it("picks the rail deterministically when paths differ in length", () => {
    const longer = [...PATH, "PLAN-027"];
    const mixed = toPendingChange(WORKSPACE, {
      ...PREVIEW,
      assignment_provenance_paths: {
        "ASSIGNMENT-1": PATH,
        "ASSIGNMENT-2": longer,
        "ASSIGNMENT-3": PATH,
      },
    });
    expect(mixed?.provenancePath.map((n) => n.id)).toEqual([
      ...longer,
      "TASK-101",
    ]);
  });

  it("returns null when the preview carries no affected scope", () => {
    expect(
      toPendingChange(WORKSPACE, {
        ...PREVIEW,
        pending: { ...PREVIEW.pending, affected_scopes: [] },
      }),
    ).toBeNull();
  });

  it("survives a preview whose assignment ids are not in the supervisor list", () => {
    const stale = toPendingChange(WORKSPACE, {
      ...PREVIEW,
      interrupted_assignment_ids: ["ASSIGNMENT-GONE"],
      assignment_provenance_paths: { "ASSIGNMENT-GONE": PATH },
    });
    expect(stale?.blastRadius.interrupted[0].name).toBe("ASSIGNMENT-GONE");
    expect(stale?.blastRadius.interrupted[0].taskId).toBe("");
  });
});

describe("requirement precedence", () => {
  it("lets the latest effective approved mutation beat an earlier one", () => {
    const withHistory: ServerWorkspace = {
      ...WORKSPACE,
      approved_mutations: [
        {
          mutation: {
            decision: {
              id: "DEC-010",
              effective_at: "2026-01-01T00:00:00Z",
              attributes: {
                requirements: { "export.authorization": { audience: "staff" } },
              },
            },
          },
        },
        {
          mutation: {
            decision: {
              id: "DEC-012",
              effective_at: "2026-06-01T00:00:00Z",
              attributes: {
                requirements: { "export.authorization": { audience: "managers" } },
              },
            },
          },
        },
      ],
    };
    // Arrival order would pick DEC-010; effective_at order picks DEC-012.
    expect(toPendingChange(withHistory, PREVIEW)?.decision.was).toBe(
      "audience=managers",
    );
  });

  it("falls back to the baseline only when no mutation covers the scope", () => {
    const unrelated: ServerWorkspace = {
      ...WORKSPACE,
      approved_mutations: [
        {
          mutation: {
            decision: {
              id: "DEC-011",
              effective_at: "2026-06-01T00:00:00Z",
              attributes: {
                requirements: { "export.generation": { format: "csv" } },
              },
            },
          },
        },
      ],
    };
    expect(toPendingChange(unrelated, PREVIEW)?.decision.was).toBe(
      "audience=all_users",
    );
  });
});

describe("multi-scope changes", () => {
  it("says a change touches more scopes rather than showing only one", () => {
    const multi = toPendingChange(WORKSPACE, {
      ...PREVIEW,
      pending: {
        ...PREVIEW.pending,
        affected_scopes: ["export.authorization", "export.retention"],
        requirements: {
          "export.authorization": { audience: "admin_only" },
          "export.retention": { days: 30 },
        },
      },
    });
    expect(multi?.decision.scope).toBe("export.authorization (+1 more scope)");
  });
});

describe("unwrapPreview", () => {
  it("accepts the flat correlated payload and the nested one", () => {
    expect(unwrapPreview({ ...PREVIEW, correlation_id: "x" })).not.toBeNull();
    expect(unwrapPreview({ preview: PREVIEW, correlation_id: "x" })).not.toBeNull();
  });

  it("rejects a body that is not a preview", () => {
    expect(unwrapPreview({ pending: { workspace_id: 1 } })).toBeNull();
    expect(unwrapPreview({})).toBeNull();
    expect(unwrapPreview(null)).toBeNull();
  });

  it("rejects partial payloads that would crash the render", () => {
    const drop = (key: string) => {
      const pending: Record<string, unknown> = { ...PREVIEW.pending };
      delete pending[key];
      return unwrapPreview({ ...PREVIEW, pending });
    };
    for (const key of [
      "source_ref",
      "text",
      "supersedes_id",
      "permission_id",
      // Without these an approval cannot confirm the proposal it was shown.
      "proposal_fingerprint",
      "proposal_instance_id",
    ]) {
      expect(drop(key), `missing ${key} must be rejected`).toBeNull();
    }
    expect(
      unwrapPreview({ ...PREVIEW, interrupted_assignment_ids: [1, 2] }),
    ).toBeNull();
    expect(
      unwrapPreview({ ...PREVIEW, assignment_provenance_paths: { A1: "nope" } }),
    ).toBeNull();
    expect(
      unwrapPreview({
        ...PREVIEW,
        pending: { ...PREVIEW.pending, requirements: "nope" },
      }),
    ).toBeNull();
  });
});

describe("appliedPartition", () => {
  const approved: ServerWorkspace = {
    ...WORKSPACE,
    supervisor: {
      assignments: WORKSPACE.supervisor?.assignments,
      applied_interrupts: [
        {
          decision_id: "DEC-018",
          interrupted_assignment_ids: ["ASSIGNMENT-1", "ASSIGNMENT-2"],
          preserved_assignment_ids: ["ASSIGNMENT-4"],
        },
      ],
    },
  };

  it("reads back the partition the server recorded for that decision", () => {
    const applied = appliedPartition(approved, "DEC-018");
    expect(applied?.interrupted.map((p) => p.name)).toEqual([
      "Priya Raman",
      "Marcus Obi",
    ]);
    expect(applied?.preserved.map((p) => p.name)).toEqual(["Ana Silva"]);
  });

  it("is null for a decision the supervisor never applied", () => {
    expect(appliedPartition(approved, "DEC-999")).toBeNull();
    expect(appliedPartition(WORKSPACE, "DEC-018")).toBeNull();
  });
});

describe("previewMatches", () => {
  it("accepts a preview describing the change that was requested", () => {
    expect(previewMatches(PREVIEW, "csv-exports", "DEC-018")).toBe(true);
  });

  it("rejects a preview for another workspace or another decision", () => {
    expect(previewMatches(PREVIEW, "other-workspace", "DEC-018")).toBe(false);
    expect(previewMatches(PREVIEW, "csv-exports", "DEC-999")).toBe(false);
  });
});

describe("pendingDecisionId", () => {
  it("finds the proposal awaiting approval", () => {
    expect(pendingDecisionId(WORKSPACE)).toBe("DEC-018");
  });

  it("is null for a workspace with nothing pending", () => {
    expect(pendingDecisionId({ ...WORKSPACE, pending_mutation: null })).toBeNull();
  });
});

describe("formatting helpers", () => {
  it("renders requirement objects deterministically", () => {
    expect(formatRequirement({ b: "2", a: "1" })).toBe("a=1, b=2");
    expect(formatRequirement("admins only")).toBe("admins only");
    expect(formatRequirement(undefined)).toBe("not previously constrained");
  });

  it("builds initials without inventing them", () => {
    expect(initialsOf("Priya Raman")).toBe("PR");
    expect(initialsOf("compliance")).toBe("CO");
    expect(initialsOf("")).toBe("??");
  });
});
