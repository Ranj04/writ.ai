import {
  LiveWorkspaceApiError,
  type LiveWorkspaceView,
  type WorkspaceImportDocument,
} from "./model";

const WORKSPACE_ID_PATTERN = /^[a-z0-9][a-z0-9-]{2,63}$/;

export const SAMPLE_WORKSPACE: WorkspaceImportDocument = {
  id: "refund-operations",
  name: "Refund operations",
  description: "A real workspace imported from dragback.yaml",
  graph_version: 17,
  authority_policy: {
    "refund.calculation": ["finance-admin"],
    "refund.identity": ["finance-admin"],
    "refund.execution": ["finance-admin"],
  },
  baseline_decision: {
    id: "DEC-001",
    kind: "Decision",
    title: "Refunds may be issued automatically",
    text: "Verified customer refunds may be calculated and issued automatically.",
    scopes: [
      "refund.calculation",
      "refund.identity",
      "refund.execution",
    ],
    approval_status: "proposal",
    authority_role: "finance-admin",
    confidence: 0.99,
    source_ref: "workspace://refund-operations/decisions/DEC-001",
    attributes: {
      requirements: {
        "refund.calculation": { method: "standard" },
        "refund.identity": { verification: "required" },
        "refund.execution": { mode: "automatic" },
      },
    },
  },
  specification: {
    id: "SPEC-001",
    kind: "Specification",
    title: "Refund processing controls",
    text: "Calculate refunds, confirm customer identity, and issue approved refunds.",
    scopes: [
      "refund.calculation",
      "refund.identity",
      "refund.execution",
    ],
    source_ref: "workspace://refund-operations/specifications/SPEC-001",
  },
  ticket: {
    id: "PAY-104",
    kind: "Ticket",
    title: "Automate customer refunds",
    text: "Implement the approved refund workflow.",
    scopes: [
      "refund.calculation",
      "refund.identity",
      "refund.execution",
    ],
    source_ref: "workspace://refund-operations/tickets/PAY-104",
    attributes: {
      assigned_agent: "Payments Coding Agent",
    },
  },
  tasks: [
    {
      id: "TASK-001",
      kind: "Task",
      title: "Calculate refund amount",
      text: "Calculate a refund using the standard method.",
      scopes: ["refund.calculation"],
      source_ref: "workspace://refund-operations/tasks/TASK-001",
    },
    {
      id: "TASK-002",
      kind: "Task",
      title: "Confirm customer identity",
      text: "Require customer identity verification.",
      scopes: ["refund.identity"],
      source_ref: "workspace://refund-operations/tasks/TASK-002",
    },
    {
      id: "TASK-003",
      kind: "Task",
      title: "Issue refund automatically",
      text: "Issue the approved refund without a manual handoff.",
      scopes: ["refund.execution"],
      source_ref: "workspace://refund-operations/tasks/TASK-003",
    },
  ],
  plan: {
    id: "PLAN-001",
    ticket_id: "PAY-104",
    objective: "Execute refund operations under the approved policy",
    actions: [
      {
        id: "ACTION-001",
        description: "Calculate refund amount",
        scopes: ["refund.calculation"],
        attributes: {
          task_id: "TASK-001",
          method: "standard",
        },
      },
      {
        id: "ACTION-002",
        description: "Confirm customer identity",
        scopes: ["refund.identity"],
        attributes: {
          task_id: "TASK-002",
          verification: "required",
        },
      },
      {
        id: "ACTION-003",
        description: "Issue refund automatically",
        scopes: ["refund.execution"],
        attributes: {
          task_id: "TASK-003",
          mode: "automatic",
        },
      },
    ],
  },
};

export const SAMPLE_WORKSPACE_JSON = JSON.stringify(SAMPLE_WORKSPACE, null, 2);

export const CALLWRIGHT_SAMPLE_WORKSPACE = {
  id: "voyagr-reservation",
  name: "VOYAGR reservation call",
  description:
    "A controlled Callwright demo: an approved schedule change stops a stale phone call and authorizes only the corrected call.",
  graph_version: 17,
  authority_policy: {
    "event.copy": ["event-ops-lead"],
    "reservation.time": ["event-ops-lead"],
  },
  baseline_decision: {
    id: "DEC-VOYAGR-001",
    kind: "Decision",
    title: "Launch dinner plan",
    text: "Prepare a concise guest summary and request the venue for 7:00 PM.",
    scopes: ["event.copy", "reservation.time"],
    approval_status: "proposal",
    authority_role: "event-ops-lead",
    confidence: 0.99,
    source_ref:
      "workspace://voyagr-reservation/evidence/slack/launch-dinner-plan",
    attributes: {
      requirements: {
        "event.copy": { tone: "concise" },
        "reservation.time": {
          requested_time: "2026-07-26T19:00:00-07:00",
        },
      },
      suggested_change: {
        id: "DEC-VOYAGR-002",
        title: "Launch dinner reservations move to 8:30 PM",
        text: "All venue reservations for the launch dinner must now be requested for 8:30 PM.",
        affected_scopes: ["reservation.time"],
        source_ref:
          "workspace://voyagr-reservation/evidence/slack/schedule-change",
        requirements: {
          "reservation.time": {
            requested_time: "2026-07-26T20:30:00-07:00",
          },
        },
      },
    },
  },
  specification: {
    id: "SPEC-VOYAGR-001",
    kind: "Specification",
    title: "Launch dinner coordination",
    text: "Prepare the guest summary and coordinate the venue reservation.",
    scopes: ["event.copy", "reservation.time"],
    source_ref:
      "workspace://voyagr-reservation/specifications/launch-dinner",
  },
  ticket: {
    id: "EVENT-208",
    kind: "Ticket",
    title: "Coordinate the launch dinner",
    text: "Prepare the event summary and arrange the approved venue reservation.",
    scopes: ["event.copy", "reservation.time"],
    source_ref: "workspace://voyagr-reservation/tickets/EVENT-208",
    attributes: {
      assigned_agent: "Reservation Calling Agent",
    },
  },
  tasks: [
    {
      id: "TASK-101",
      kind: "Task",
      title: "Prepare guest summary",
      text: "Prepare a concise guest summary for the venue.",
      scopes: ["event.copy"],
      source_ref: "workspace://voyagr-reservation/tasks/TASK-101",
      attributes: {
        agent_name: "Guest Summary Subagent",
        runtime_provider: "codex",
      },
    },
    {
      id: "TASK-102",
      kind: "Task",
      title: "Call venue for the approved time",
      text: "Use Callwright to request the approved reservation time.",
      scopes: ["reservation.time"],
      source_ref: "workspace://voyagr-reservation/tasks/TASK-102",
      attributes: {
        agent_name: "Venue Calling Subagent",
        runtime_provider: "claude-code",
      },
    },
  ],
  plan: {
    id: "PLAN-VOYAGR-017",
    ticket_id: "EVENT-208",
    objective: "Prepare the launch dinner details and request the reservation",
    actions: [
      {
        id: "ACTION-SUMMARY-001",
        description: "Prepare the concise guest summary",
        scopes: ["event.copy"],
        attributes: {
          task_id: "TASK-101",
          tone: "concise",
        },
      },
      {
        id: "ACTION-CALL-001",
        description:
          "Call the venue for 7:00 PM, the approved reservation time",
        scopes: ["reservation.time"],
        attributes: {
          task_id: "TASK-102",
          provider: "voyagr-callwright",
          phone_number_ref: "demo-venue",
          objective:
            "Request a reservation for four guests without making a paid commitment.",
          requested_time: "2026-07-26T19:00:00-07:00",
          party_size: 4,
          max_deposit_usd: 0,
          instructions: [
            "Ask whether the approved time is available.",
            "Politely end the call after receiving the answer.",
          ],
          allowed_commitments: [
            "Request the reservation only when no deposit is required.",
          ],
          language: "en",
        },
      },
    ],
  },
} satisfies WorkspaceImportDocument;

export const CALLWRIGHT_SAMPLE_WORKSPACE_JSON = JSON.stringify(
  CALLWRIGHT_SAMPLE_WORKSPACE,
  null,
  2,
);

function starterRunToken(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function normalizedRunToken(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9-]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40)
      .replace(/-+$/g, "") || "demo"
  );
}

function declaredWorkspaceId(document: WorkspaceImportDocument): string {
  if (
    typeof document.id !== "string" ||
    !WORKSPACE_ID_PATTERN.test(document.id)
  ) {
    throw new LiveWorkspaceApiError(
      "The workspace ID must use 3–64 lowercase letters, numbers, or hyphens.",
      "INVALID_WORKSPACE_ID",
    );
  }
  return document.id;
}

export function createWorkspaceRun(
  document: WorkspaceImportDocument,
  runToken = starterRunToken(),
): WorkspaceImportDocument {
  const suffix = `-run-${normalizedRunToken(runToken)}`;
  const previousId = declaredWorkspaceId(document);
  const workspaceBase = previousId
    .slice(0, 64 - suffix.length)
    .replace(/-+$/g, "");
  const workspaceId = `${workspaceBase}${suffix}`;
  const sourcePrefix = `workspace://${previousId}/`;
  const nextSourcePrefix = `workspace://${workspaceId}/`;
  const serialized = JSON.stringify(document);
  const cloned = JSON.parse(
    serialized.replaceAll(sourcePrefix, nextSourcePrefix),
  ) as WorkspaceImportDocument;
  return { ...cloned, id: workspaceId };
}

export function createStarterWorkspaceRun(
  runToken = starterRunToken(),
): WorkspaceImportDocument {
  return createWorkspaceRun(SAMPLE_WORKSPACE, runToken);
}

export function createCallwrightWorkspaceRun(
  runToken = starterRunToken(),
): WorkspaceImportDocument {
  return createWorkspaceRun(CALLWRIGHT_SAMPLE_WORKSPACE, runToken);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

interface SuggestedWorkspaceChange {
  id?: string;
  title: string;
  text: string;
  affectedScopes: string[];
  requirements: Record<string, Record<string, unknown>>;
  sourceRef?: string;
}

function suggestedWorkspaceChange(
  workspace: LiveWorkspaceView,
): SuggestedWorkspaceChange | null {
  const raw = workspace.baselineDecision.attributes.suggested_change;
  if (!isRecord(raw)) return null;
  const rawAffectedScopes = raw.affected_scopes;
  const affectedScopes = Array.isArray(rawAffectedScopes)
    ? rawAffectedScopes.filter(
        (scope): scope is string =>
          typeof scope === "string" &&
          workspace.baselineDecision.scopes.includes(scope),
      )
    : [];
  const affectedScopeCount = Array.isArray(rawAffectedScopes)
    ? rawAffectedScopes.length
    : 0;
  const rawRequirements = raw.requirements;
  if (
    affectedScopes.length === 0 ||
    affectedScopes.length !== affectedScopeCount ||
    typeof raw.title !== "string" ||
    !raw.title.trim() ||
    typeof raw.text !== "string" ||
    !raw.text.trim() ||
    !isRecord(rawRequirements)
  ) {
    return null;
  }
  const requirements = Object.fromEntries(
    affectedScopes.map((scope) => [
      scope,
      isRecord(rawRequirements[scope])
        ? { ...rawRequirements[scope] }
        : null,
    ]),
  );
  if (Object.values(requirements).some((requirement) => requirement === null)) {
    return null;
  }
  if (
    Object.keys(rawRequirements).length !== affectedScopes.length ||
    Object.keys(rawRequirements).some(
      (scope) => !affectedScopes.includes(scope),
    )
  ) {
    return null;
  }
  return {
    id: typeof raw.id === "string" && raw.id.trim() ? raw.id : undefined,
    title: raw.title.trim(),
    text: raw.text.trim(),
    affectedScopes,
    requirements: requirements as Record<
      string,
      Record<string, unknown>
    >,
    sourceRef:
      typeof raw.source_ref === "string" && raw.source_ref.trim()
        ? raw.source_ref
        : undefined,
  };
}

export function initialChangeDocument(
  workspace: LiveWorkspaceView,
): Record<string, unknown> {
  const suggestedChange = suggestedWorkspaceChange(workspace);
  if (suggestedChange) {
    const primaryScope = suggestedChange.affectedScopes[0];
    return {
      decision: {
        id:
          suggestedChange.id ??
          `${workspace.baselineDecision.id}-CHANGE`,
        kind: "Decision",
        title: suggestedChange.title,
        text: suggestedChange.text,
        scopes: suggestedChange.affectedScopes,
        approval_status: "proposal",
        authority_role:
          workspace.baselineDecision.authorityRole ??
          workspace.authorityPolicy[primaryScope]?.[0] ??
          "admin",
        confidence: 0.99,
        source_ref:
          suggestedChange.sourceRef ??
          `workspace://${workspace.id}/decisions/change`,
        attributes: { requirements: suggestedChange.requirements },
      },
      supersedes_id: workspace.baselineDecision.id,
      affected_scopes: suggestedChange.affectedScopes,
    };
  }
  const sortedScopes = [...workspace.baselineDecision.scopes].sort();
  const scope =
    sortedScopes.find((candidate) => candidate === "refund.execution") ??
    sortedScopes.find((candidate) => candidate.endsWith(".execution")) ??
    sortedScopes[0] ??
    "workspace.change";
  const refundSample =
    scope.toLowerCase().includes("refund") && scope.endsWith("execution");
  const requirements = refundSample
    ? { [scope]: { mode: "human_approval_over_500" } }
    : { [scope]: { requires_review: true } };
  return {
    decision: {
      id: refundSample ? "DEC-002" : `${workspace.baselineDecision.id}-CHANGE`,
      kind: "Decision",
      title: refundSample
        ? "Refunds over $500 require human approval"
        : `Review required for ${scope}`,
      text: refundSample
        ? "Refunds over $500 must be escalated to a human approver."
        : `Work in ${scope} now requires explicit review.`,
      scopes: [scope],
      approval_status: "proposal",
      authority_role:
        workspace.baselineDecision.authorityRole ??
        workspace.authorityPolicy[scope]?.[0] ??
        "admin",
      confidence: 0.99,
      source_ref: `workspace://${workspace.id}/decisions/change`,
      attributes: { requirements },
    },
    supersedes_id: workspace.baselineDecision.id,
    affected_scopes: [scope],
  };
}

function isRefundStarterScenario(workspace: LiveWorkspaceView): boolean {
  return (
    workspace.baselineDecision.id === SAMPLE_WORKSPACE.baseline_decision.id &&
    workspace.ticket.id === SAMPLE_WORKSPACE.ticket.id &&
    workspace.baselineDecision.scopes.includes("refund.execution")
  );
}

function callwrightActionForScope(
  workspace: LiveWorkspaceView,
  scope: string,
) {
  return workspace.currentPlan.actions.find(
    (action) =>
      action.attributes.provider === "voyagr-callwright" &&
      action.scopes.includes(scope),
  );
}

function approvedReservationTime(
  requirement: Record<string, unknown> | undefined,
): string | null {
  const requestedTime = requirement?.requested_time;
  if (typeof requestedTime !== "string" || requestedTime.length === 0) {
    return null;
  }
  const clock = /T(\d{2}):(\d{2})/.exec(requestedTime);
  if (!clock) {
    return requestedTime;
  }
  const hour = Number(clock[1]);
  const minute = clock[2];
  const suffix = hour >= 12 ? "PM" : "AM";
  const displayHour = hour % 12 || 12;
  return `${displayHour}:${minute} ${suffix}`;
}

function correctedCallDescription(
  requirement: Record<string, unknown> | undefined,
): string {
  const approvedTime = approvedReservationTime(requirement);
  return approvedTime
    ? `Call the venue for ${approvedTime}, the newly approved reservation time`
    : "Call the venue using the newly approved reservation time";
}

export function correctedPlanDocument(
  workspace: LiveWorkspaceView,
): Record<string, unknown> {
  const invalidated = new Set(
    workspace.invalidationReport?.invalidated_task_ids ??
      workspace.invalidationReport?.stopped_work_artifact_ids ??
      [],
  );
  const affectedScopes = new Set(
    workspace.invalidationReport?.affected_scopes ??
      workspace.pendingMutation?.affectedScopes ??
      workspace.latestApprovedMutation?.affectedScopes ??
      [],
  );
  const retainedActions = workspace.currentPlan.actions
    .filter((action) => {
      const taskId = action.attributes.task_id;
      const referencesInvalidatedTask =
        typeof taskId === "string" && invalidated.has(taskId);
      const intersectsChangedScope = action.scopes.some((scope) =>
        affectedScopes.has(scope),
      );
      return !referencesInvalidatedTask && !intersectsChangedScope;
    })
    .map((action) => ({
      id: action.id,
      description: action.description,
      scopes: [...action.scopes],
      attributes: { ...action.attributes },
    }));
  const requirements =
    workspace.conflictAuthorization?.currentRequirements ??
    (workspace.pendingMutation?.decision.attributes.requirements as
      | Record<string, Record<string, unknown>>
      | undefined) ??
    {};
  const refundStarterScenario = isRefundStarterScenario(workspace);
  const readOnlyScope = [...affectedScopes].find((scope) => {
    const requirement = requirements[scope];
    return (
      requirement?.write_access === false ||
      requirement?.access === "read_only"
    );
  });
  const correctiveActions = [...affectedScopes].sort().map((scope, index) => ({
    id: `ACTION-CORRECTED-${index + 1}`,
    description:
      refundStarterScenario
        ? "Escalate qualifying refunds for human approval"
        : scope === readOnlyScope
          ? "Remove CRM create, update, and delete operations; keep synchronization read-only"
          : callwrightActionForScope(workspace, scope)
            ? correctedCallDescription(requirements[scope])
        : `Satisfy the approved requirement for ${scope}`,
    scopes: [scope],
    attributes: {
      ...(callwrightActionForScope(workspace, scope)?.attributes ?? {}),
      ...(requirements[scope] ?? {}),
    },
  }));
  const callwrightCorrection = correctiveActions.some(
    (action) => action.attributes.provider === "voyagr-callwright",
  );
  return {
    id: `${workspace.currentPlan.id}-REV1`,
    ticket_id: workspace.currentPlan.ticketId,
    objective:
      refundStarterScenario
        ? "Execute refund operations with the updated approval rule"
        : readOnlyScope
          ? `Continue ${workspace.ticket.title.replace(/^(?:Build|Implement)\s+/i, "")} with read-only CRM access`
          : callwrightCorrection
            ? "Prepare the event summary and place the venue call using the newly approved time"
        : `Correct ${workspace.currentPlan.objective} for the approved change`,
    actions: [...retainedActions, ...correctiveActions],
  };
}
