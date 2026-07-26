import type {
  LiveWorkspaceView,
  LiveWorkspaceStage,
  LiveWorkspaceStatus,
  WorkspaceDocumentFormat,
  WorkspaceImportDocument,
} from "./model";

export interface WorkspaceDocumentEditorState {
  content: string;
  format: WorkspaceDocumentFormat;
}

export function editWorkspaceDocument(
  current: WorkspaceDocumentEditorState,
  content: string,
): WorkspaceDocumentEditorState {
  return { ...current, content };
}

export const WORKSPACE_STAGES: ReadonlyArray<{
  id: LiveWorkspaceStage;
  label: string;
}> = [
  {
    id: "import",
    label: "Add workspace",
  },
  {
    id: "approve-baseline",
    label: "Approve baseline",
  },
  {
    id: "authorize-plan",
    label: "Authorize plan",
  },
  {
    id: "apply-change",
    label: "Review change",
  },
  {
    id: "verify-update",
    label: "Resolve impact",
  },
];

const STATUS_STAGE: Record<LiveWorkspaceStatus, LiveWorkspaceStage> = {
  imported: "approve-baseline",
  "baseline-approved": "authorize-plan",
  authorized: "apply-change",
  "change-proposed": "apply-change",
  "change-applied": "apply-change",
  "initial-grant-rejected": "verify-update",
  "plan-updated": "verify-update",
  reauthorized: "verify-update",
  complete: "verify-update",
};

export type WorkspaceStageProgress =
  | "complete"
  | "current"
  | "upcoming"
  | "attention";

export function activeWorkspaceStage(
  status?: LiveWorkspaceStatus,
): LiveWorkspaceStage {
  return status ? STATUS_STAGE[status] : "import";
}

export function workspaceStageProgress(
  stage: LiveWorkspaceStage,
  status?: LiveWorkspaceStatus,
): WorkspaceStageProgress {
  if (status === "complete") return "complete";
  if (
    stage === "verify-update" &&
    (status === "initial-grant-rejected" || status === "plan-updated")
  ) {
    return "attention";
  }
  const active = activeWorkspaceStage(status);
  const stageIndex = WORKSPACE_STAGES.findIndex((item) => item.id === stage);
  const activeIndex = WORKSPACE_STAGES.findIndex((item) => item.id === active);
  if (stageIndex < activeIndex) return "complete";
  if (stageIndex === activeIndex) return "current";
  return "upcoming";
}

export interface WorkspaceGuide {
  step: number;
  totalSteps: number;
  title: string;
  instruction: string;
  next: string;
  busyMessage: string;
  stateLabel: string;
  tone: "neutral" | "attention" | "complete";
}

const GUIDE_BY_STATUS: Record<LiveWorkspaceStatus, Omit<WorkspaceGuide, "step" | "totalSteps">> = {
  imported: {
    title: "Approve the starting point",
    instruction:
      "Review what was imported and choose the role allowed to approve its governed scopes.",
    next:
      "Dragback will create the first approved decision version. The plan can then be checked against it.",
    busyMessage: "Verifying the approver and creating the baseline…",
    stateLabel: "Approval required",
    tone: "neutral",
  },
  "baseline-approved": {
    title: "Authorize the current plan",
    instruction:
      "Ask Dragback to compare every plan action with the approved baseline.",
    next:
      "If the plan matches, Dragback approves this exact plan for the current company decisions.",
    busyMessage: "Evaluating the plan and issuing its authorization…",
    stateLabel: "Ready to authorize",
    tone: "neutral",
  },
  authorized: {
    title: "Add a decision change",
    instruction:
      "Submit the new decision as a proposal. A proposal alone cannot change the graph or stop work.",
    next:
      "An authoritative role must approve the proposal before Dragback updates the graph.",
    busyMessage: "Recording the proposal without changing the approved graph…",
    stateLabel: "Plan authorized",
    tone: "neutral",
  },
  "change-proposed": {
    title: "Approve the decision change",
    instruction:
      "Review the proposal, then approve it with an authoritative role—or cancel it to keep editing.",
    next:
      "Approval creates a new decision version and identifies only the work touched by the change.",
    busyMessage: "Checking authority and applying the approved decision…",
    stateLabel: "Proposal awaiting approval",
    tone: "neutral",
  },
  "change-applied": {
    title: "Check the original authorization",
    instruction:
      "Ask the independent executor whether the earlier approval is still safe to use.",
    next:
      "If it is stale, conflicting work stops while work outside the changed scope stays valid.",
    busyMessage: "The independent executor is checking the original authorization…",
    stateLabel: "New decision version created",
    tone: "neutral",
  },
  "initial-grant-rejected": {
    title: "Update the affected plan",
    instruction:
      "Review the stopped task, update the plan, and save the correction.",
    next:
      "Dragback saves the plan, checks it against the new decision, then requests a new authorization.",
    busyMessage: "Saving the corrected plan and requesting a new authorization…",
    stateLabel: "Old authorization rejected",
    tone: "attention",
  },
  "plan-updated": {
    title: "Authorize the saved plan",
    instruction:
      "The correction is saved. Request an authorization for that exact plan.",
    next:
      "The authority will allow the saved plan or explain any remaining mismatch.",
    busyMessage: "Requesting a new authorization for the saved plan…",
    stateLabel: "Plan saved",
    tone: "attention",
  },
  reauthorized: {
    title: "Verify the new authorization",
    instruction:
      "Ask the independent executor to verify the new authorization before work resumes.",
    next:
      "A valid result completes the workflow and makes a downloadable verification report available.",
    busyMessage: "The independent executor is checking the new authorization…",
    stateLabel: "New authorization issued",
    tone: "neutral",
  },
  complete: {
    title: "Workspace verified",
    instruction:
      "The executor accepted the new authorization. Conflicting work was corrected and unaffected work stayed valid.",
    next:
      "Download the verification report or open technical evidence when you need an audit record.",
    busyMessage: "Workspace verified.",
    stateLabel: "Complete",
    tone: "complete",
  },
};

const IMPORT_GUIDE: WorkspaceGuide = {
  step: 1,
  totalSteps: WORKSPACE_STAGES.length,
  title: "Add your workspace",
  instruction:
    "Upload a project document, screenshot, or structured workspace file, or continue with the starter example.",
  next:
    "After validation, you’ll review everything before the baseline can be approved.",
  busyMessage: "Checking the file and importing your workspace…",
  stateLabel: "Waiting for a file",
  tone: "neutral",
};

export function workspaceGuide(status?: LiveWorkspaceStatus): WorkspaceGuide {
  if (!status) return IMPORT_GUIDE;
  const stage = activeWorkspaceStage(status);
  const step = WORKSPACE_STAGES.findIndex((item) => item.id === stage) + 1;
  return {
    ...GUIDE_BY_STATUS[status],
    step,
    totalSteps: WORKSPACE_STAGES.length,
  };
}

export function workspaceVerificationReport(
  workspace: LiveWorkspaceView,
): Record<string, unknown> {
  return {
    exported_at: new Date().toISOString(),
    workspace: {
      id: workspace.id,
      name: workspace.name,
      status: workspace.status,
      graph_version: workspace.graphVersion,
    },
    approved_change: workspace.latestApprovedMutation
      ? {
          decision_id: workspace.latestApprovedMutation.decision.id,
          title: workspace.latestApprovedMutation.decision.title,
          text: workspace.latestApprovedMutation.decision.text,
          affected_scopes: workspace.latestApprovedMutation.affectedScopes,
        }
      : null,
    outcome: {
      invalidated_tasks:
        workspace.invalidationReport?.invalidated_task_ids ?? [],
      preserved_tasks: workspace.invalidationReport?.preserved_task_ids ?? [],
      original_authorization: workspace.initialVerification
        ? {
            applied: workspace.initialVerification.applied,
            verification_code:
              workspace.initialVerification.verificationCode,
          }
        : null,
      replacement_authorization: workspace.replacementVerification
        ? {
            applied: workspace.replacementVerification.applied,
            verification_code:
              workspace.replacementVerification.verificationCode,
          }
        : null,
      execution_receipt: workspace.replacementVerification?.callReceipt
        ? {
            execution_mode:
              workspace.replacementVerification.executionMode ?? null,
            provider:
              workspace.replacementVerification.callReceipt.provider,
            call_id:
              workspace.replacementVerification.callReceipt.callId,
            status:
              workspace.replacementVerification.callReceipt.status,
            evidence_ref:
              workspace.replacementVerification.callReceipt.evidenceRef,
          }
        : null,
    },
    provenance: {
      path: workspace.conflictAuthorization?.invalidationPath ?? [],
      evidence_refs: workspace.conflictAuthorization?.evidenceRefs ?? [],
    },
    activity: workspace.history,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export interface WorkspaceReadiness {
  approvedDecision: boolean;
  ticketAndTasks: boolean;
  scopedPlan: boolean;
  authorityRoles: boolean;
  ready: boolean;
}

export function workspaceReadiness(
  document: Partial<WorkspaceImportDocument>,
): WorkspaceReadiness {
  const baseline = document.baseline_decision;
  const ticket = document.ticket;
  const tasks = document.tasks;
  const plan = document.plan;
  const authorityPolicy = document.authority_policy;
  const baselineScopes =
    isRecord(baseline) && Array.isArray(baseline.scopes)
      ? baseline.scopes.filter((scope): scope is string => typeof scope === "string")
      : [];
  const approvedDecision =
    isRecord(baseline) &&
    baseline.kind === "Decision" &&
    baseline.approval_status === "proposal" &&
    baselineScopes.length > 0;
  const ticketAndTasks =
    isRecord(ticket) &&
    ticket.kind === "Ticket" &&
    Array.isArray(tasks) &&
    tasks.length > 0 &&
    tasks.every((task) => isRecord(task) && task.kind === "Task");
  const scopedPlan =
    isRecord(plan) &&
    typeof plan.id === "string" &&
    Array.isArray(plan.actions) &&
    plan.actions.length > 0;
  const authorityRoles =
    isRecord(authorityPolicy) &&
    baselineScopes.length > 0 &&
    baselineScopes.every(
      (scope) =>
        Array.isArray(authorityPolicy[scope]) &&
        (authorityPolicy[scope] as unknown[]).some(
          (role) => typeof role === "string" && role.length > 0,
        ),
    );
  return {
    approvedDecision,
    ticketAndTasks,
    scopedPlan,
    authorityRoles,
    ready:
      approvedDecision && ticketAndTasks && scopedPlan && authorityRoles,
  };
}
