import { parse as parseYaml, stringify as stringifyYaml } from "yaml";
import type { InvalidationReport, Verdict } from "../types";
import {
  LiveWorkspaceApiError,
  type LiveWorkspaceClient,
  type LiveWorkspaceView,
  type WorkspaceArtifact,
  type WorkspaceApprovalConfirmation,
  type WorkspaceAuthorization,
  type WorkspaceCallReceipt,
  type WorkspaceDecisionMutation,
  type WorkspaceDocumentFormat,
  type WorkspaceEvent,
  type WorkspaceExecution,
  type WorkspaceExecutionMode,
  type WorkspaceGrantMetadata,
  type WorkspaceImportDocument,
  type WorkspacePlan,
  type WorkspaceSupervisor,
  type WorkspaceSubagentAssignment,
  type WorkspaceValidationIssue,
} from "./model";
import { serviceBaseUrl } from "../api";
import { createWorkspaceRun } from "./sample";

const AGENT = serviceBaseUrl(import.meta.env.VITE_AGENT_URL, "http://localhost:8002");

interface RawArtifact {
  id: string;
  kind: string;
  title: string;
  text?: string;
  scopes?: string[];
  validity?: WorkspaceArtifact["validity"];
  invalidated_scopes?: string[];
  approval_status?: WorkspaceArtifact["approvalStatus"];
  authority_role?: string | null;
  confidence?: number;
  effective_at?: string | null;
  source_ref?: string | null;
  attributes?: Record<string, unknown>;
}

interface RawPlanAction {
  id: string;
  description: string;
  scopes?: string[];
  attributes?: Record<string, unknown>;
}

interface RawPlan {
  id: string;
  ticket_id: string;
  objective: string;
  actions: RawPlanAction[];
}

interface RawGrant {
  authorization_id: string;
  run_id: string;
  task_id: string;
  decision_snapshot: string;
  plan_hash: string;
  verdict: Verdict;
  issued_at: string;
  expires_at: string;
}

interface RawAuthorization {
  verdict: Verdict;
  reason: string;
  graph_version: string;
  task_id: string;
  affected_scopes?: string[];
  mismatches?: Array<{
    action_id: string;
    scope: string;
    expected: Record<string, unknown>;
    actual: Record<string, unknown>;
  }>;
  current_requirements?: Record<string, Record<string, unknown>>;
  invalidation_path?: string[];
  invalidated_artifact_ids?: string[];
  preserved_artifact_ids?: string[];
  evidence_refs?: string[];
  grant?: RawGrant | null;
}

interface RawExecution {
  applied: boolean;
  reason: string;
  verification_code: string;
  pull_request_url?: string | null;
  execution_mode?: WorkspaceExecutionMode | null;
  call_receipt?: {
    provider: string;
    call_id: string;
    status: string;
    evidence_ref: string;
  } | null;
}

interface RawMutation {
  decision: RawArtifact;
  supersedes_id: string;
  affected_scopes: string[];
}

interface RawEvent {
  sequence: number;
  event_type: string;
  detail: string;
  created_at: string;
  actor_role?: string | null;
  data?: Record<string, unknown>;
}

interface RawSubagentAssignment {
  id: string;
  task_id: string;
  task_title: string;
  agent_name: string;
  runtime_provider?: WorkspaceSubagentAssignment["runtimeProvider"];
  execution_mode?: WorkspaceSubagentAssignment["executionMode"];
  run_id: string;
  state: WorkspaceSubagentAssignment["state"];
  scopes?: string[];
  action_ids?: string[];
  plan_id: string;
  decision_snapshot: string;
  redirected_from_run_id?: string | null;
  interrupt_reason?: string | null;
  redirect_instruction?: string | null;
  provenance_path?: string[];
  interrupt_enforced?: boolean;
}

interface RawSupervisor {
  id: string;
  name?: string;
  state: string;
  adapter?: string;
  execution_mode?: WorkspaceSupervisor["executionMode"];
  assignments?: RawSubagentAssignment[];
}

interface RawWorkspace {
  id: string;
  name: string;
  description?: string;
  status: LiveWorkspaceView["status"];
  graph_version: string;
  baseline_approved: boolean;
  baseline_proposal_fingerprint?: string | null;
  baseline_proposal_instance_id?: string | null;
  baseline_decision: RawArtifact;
  specification: RawArtifact;
  ticket: RawArtifact;
  tasks: RawArtifact[];
  initial_plan: RawPlan;
  current_plan: RawPlan;
  authority_policy: Record<string, string[]>;
  pending_mutation?: RawMutation | null;
  pending_proposal_fingerprint?: string | null;
  pending_proposal_instance_id?: string | null;
  latest_approved_mutation?: RawMutation | null;
  initial_authorization?: RawAuthorization | null;
  conflict_authorization?: RawAuthorization | null;
  replacement_authorization?: RawAuthorization | null;
  invalidation_report?: InvalidationReport | null;
  initial_verification?: RawExecution | null;
  replacement_verification?: RawExecution | null;
  supervisor?: RawSupervisor | null;
  history?: RawEvent[];
  created_at: string;
  updated_at: string;
}

interface RawWorkspaceList {
  workspaces: RawWorkspace[];
}

function mapArtifact(raw: RawArtifact): WorkspaceArtifact {
  return {
    id: raw.id,
    kind: raw.kind,
    title: raw.title,
    text: raw.text ?? "",
    scopes: raw.scopes ?? [],
    validity: raw.validity ?? "VALID",
    invalidatedScopes: raw.invalidated_scopes ?? [],
    approvalStatus: raw.approval_status,
    authorityRole: raw.authority_role,
    confidence: raw.confidence ?? 1,
    effectiveAt: raw.effective_at,
    sourceRef: raw.source_ref,
    attributes: raw.attributes ?? {},
  };
}

function mapPlan(raw: RawPlan): WorkspacePlan {
  return {
    id: raw.id,
    ticketId: raw.ticket_id,
    objective: raw.objective,
    actions: raw.actions.map((action) => ({
      id: action.id,
      description: action.description,
      scopes: action.scopes ?? [],
      attributes: action.attributes ?? {},
    })),
  };
}

function mapGrant(raw: RawGrant | null | undefined): WorkspaceGrantMetadata | null {
  if (!raw) return null;
  return {
    authorizationId: raw.authorization_id,
    runId: raw.run_id,
    taskId: raw.task_id,
    decisionSnapshot: raw.decision_snapshot,
    planHash: raw.plan_hash,
    verdict: raw.verdict,
    issuedAt: raw.issued_at,
    expiresAt: raw.expires_at,
  };
}

function mapAuthorization(
  raw: RawAuthorization | null | undefined,
): WorkspaceAuthorization | null {
  if (!raw) return null;
  return {
    verdict: raw.verdict,
    reason: raw.reason,
    graphVersion: raw.graph_version,
    taskId: raw.task_id,
    affectedScopes: raw.affected_scopes ?? [],
    mismatches: (raw.mismatches ?? []).map((mismatch) => ({
      actionId: mismatch.action_id,
      scope: mismatch.scope,
      expected: mismatch.expected,
      actual: mismatch.actual,
    })),
    currentRequirements: raw.current_requirements ?? {},
    invalidationPath: raw.invalidation_path ?? [],
    invalidatedArtifactIds: raw.invalidated_artifact_ids ?? [],
    preservedArtifactIds: raw.preserved_artifact_ids ?? [],
    evidenceRefs: raw.evidence_refs ?? [],
    grant: mapGrant(raw.grant),
  };
}

function mapExecution(
  raw: RawExecution | null | undefined,
): WorkspaceExecution | null {
  if (!raw) return null;
  const receipt: WorkspaceCallReceipt | null = raw.call_receipt
    ? {
        provider: raw.call_receipt.provider,
        callId: raw.call_receipt.call_id,
        status: raw.call_receipt.status,
        evidenceRef: raw.call_receipt.evidence_ref,
      }
    : null;
  return {
    applied: raw.applied,
    reason: raw.reason,
    verificationCode: raw.verification_code,
    pullRequestUrl: raw.pull_request_url,
    executionMode: raw.execution_mode,
    callReceipt: receipt,
  };
}

function mapMutation(
  raw: RawMutation | null | undefined,
): WorkspaceDecisionMutation | null {
  if (!raw) return null;
  return {
    decision: mapArtifact(raw.decision),
    supersedesId: raw.supersedes_id,
    affectedScopes: raw.affected_scopes,
  };
}

function mapEvent(raw: RawEvent): WorkspaceEvent {
  return {
    sequence: raw.sequence,
    eventType: raw.event_type,
    detail: raw.detail,
    createdAt: raw.created_at,
    actorRole: raw.actor_role,
    data: raw.data ?? {},
  };
}

function mapSupervisor(
  raw: RawSupervisor | null | undefined,
): WorkspaceSupervisor | null {
  if (!raw) return null;
  return {
    id: raw.id,
    name: raw.name ?? "writ.ai Supervisor",
    state: raw.state,
    adapter: raw.adapter ?? "fixture-agent-runtime",
    executionMode: raw.execution_mode ?? "simulated",
    assignments: (raw.assignments ?? []).map((assignment) => ({
      id: assignment.id,
      taskId: assignment.task_id,
      taskTitle: assignment.task_title,
      agentName: assignment.agent_name,
      runtimeProvider: assignment.runtime_provider ?? "generic",
      executionMode: assignment.execution_mode ?? "simulated",
      runId: assignment.run_id,
      state: assignment.state,
      scopes: assignment.scopes ?? [],
      actionIds: assignment.action_ids ?? [],
      planId: assignment.plan_id,
      decisionSnapshot: assignment.decision_snapshot,
      redirectedFromRunId: assignment.redirected_from_run_id,
      interruptReason: assignment.interrupt_reason,
      redirectInstruction: assignment.redirect_instruction,
      provenancePath: assignment.provenance_path ?? [],
      interruptEnforced: assignment.interrupt_enforced ?? false,
    })),
  };
}

export function mapLiveWorkspace(raw: RawWorkspace): LiveWorkspaceView {
  return {
    id: raw.id,
    name: raw.name,
    description: raw.description ?? "",
    status: raw.status,
    graphVersion: raw.graph_version,
    baselineApproved: raw.baseline_approved,
    baselineProposalFingerprint: raw.baseline_proposal_fingerprint,
    baselineProposalInstanceId: raw.baseline_proposal_instance_id,
    baselineDecision: mapArtifact(raw.baseline_decision),
    specification: mapArtifact(raw.specification),
    ticket: mapArtifact(raw.ticket),
    tasks: raw.tasks.map(mapArtifact),
    initialPlan: mapPlan(raw.initial_plan),
    currentPlan: mapPlan(raw.current_plan),
    authorityPolicy: raw.authority_policy,
    pendingMutation: mapMutation(raw.pending_mutation),
    pendingProposalFingerprint: raw.pending_proposal_fingerprint,
    pendingProposalInstanceId: raw.pending_proposal_instance_id,
    latestApprovedMutation: mapMutation(raw.latest_approved_mutation),
    initialAuthorization: mapAuthorization(raw.initial_authorization),
    conflictAuthorization: mapAuthorization(raw.conflict_authorization),
    replacementAuthorization: mapAuthorization(raw.replacement_authorization),
    invalidationReport: raw.invalidation_report,
    initialVerification: mapExecution(raw.initial_verification),
    replacementVerification: mapExecution(raw.replacement_verification),
    supervisor: mapSupervisor(raw.supervisor),
    history: (raw.history ?? []).map(mapEvent).sort(
      (left, right) => left.sequence - right.sequence,
    ),
    createdAt: raw.created_at,
    updatedAt: raw.updated_at,
  };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseWorkspaceDocument(
  content: string,
  format?: WorkspaceDocumentFormat,
): WorkspaceImportDocument {
  if (!content.trim()) {
    throw new LiveWorkspaceApiError(
      "Paste a workspace document or choose a file.",
      "EMPTY_DOCUMENT",
    );
  }
  let parsed: unknown;
  try {
    const useJson =
      format === "json" ||
      (format === undefined && /^\s*[\[{]/.test(content));
    parsed = useJson ? JSON.parse(content) : parseYaml(content);
  } catch (caught) {
    throw new LiveWorkspaceApiError(
      caught instanceof Error
        ? `The workspace document could not be parsed: ${caught.message}`
        : "The workspace document could not be parsed.",
      "INVALID_DOCUMENT",
    );
  }
  if (!isObject(parsed)) {
    throw new LiveWorkspaceApiError(
      "The workspace document must contain one object at its top level.",
      "INVALID_DOCUMENT",
    );
  }
  return parsed as unknown as WorkspaceImportDocument;
}

export function serializeWorkspaceDocument(
  document: WorkspaceImportDocument,
  format: WorkspaceDocumentFormat,
): string {
  return format === "yaml"
    ? stringifyYaml(document)
    : JSON.stringify(document, null, 2);
}

export function prepareWorkspaceDocumentRun(
  content: string,
  format: WorkspaceDocumentFormat,
  runToken?: string,
): { content: string; document: WorkspaceImportDocument } {
  const document = createWorkspaceRun(
    parseWorkspaceDocument(content, format),
    runToken,
  );
  return {
    content: serializeWorkspaceDocument(document, format),
    document,
  };
}

export async function importWorkspaceWithConflictRetry(
  client: LiveWorkspaceClient,
  document: WorkspaceImportDocument,
  options: { signal?: AbortSignal } = {},
  runToken?: string,
): Promise<{
  document: WorkspaceImportDocument;
  retried: boolean;
  workspace: LiveWorkspaceView;
}> {
  try {
    const workspace = await client.importWorkspace(document, options);
    return { document, retried: false, workspace };
  } catch (caught) {
    if (
      !(caught instanceof LiveWorkspaceApiError) ||
      caught.code !== "LIVE_WORKSPACE_CONFLICT"
    ) {
      throw caught;
    }
    const freshDocument = createWorkspaceRun(document, runToken);
    const workspace = await client.importWorkspace(freshDocument, options);
    return { document: freshDocument, retried: true, workspace };
  }
}

function normalizeIssues(raw: unknown): WorkspaceValidationIssue[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((issue) => {
    if (!isObject(issue)) return [];
    const rawLocation = issue.location;
    const location = Array.isArray(rawLocation)
      ? rawLocation.map(String).join(".")
      : typeof rawLocation === "string"
        ? rawLocation
        : "document";
    return [
      {
        location,
        type:
          typeof issue.type === "string"
            ? issue.type
            : "Invalid value",
      },
    ];
  });
}

async function workspaceJson<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${AGENT}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        ...(init.headers ?? {}),
      },
    });
  } catch (caught) {
    init.signal?.throwIfAborted();
    throw new LiveWorkspaceApiError(
      caught instanceof Error
        ? `Agent service: ${caught.message}`
        : "Agent service: network request failed.",
      "NETWORK_ERROR",
    );
  }
  if (!response.ok) {
    let message = `Agent service returned HTTP ${response.status}.`;
    let code = `HTTP_${response.status}`;
    let issues: WorkspaceValidationIssue[] = [];
    try {
      const body = (await response.json()) as {
        error?: {
          code?: unknown;
          message?: unknown;
          details?: { issues?: unknown };
        };
      };
      if (typeof body.error?.message === "string") {
        message = body.error.message;
      }
      if (typeof body.error?.code === "string") code = body.error.code;
      issues = normalizeIssues(body.error?.details?.issues);
    } catch {
      // Preserve the safe HTTP status fallback for a non-JSON response.
    }
    throw new LiveWorkspaceApiError(message, code, issues);
  }
  return response.json() as Promise<T>;
}

function workspacePath(workspaceId: string, suffix = ""): string {
  return `/live-workspaces/${encodeURIComponent(workspaceId)}${suffix}`;
}

function approvalBody(
  workspaceId: string,
  decisionRef: string,
  confirmation: WorkspaceApprovalConfirmation,
) {
  return {
    approval_token: confirmation.approvalToken,
    channel: "workspace-ui",
    evidence_ref: [
      "workspace-ui:/",
      encodeURIComponent(workspaceId),
      encodeURIComponent(decisionRef),
      confirmation.proposalFingerprint,
    ].join("/"),
    confirmed_proposal_fingerprint: confirmation.proposalFingerprint,
    confirmed_proposal_instance_id: confirmation.proposalInstanceId,
  };
}

export function createLiveWorkspaceClient(): LiveWorkspaceClient {
  return {
    list: async (options) => {
      const raw = await workspaceJson<RawWorkspaceList>("/live-workspaces", {
        signal: options?.signal,
      });
      return raw.workspaces.map(mapLiveWorkspace);
    },
    load: async (workspaceId, options) => {
      const raw = await workspaceJson<RawWorkspace>(
        workspacePath(workspaceId),
        { signal: options?.signal },
      );
      return mapLiveWorkspace(raw);
    },
    importWorkspace: async (document, options) => {
      const raw = await workspaceJson<RawWorkspace>(
        "/live-workspaces/import",
        {
          method: "POST",
          signal: options?.signal,
          body: JSON.stringify(document),
        },
      );
      return mapLiveWorkspace(raw);
    },
    approveBaseline: async (workspaceId, confirmation, options) => {
      const raw = await workspaceJson<RawWorkspace>(
        workspacePath(workspaceId, "/baseline/approve"),
        {
          method: "POST",
          signal: options?.signal,
          body: JSON.stringify(
            approvalBody(
              workspaceId,
              "baseline",
              confirmation,
            ),
          ),
        },
      );
      return mapLiveWorkspace(raw);
    },
    authorizePlan: async (workspaceId, options) => {
      const raw = await workspaceJson<RawWorkspace>(
        workspacePath(workspaceId, "/authorize"),
        {
          method: "POST",
          signal: options?.signal,
          body: "{}",
        },
      );
      return mapLiveWorkspace(raw);
    },
    proposeChange: async (workspaceId, mutation, options) => {
      const raw = await workspaceJson<RawWorkspace>(
        workspacePath(workspaceId, "/decisions/propose"),
        {
          method: "POST",
          signal: options?.signal,
          body: JSON.stringify(mutation),
        },
      );
      return mapLiveWorkspace(raw);
    },
    cancelPendingChange: async (workspaceId, options) => {
      const raw = await workspaceJson<RawWorkspace>(
        workspacePath(workspaceId, "/decisions/pending"),
        {
          method: "DELETE",
          signal: options?.signal,
        },
      );
      return mapLiveWorkspace(raw);
    },
    approveChange: async (
      workspaceId,
      decisionId,
      confirmation,
      options,
    ) => {
      const raw = await workspaceJson<RawWorkspace>(
        workspacePath(
          workspaceId,
          `/decisions/${encodeURIComponent(decisionId)}/approve`,
        ),
        {
          method: "POST",
          signal: options?.signal,
          body: JSON.stringify(
            approvalBody(
              workspaceId,
              decisionId,
              confirmation,
            ),
          ),
        },
      );
      return mapLiveWorkspace(raw);
    },
    verifyInitialGrant: async (workspaceId, options) => {
      const raw = await workspaceJson<RawWorkspace>(
        workspacePath(workspaceId, "/grants/initial/verify"),
        {
          method: "POST",
          signal: options?.signal,
          body: "{}",
        },
      );
      return mapLiveWorkspace(raw);
    },
    updatePlan: async (workspaceId, plan, options) => {
      const raw = await workspaceJson<RawWorkspace>(
        workspacePath(workspaceId, "/plan"),
        {
          method: "PUT",
          signal: options?.signal,
          body: JSON.stringify({ plan }),
        },
      );
      return mapLiveWorkspace(raw);
    },
    reauthorize: async (workspaceId, options) => {
      const raw = await workspaceJson<RawWorkspace>(
        workspacePath(workspaceId, "/reauthorize"),
        {
          method: "POST",
          signal: options?.signal,
          body: "{}",
        },
      );
      return mapLiveWorkspace(raw);
    },
    verifyReplacementGrant: async (workspaceId, options) => {
      const raw = await workspaceJson<RawWorkspace>(
        workspacePath(workspaceId, "/grants/replacement/verify"),
        {
          method: "POST",
          signal: options?.signal,
          body: "{}",
        },
      );
      return mapLiveWorkspace(raw);
    },
  };
}
