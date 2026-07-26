import { describe, expect, it } from "vitest";
import type { LiveWorkspaceView } from "./model";
import {
  CALLWRIGHT_SAMPLE_WORKSPACE,
  correctedPlanDocument,
  createCallwrightWorkspaceRun,
  createStarterWorkspaceRun,
  createWorkspaceRun,
  SAMPLE_WORKSPACE,
} from "./sample";

function sourceRefs(document: ReturnType<typeof createStarterWorkspaceRun>) {
  return [
    document.baseline_decision.source_ref,
    document.specification.source_ref,
    document.ticket.source_ref,
    ...document.tasks.map((task) => task.source_ref),
  ];
}

describe("Live Workspace starter runs", () => {
  it("keeps the starter scenario intact while assigning a fresh workspace identity", () => {
    const run = createStarterWorkspaceRun("rehearsal-001");

    expect(run.id).toBe("refund-operations-run-rehearsal-001");
    expect(run.name).toBe(SAMPLE_WORKSPACE.name);
    expect(run.ticket.title).toBe(SAMPLE_WORKSPACE.ticket.title);
    expect(run.tasks.map((task) => task.title)).toEqual(
      SAMPLE_WORKSPACE.tasks.map((task) => task.title),
    );
    expect(run.plan).toEqual(SAMPLE_WORKSPACE.plan);
    expect(sourceRefs(run)).toHaveLength(6);
    expect(
      sourceRefs(run).every((sourceRef) =>
        String(sourceRef).startsWith(
          "workspace://refund-operations-run-rehearsal-001/",
        ),
      ),
    ).toBe(true);
    expect(JSON.stringify(run)).not.toContain(
      "workspace://refund-operations/",
    );
    expect(SAMPLE_WORKSPACE.id).toBe("refund-operations");
  });

  it("creates distinct valid IDs for separate starter rehearsals", () => {
    const first = createStarterWorkspaceRun("first run");
    const second = createStarterWorkspaceRun("second run");

    expect(first.id).toBe("refund-operations-run-first-run");
    expect(second.id).toBe("refund-operations-run-second-run");
    expect(first.id).not.toBe(second.id);
    expect(first.id).toMatch(/^[a-z0-9][a-z0-9-]{2,63}$/);
    expect(second.id).toMatch(/^[a-z0-9][a-z0-9-]{2,63}$/);
  });

  it("keeps generated starter IDs inside the workspace ID limit", () => {
    const run = createStarterWorkspaceRun("x".repeat(200));

    expect(run.id.length).toBeLessThanOrEqual(64);
    expect(run.id).toMatch(/^[a-z0-9][a-z0-9-]{2,63}$/);
  });

  it("turns an uploaded workspace into a distinct run without changing its work", () => {
    const internalPrefix = "workspace://customer-refund-workspace/";
    const uploaded = {
      ...(JSON.parse(
        JSON.stringify(SAMPLE_WORKSPACE).replaceAll(
          "workspace://refund-operations/",
          internalPrefix,
        ),
      ) as typeof SAMPLE_WORKSPACE),
      id: "customer-refund-workspace",
      edges: [
        {
          source_id: "DEC-001",
          target_id: "SPEC-001",
          kind: "BASIS_FOR",
          evidence_ref: `${internalPrefix}evidence/decision-to-spec`,
        },
        {
          source_id: "SPEC-001",
          target_id: "PAY-104",
          kind: "CREATES",
          evidence_ref: "manual://external/evidence",
        },
      ],
    };
    const original = structuredClone(uploaded);
    const run = createWorkspaceRun(uploaded, "upload-001");
    const serialized = JSON.stringify(run);

    expect(run.id).toBe("customer-refund-workspace-run-upload-001");
    expect(run.ticket.id).toBe(uploaded.ticket.id);
    expect(run.plan).toEqual(uploaded.plan);
    expect(serialized).not.toContain(internalPrefix);
    expect(serialized).toContain(
      "workspace://customer-refund-workspace-run-upload-001/",
    );
    expect(serialized).toContain("manual://external/evidence");
    expect(uploaded).toEqual(original);
  });

  it("truncates long uploaded IDs before adding the unique run suffix", () => {
    const run = createWorkspaceRun(
      { ...SAMPLE_WORKSPACE, id: "workspace-" + "x".repeat(54) },
      "upload-002",
    );

    expect(run.id.length).toBeLessThanOrEqual(64);
    expect(run.id).toMatch(/-run-upload-002$/);
    expect(run.id).toMatch(/^[a-z0-9][a-z0-9-]{2,63}$/);
  });

  it("does not repair malformed uploaded workspace IDs", () => {
    for (const id of ["REFUND", "$$$", "ab", "workspace_underscore"]) {
      expect(() =>
        createWorkspaceRun({ ...SAMPLE_WORKSPACE, id }, "upload-003"),
      ).toThrow("workspace ID must use 3–64 lowercase");
    }
  });

  it("ships a VOYAGR run with old evidence, a preserved sibling, and a hashed call action", () => {
    const run = createCallwrightWorkspaceRun("sponsor-demo");
    const callAction = CALLWRIGHT_SAMPLE_WORKSPACE.plan.actions.find(
      (action) =>
        "provider" in action.attributes &&
        action.attributes.provider === "voyagr-callwright",
    );
    const callAttributes = callAction?.attributes as
      | Record<string, unknown>
      | undefined;
    const suggestedChange =
      CALLWRIGHT_SAMPLE_WORKSPACE.baseline_decision.attributes
        .suggested_change;

    expect(run.id).toBe("voyagr-reservation-run-sponsor-demo");
    expect(CALLWRIGHT_SAMPLE_WORKSPACE.ticket.attributes.assigned_agent).toBe(
      "Reservation Calling Agent",
    );
    expect(CALLWRIGHT_SAMPLE_WORKSPACE.tasks.map((task) => task.id)).toEqual([
      "TASK-101",
      "TASK-102",
    ]);
    expect(CALLWRIGHT_SAMPLE_WORKSPACE.tasks[0]?.attributes).toMatchObject({
      agent_name: "Guest Summary Subagent",
      runtime_provider: "codex",
    });
    expect(CALLWRIGHT_SAMPLE_WORKSPACE.tasks[1]?.attributes).toMatchObject({
      agent_name: "Venue Calling Subagent",
      runtime_provider: "claude-code",
    });
    expect(
      run.tasks.map(
        (task) =>
          (task.attributes as Record<string, unknown> | undefined)
            ?.runtime_provider,
      ),
    ).toEqual(["codex", "claude-code"]);
    expect(callAttributes).toMatchObject({
      task_id: "TASK-102",
      provider: "voyagr-callwright",
      phone_number_ref: "demo-venue",
      requested_time: "2026-07-26T19:00:00-07:00",
      party_size: 4,
      max_deposit_usd: 0,
    });
    expect(suggestedChange.affected_scopes).toEqual(["reservation.time"]);
    expect(suggestedChange.text).not.toContain(
      CALLWRIGHT_SAMPLE_WORKSPACE.ticket.id,
    );
    expect(JSON.stringify(run)).not.toContain(
      "workspace://voyagr-reservation/",
    );
    expect(CALLWRIGHT_SAMPLE_WORKSPACE.id).toBe("voyagr-reservation");
  });

  it("keeps the Callwright structure while correcting only the approved time", () => {
    const callAction = CALLWRIGHT_SAMPLE_WORKSPACE.plan.actions[1];
    const callAttributes = callAction.attributes as Record<string, unknown>;
    const workspace = {
      baselineDecision: {
        id: "DEC-VOYAGR-001",
        scopes: ["event.copy", "reservation.time"],
      },
      ticket: {
        id: "EVENT-208",
        title: "Coordinate the launch dinner",
      },
      currentPlan: {
        id: "PLAN-VOYAGR-017",
        ticketId: "EVENT-208",
        objective: "Prepare details and request the reservation",
        actions: CALLWRIGHT_SAMPLE_WORKSPACE.plan.actions.map((action) => ({
          id: action.id,
          description: action.description as string,
          scopes: action.scopes as string[],
          attributes: action.attributes as Record<string, unknown>,
        })),
      },
      invalidationReport: {
        affected_scopes: ["reservation.time"],
        invalidated_task_ids: ["TASK-102"],
      },
      conflictAuthorization: {
        currentRequirements: {
          "reservation.time": {
            requested_time: "2026-07-26T20:30:00-07:00",
          },
        },
      },
      pendingMutation: null,
      latestApprovedMutation: null,
    } as unknown as LiveWorkspaceView;

    const corrected = correctedPlanDocument(workspace) as {
      objective: string;
      actions: Array<{
        description: string;
        scopes: string[];
        attributes: Record<string, unknown>;
      }>;
    };
    const correctedCall = corrected.actions.find((action) =>
      action.scopes.includes("reservation.time"),
    );

    expect(callAttributes.requested_time).toBe(
      "2026-07-26T19:00:00-07:00",
    );
    expect(callAction.description).toContain("7:00 PM");
    expect(corrected.objective).toContain("newly approved time");
    expect(correctedCall?.description).toContain("8:30 PM");
    expect(correctedCall?.description).not.toContain("7:00 PM");
    expect(correctedCall?.attributes).toMatchObject({
      provider: "voyagr-callwright",
      phone_number_ref: "demo-venue",
      requested_time: "2026-07-26T20:30:00-07:00",
      party_size: 4,
      max_deposit_usd: 0,
    });
  });
});
