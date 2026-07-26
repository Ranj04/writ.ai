/**
 * Adapter from Lane B's real agent-service payloads to this route's view model.
 *
 * Lane B did not build `GET /approvals/pending`; the approval surface lives on
 * the agent service under `/live-workspaces` (see ASSUMPTIONS.md A1). Everything
 * in this file is a pure transform over payloads the server already computed —
 * in particular the blast radius, which comes from `WorkspaceApprovalPreview`
 * (itself produced by `SupervisorInterruptPort.preview()`). Nothing here decides
 * who is affected.
 */

import type { PendingChange, Person, ProvenanceNode } from "./model";

/* ---------- the subset of Lane B's models this route reads ---------- */

export type ServerArtifact = {
  id: string;
  kind?: string;
  title?: string;
  text?: string;
  scopes?: string[];
  attributes?: Record<string, unknown>;
  source_ref?: string;
  effective_at?: string;
};

export type ServerMutation = {
  decision: ServerArtifact;
  supersedes_id?: string;
  affected_scopes?: string[];
};

export type ServerAssignment = {
  id: string;
  task_id?: string;
  task_title?: string;
  agent_name?: string;
  state?: string;
  scopes?: string[];
};

export type ServerAppliedInterrupt = {
  decision_id?: string;
  interrupted_assignment_ids?: string[];
  preserved_assignment_ids?: string[];
};

export type ServerWorkspace = {
  id: string;
  name?: string;
  baseline_decision?: ServerArtifact;
  specification?: ServerArtifact;
  ticket?: ServerArtifact;
  tasks?: ServerArtifact[];
  initial_plan?: ServerArtifact;
  current_plan?: ServerArtifact;
  pending_mutation?: ServerMutation | null;
  approved_mutations?: { mutation?: ServerMutation }[];
  supervisor?: {
    assignments?: ServerAssignment[];
    applied_interrupts?: ServerAppliedInterrupt[];
  } | null;
};

export type ServerPendingApproval = {
  workspace_id: string;
  decision_id: string;
  supersedes_id: string;
  affected_scopes: string[];
  permission_id: string;
  source_ref: string;
  title: string;
  text: string;
  effective_at: string;
  requirements: Record<string, Record<string, unknown>>;
  /**
   * The exact proposal a human confirmed. The approve route rejects an approval
   * that does not echo both of these back (409 MISSING_PROPOSAL_CONFIRMATION /
   * STALE_CONFIRMATION), which is what stops an approval of a proposal that has
   * since been edited.
   */
  proposal_fingerprint: string;
  proposal_instance_id: string;
};

export type ServerPreview = {
  pending: ServerPendingApproval;
  interrupted_assignment_ids?: string[];
  preserved_assignment_ids?: string[];
  assignment_provenance_paths?: Record<string, string[]>;
};

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

/**
 * `_preview_payload` in cli_approve.py accepts both shapes; so do we.
 *
 * Validates the whole runtime shape, not a sample of it. Everything downstream
 * calls `.map`, `.trim`, `.match` and `.length` on these fields, so a partial
 * payload that gets past this function crashes the route instead of falling back.
 */
export function unwrapPreview(body: unknown): ServerPreview | null {
  if (!isRecord(body)) {
    return null;
  }
  const candidate = isRecord(body.preview) ? body.preview : body;
  if (!isRecord(candidate.pending)) {
    return null;
  }
  const pending = candidate.pending;
  const strings = [
    "workspace_id",
    "decision_id",
    "supersedes_id",
    "permission_id",
    "source_ref",
    "text",
    "proposal_fingerprint",
    "proposal_instance_id",
  ];
  if (strings.some((key) => typeof pending[key] !== "string")) {
    return null;
  }
  if (!isStringArray(pending.affected_scopes)) {
    return null;
  }
  if (pending.title !== undefined && typeof pending.title !== "string") {
    return null;
  }
  if (
    pending.effective_at !== undefined &&
    typeof pending.effective_at !== "string"
  ) {
    return null;
  }
  if (!isRecord(pending.requirements)) {
    return null;
  }
  for (const key of ["interrupted_assignment_ids", "preserved_assignment_ids"]) {
    if (candidate[key] !== undefined && !isStringArray(candidate[key])) {
      return null;
    }
  }
  const paths = candidate.assignment_provenance_paths;
  if (paths !== undefined) {
    if (!isRecord(paths)) {
      return null;
    }
    if (!Object.values(paths).every(isStringArray)) {
      return null;
    }
  }
  return candidate as unknown as ServerPreview;
}

/**
 * A preview must describe the change we asked about.
 *
 * `cli_approve.py:454 _pending_from_preview` raises on exactly this mismatch;
 * a cached or misrouted preview joined onto another workspace's assignments
 * would render one team's blast radius under another team's decision.
 */
export function previewMatches(
  preview: ServerPreview,
  workspaceId: string,
  decisionId: string,
): boolean {
  return (
    preview.pending.workspace_id === workspaceId &&
    preview.pending.decision_id === decisionId
  );
}

export function pendingDecisionId(workspace: ServerWorkspace): string | null {
  const id = workspace.pending_mutation?.decision?.id;
  return typeof id === "string" && id ? id : null;
}

/* ---------- the transform ---------- */

export function toPendingChange(
  workspace: ServerWorkspace,
  preview: ServerPreview,
): PendingChange | null {
  const pending = preview.pending;
  const scopes = [...(pending.affected_scopes ?? [])].sort();
  const scope = scopes[0];
  if (!scope) {
    return null;
  }
  // A multi-scope change would otherwise render as if it touched only one
  // scope, while the blast radius below covers all of them. Say so instead.
  const scopeLabel =
    scopes.length > 1
      ? `${scope} (+${scopes.length - 1} more ${
          scopes.length === 2 ? "scope" : "scopes"
        })`
      : scope;

  const assignments = assignmentIndex(workspace);
  const interrupted = people(preview.interrupted_assignment_ids, assignments);
  const preserved = people(preview.preserved_assignment_ids, assignments);

  return {
    id: `${pending.workspace_id}:${pending.decision_id}`,
    source: sourceOf(pending),
    decision: {
      id: pending.decision_id,
      supersedes: pending.supersedes_id,
      scope: scopeLabel,
      was: previousRequirement(workspace, scope),
      now: formatRequirement(pending.requirements?.[scope]),
    },
    provenancePath: rail(workspace, preview, interrupted, preserved),
    blastRadius: { interrupted, preserved },
    approverPermission: pending.permission_id,
  };
}

/**
 * What the server actually did, read back off the workspace the approve route
 * returns.
 *
 * `WorkspaceSupervisor.applied_interrupts` (`workspaces/supervisor.py:86`) is the
 * server's own record of the partition it applied for one decision. It is not
 * the preview: if the two differ — because state moved between the approver
 * seeing the blast radius and confirming it — this is the one that happened.
 */
export function appliedPartition(
  workspace: ServerWorkspace,
  decisionId: string,
): { interrupted: Person[]; preserved: Person[] } | null {
  const applied = (workspace.supervisor?.applied_interrupts ?? []).find(
    (entry) => entry?.decision_id === decisionId,
  );
  if (!applied) {
    return null;
  }
  const assignments = assignmentIndex(workspace);
  return {
    interrupted: people(applied.interrupted_assignment_ids, assignments),
    preserved: people(applied.preserved_assignment_ids, assignments),
  };
}

function assignmentIndex(
  workspace: ServerWorkspace,
): Map<string, ServerAssignment> {
  return new Map(
    (workspace.supervisor?.assignments ?? [])
      .filter((entry) => typeof entry?.id === "string")
      .map((entry) => [entry.id, entry] as const),
  );
}

function people(
  ids: string[] | undefined,
  assignments: Map<string, ServerAssignment>,
): Person[] {
  return (ids ?? []).map((assignmentId) => {
    const assignment = assignments.get(assignmentId);
    const name = assignment?.agent_name?.trim() || assignmentId;
    return {
      assignmentId,
      name,
      initials: initialsOf(name),
      taskId: assignment?.task_id ?? "",
    };
  });
}

export function initialsOf(name: string): string {
  const words = name.trim().split(/[\s._-]+/).filter(Boolean);
  if (words.length === 0) {
    return "??";
  }
  if (words.length === 1) {
    return words[0].slice(0, 2).toUpperCase();
  }
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

const SLACK_SOURCE = /^slack:\/\/([^/]+)/i;

/**
 * `PendingApproval` carries no author (ASSUMPTIONS.md A3). Rather than invent a
 * person on a screen whose job is to show who decided something, fall back to
 * attributing the message to its own source reference.
 */
function sourceOf(pending: ServerPendingApproval): PendingChange["source"] {
  const sourceRef = typeof pending.source_ref === "string" ? pending.source_ref : "";
  const slack = sourceRef.match(SLACK_SOURCE);
  const channel = slack ? slack[1] : sourceRef || "unknown source";
  // NOT `pending.title` — that is the decision's title, not a person. Showing it
  // as the author would attribute the decision to a sentence.
  const author = sourceRef || "Unattributed";
  return {
    channel,
    author,
    authorInitials: initialsOf(channel),
    timestamp: formatTimestamp(pending.effective_at),
    text: typeof pending.text === "string" ? pending.text : "",
  };
}

export function formatTimestamp(value: string | undefined): string {
  if (!value) {
    return "";
  }
  const at = new Date(value);
  return Number.isNaN(at.getTime())
    ? value
    : at.toLocaleString(undefined, {
        hour: "numeric",
        minute: "2-digit",
        day: "numeric",
        month: "short",
      });
}

export function formatRequirement(value: unknown): string {
  if (value === undefined || value === null) {
    return "not previously constrained";
  }
  if (typeof value === "string") {
    return value;
  }
  if (isRecord(value)) {
    const parts = Object.entries(value)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, entry]) => `${key}=${scalar(entry)}`);
    return parts.length > 0 ? parts.join(", ") : "no requirements";
  }
  return String(value);
}

function scalar(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/**
 * The value the currently-approved decision holds for this scope.
 *
 * Precedence follows the engine: `current_requirements()` orders candidates by
 * `effective_at` (`engine.py:344-350`), so the latest effective approved
 * mutation wins and the baseline is only the fallback. Searching the list in
 * arrival order would show a superseded value as the "was".
 */
function previousRequirement(workspace: ServerWorkspace, scope: string): string {
  const approved = (workspace.approved_mutations ?? [])
    .map((entry) => entry?.mutation?.decision)
    .filter((decision): decision is ServerArtifact => Boolean(decision))
    .map((decision) => ({
      decision,
      effectiveAt: effectiveAtOf(decision),
    }))
    .sort((a, b) => b.effectiveAt - a.effectiveAt)
    .map((entry) => entry.decision);

  const candidates = workspace.baseline_decision
    ? [...approved, workspace.baseline_decision]
    : approved;

  for (const decision of candidates) {
    const requirements = decision.attributes?.["requirements"];
    if (isRecord(requirements) && scope in requirements) {
      return formatRequirement(requirements[scope]);
    }
  }
  return "not previously constrained";
}

/** Missing or unparseable `effective_at` sorts last, as `datetime.min` does. */
function effectiveAtOf(decision: ServerArtifact): number {
  const raw = decision.attributes?.["effective_at"] ?? decision.effective_at;
  if (typeof raw !== "string") {
    return Number.NEGATIVE_INFINITY;
  }
  const at = new Date(raw).getTime();
  return Number.isNaN(at) ? Number.NEGATIVE_INFINITY : at;
}

/**
 * One rail (ASSUMPTIONS.md A5): the longest interrupted path, ties broken by
 * assignment id so the render is deterministic, then the surviving siblings'
 * tasks appended as unaffected — that branch is the point of the screen.
 */
function rail(
  workspace: ServerWorkspace,
  preview: ServerPreview,
  interrupted: Person[],
  preserved: Person[],
): ProvenanceNode[] {
  const titles = titleIndex(workspace);
  const paths = preview.assignment_provenance_paths ?? {};
  const chosen = Object.entries(paths)
    .sort(([idA, a], [idB, b]) => b.length - a.length || idA.localeCompare(idB))
    .map(([, path]) => path)[0];

  const nodes: ProvenanceNode[] = (chosen ?? []).map((id) => ({
    id,
    title: titles.get(id) ?? id,
    detail: detailFor(id, interrupted),
    affected: true,
  }));

  const seen = new Set(nodes.map((node) => node.id));
  for (const person of preserved) {
    if (!person.taskId || seen.has(person.taskId)) {
      continue;
    }
    seen.add(person.taskId);
    nodes.push({
      id: person.taskId,
      title: titles.get(person.taskId) ?? person.taskId,
      detail: "out of scope · unaffected",
      affected: false,
    });
  }
  return nodes;
}

function detailFor(id: string, interrupted: Person[]): string {
  const count = interrupted.filter((person) => person.taskId === id).length;
  if (count > 0) {
    return `${count} ${count === 1 ? "session" : "sessions"} affected`;
  }
  return id;
}

function titleIndex(workspace: ServerWorkspace): Map<string, string> {
  const index = new Map<string, string>();
  const add = (artifact: ServerArtifact | undefined | null): void => {
    if (artifact?.id) {
      index.set(artifact.id, artifact.title?.trim() || artifact.id);
    }
  };
  add(workspace.baseline_decision);
  add(workspace.specification);
  add(workspace.ticket);
  add(workspace.initial_plan);
  add(workspace.current_plan);
  for (const task of workspace.tasks ?? []) {
    add(task);
  }
  for (const approved of workspace.approved_mutations ?? []) {
    add(approved?.mutation?.decision);
  }
  add(workspace.pending_mutation?.decision);
  return index;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
