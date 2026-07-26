import { hexclaveApprovalToken } from "../hexclave/client";
import { FIXTURE_PENDING_CHANGES } from "./fixtures";
import {
  appliedPartition,
  pendingDecisionId,
  previewMatches,
  toPendingChange,
  unwrapPreview,
  type ServerWorkspace,
} from "./live";
import type { ApprovalReceipt, DataSource, PendingChange } from "./model";

/**
 * Wired to Lane B's real surface on the AGENT service. The routes assumed by
 * `docs/BUILD_LANE_D.md` (`/approvals/pending`, `/approvals/{id}/approve` on the
 * authority service) were never built — see ASSUMPTIONS.md A1.
 *
 *   GET  {AGENT_URL}/live-workspaces
 *   GET  {AGENT_URL}/live-workspaces/{id}/decisions/{decision_id}/preview
 *   GET  {AGENT_URL}/live-workspaces/{id}/events            (SSE)
 *
 *   POST {AGENT_URL}/live-workspaces/{id}/decisions/{decision_id}/approve
 *
 * Approve IS wired, but only ever with a token read from the runtime — never
 * from a `VITE_*` variable, which Vite would inline into the bundle and ship to
 * every browser. See ASSUMPTIONS.md A2. With no token it does not post.
 *
 * On a failure to READ, we fall back to the fixture and say so loudly in the
 * console and on screen. A replayed payload is never labelled live.
 *
 * On a failure to read the answer to a WRITE, we do not: an approval that
 * landed and lost its response really did interrupt sessions, so the outcome is
 * `indeterminate` and is reconciled against the workspace rather than being
 * reported as a rehearsal. See `ApprovalOutcome`.
 */
const AGENT = import.meta.env.VITE_AGENT_URL ?? "http://localhost:8002";

const WORKSPACES_URL = `${AGENT}/live-workspaces`;

export type PendingChangesResult = {
  changes: PendingChange[];
  source: DataSource;
  /** Workspace ids behind the live changes, for the SSE subscription. */
  workspaceIds: string[];
};

/**
 * What we can honestly say happened.
 *
 * `indeterminate` exists because "we could not read the answer" is not the same
 * as "nothing happened". An approval that lands and then loses its response
 * really did interrupt sessions; reporting that as a rehearsal would put a
 * screen saying *no approval was recorded* in front of agents that are visibly
 * being redirected. Under-reporting a real mutation is the worse error, so a
 * failed read never resolves to "not applied".
 */
export type ApprovalOutcome = "applied" | "rehearsal" | "indeterminate";

export type ApprovalResult = {
  receipt: ApprovalReceipt;
  source: DataSource;
  outcome: ApprovalOutcome;
  /** Why this was a rehearsal or is unresolved, when it was. */
  note?: string;
};

const NO_APPROVAL_CHANNEL =
  "NOT SIGNED IN — this browser has no Hexclave identity, so nothing was " +
  "approved here. The staged demo approves through the unauthenticated seam " +
  "(WRITAI_DEMO_UNAUTHENTICATED_APPROVAL=1), which checks every authority rule " +
  "but proves no approver. Sign in, or approve with `writai approve`.";

/**
 * What a live approval needs, kept beside the change rather than inside it so
 * `PendingChange` stays exactly the shape the lane brief froze.
 */
type ApprovalBinding = Readonly<{
  workspaceId: string;
  decisionId: string;
  proposalFingerprint: string;
  proposalInstanceId: string;
}>;

/**
 * Keyed by the change OBJECT, not by its id, and never mutated once set.
 *
 * `PendingChange.id` is the composite `"{workspaceId}:{decisionId}"`, and the
 * fixture uses the same ids as the live workspace on purpose. Keying a shared
 * map by that string let a fixture card find a live binding left behind by an
 * earlier load and POST a real approval from a screen labelled "fixture data".
 * A stale load could do the same to a newer one.
 *
 * A WeakMap keyed on identity cannot: every load builds fresh objects, the
 * fixture's objects were never bound, and an old change carries only the
 * binding it was created with. It also lets a discarded load be collected.
 */
const bindings = new WeakMap<PendingChange, ApprovalBinding>();

/**
 * The approver's token, read from the runtime — never from a `VITE_*` variable.
 *
 * Vite inlines `import.meta.env` into the client bundle, so a token configured
 * that way would ship to every visitor. This reads a value an operator or a
 * server-rendered shell can place on `window` for one session instead. When it
 * is absent, approval is a rehearsal and the screen says so.
 */
async function approvalToken(): Promise<string | null> {
  // Preferred: a real Hexclave sign-in on this page. The server resolves the
  // access token to a user and checks THAT user's permission, so the person
  // clicking approve is the person the audit records.
  const signedIn = await hexclaveApprovalToken();
  if (signedIn) return signedIn;

  // Fallback, kept deliberately: an operator-pasted value for one session. It
  // predates the SDK and is still useful for a machine with no browser
  // sign-in. It is NOT a VITE_* variable, because Vite inlines those into the
  // bundle and would ship an approval credential to every visitor.
  const runtime = globalThis as { __WRITAI_APPROVAL_TOKEN__?: unknown };
  const token = runtime.__WRITAI_APPROVAL_TOKEN__;
  return typeof token === "string" && token.trim() ? token.trim() : null;
}

function fellBack(reason: string): void {
  // Never let fixture data pass for live data silently.
  console.warn(
    `[writai/approvals] ${reason} — rendering FIXTURE data. This is not a live approval.`,
  );
}

function fixtureResult(): PendingChangesResult {
  return {
    changes: FIXTURE_PENDING_CHANGES,
    source: "fixture",
    workspaceIds: [],
  };
}

export async function fetchPendingChanges(
  signal?: AbortSignal,
): Promise<PendingChangesResult> {
  let workspaces: ServerWorkspace[];
  try {
    const response = await fetch(WORKSPACES_URL, {
      headers: { Accept: "application/json" },
      signal,
    });
    if (!response.ok) {
      fellBack(`GET /live-workspaces returned ${response.status}`);
      return fixtureResult();
    }
    const parsed = readWorkspaces(await response.json());
    if (!parsed) {
      fellBack("GET /live-workspaces returned an unrecognised body");
      return fixtureResult();
    }
    workspaces = parsed;
  } catch (error) {
    fellBack(`GET /live-workspaces failed (${describe(error)})`);
    return fixtureResult();
  }

  const awaiting = workspaces.filter((workspace) => pendingDecisionId(workspace));
  if (awaiting.length === 0) {
    // The service answered; there is genuinely nothing pending. That is live
    // data, not a fallback — an empty queue is a real answer.
    return { changes: [], source: "live", workspaceIds: workspaces.map((w) => w.id) };
  }

  // Concurrent: one slow preview must not delay the rest of the queue.
  const settled = await Promise.all(
    awaiting.map((workspace) => {
      const decisionId = pendingDecisionId(workspace);
      return decisionId
        ? fetchOneChange(workspace, decisionId, signal)
        : Promise.resolve(null);
    }),
  );
  const changes = settled.filter((change): change is PendingChange =>
    Boolean(change),
  );
  if (changes.length < awaiting.length) {
    console.warn(
      `[writai/approvals] ${awaiting.length - changes.length} of ` +
        `${awaiting.length} pending changes could not be read — the queue ` +
        `shown is INCOMPLETE.`,
    );
  }
  if (changes.length === 0) {
    fellBack("no pending change could be read from the preview endpoint");
    return fixtureResult();
  }
  return {
    changes,
    source: "live",
    workspaceIds: awaiting.map((workspace) => workspace.id),
  };
}

async function fetchOneChange(
  workspace: ServerWorkspace,
  decisionId: string,
  signal?: AbortSignal,
): Promise<PendingChange | null> {
  const url =
    `${WORKSPACES_URL}/${encodeURIComponent(workspace.id)}` +
    `/decisions/${encodeURIComponent(decisionId)}/preview`;
  try {
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
      signal,
    });
    if (!response.ok) {
      console.warn(
        `[writai/approvals] preview for ${workspace.id} returned ${response.status}`,
      );
      return null;
    }
    const preview = unwrapPreview(await response.json());
    if (!preview) {
      console.warn(
        `[writai/approvals] preview for ${workspace.id} was unrecognised`,
      );
      return null;
    }
    if (!previewMatches(preview, workspace.id, decisionId)) {
      // A preview for a different change joined onto this workspace's
      // assignments would show one team's blast radius under another's decision.
      console.warn(
        `[writai/approvals] preview for ${workspace.id}/${decisionId} ` +
          `described a different change — discarded.`,
      );
      return null;
    }
    // Bind the change we are about to return, by identity. Only this object can
    // ever be approved with these credentials.
    const change = toPendingChange(workspace, preview);
    if (!change) {
      return null;
    }
    bindings.set(
      change,
      Object.freeze({
        workspaceId: workspace.id,
        decisionId,
        proposalFingerprint: preview.pending.proposal_fingerprint,
        proposalInstanceId: preview.pending.proposal_instance_id,
      }),
    );
    return change;
  } catch (error) {
    console.warn(
      `[writai/approvals] preview for ${workspace.id} failed (${describe(error)})`,
    );
    return null;
  }
}

/**
 * Approve posts when it can, and rehearses when it cannot.
 *
 * The real route takes an `ApprovalAttemptEnvelope` whose `approval_token`
 * resolves through Hexclave to a user id, and which must echo back the exact
 * proposal fingerprint and instance the approver was shown — the server rejects
 * an approval of a proposal that has since changed. Everything except the token
 * comes from the preview we already read.
 *
 * With no token this does NOT post. It returns the server's own preview
 * partition, marked `fixture`, and the screen renders it as a rehearsal.
 */
export async function approveChange(
  change: PendingChange,
): Promise<ApprovalResult> {
  const predicted: ApprovalReceipt = {
    changeId: change.id,
    interrupted: change.blastRadius.interrupted,
    preserved: change.blastRadius.preserved,
  };
  const rehearsal = (note: string): ApprovalResult => ({
    receipt: predicted,
    source: "fixture",
    outcome: "rehearsal",
    note,
  });
  /** The request may have landed. Say so; never claim nothing happened. */
  const unresolved = (note: string): ApprovalResult => ({
    receipt: predicted,
    source: "live",
    outcome: "indeterminate",
    note,
  });
  const applied = (receipt: ApprovalReceipt): ApprovalResult => ({
    receipt,
    source: "live",
    outcome: "applied",
  });

  // Identity, not id: a fixture card carrying the same composite id was never
  // bound, so it cannot borrow a live approval's credentials.
  const binding = bindings.get(change);
  const token = await approvalToken();
  if (!binding || !token) {
    console.warn(
      `[writai/approvals] rehearsing approval of ${change.id} — ` +
        `${binding ? NO_APPROVAL_CHANNEL : "this change was not read from a live service"}.`,
    );
    return rehearsal(
      binding ? NO_APPROVAL_CHANNEL : "this change came from the fixture",
    );
  }

  const url =
    `${WORKSPACES_URL}/${encodeURIComponent(binding.workspaceId)}` +
    `/decisions/${encodeURIComponent(binding.decisionId)}/approve`;
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        approval_token: token,
        channel: "workspace-ui",
        evidence_ref: `workspace-ui://approvals/${binding.workspaceId}/${binding.decisionId}`,
        confirmed_proposal_fingerprint: binding.proposalFingerprint,
        confirmed_proposal_instance_id: binding.proposalInstanceId,
      }),
    });
    if (!response.ok) {
      const reason = await failureReason(response);
      if (refusedOutright(response.status)) {
        // The server declined before applying anything: a bad token, a proposal
        // that moved, a malformed envelope. Nothing landed, and saying so is
        // accurate.
        console.warn(
          `[writai/approvals] approve of ${change.id} was refused (${reason})`,
        );
        return rehearsal(`the server refused the approval: ${reason}`);
      }
      // A 5xx (or a timeout) can mean the change applied and the response did
      // not survive. Go and look rather than guessing.
      console.warn(
        `[writai/approvals] approve of ${change.id} returned ${response.status} ` +
          `(${reason}) — reconciling against the workspace`,
      );
      return await reconcile(
        change,
        binding,
        `the server returned ${response.status}: ${reason}`,
        applied,
        unresolved,
      );
    }
    const body: unknown = await response.json();
    if (!isRecord(body) || typeof body.id !== "string") {
      return await reconcile(
        change,
        binding,
        "the server returned an unrecognised workspace",
        applied,
        unresolved,
      );
    }
    const partition = appliedPartition(
      body as unknown as ServerWorkspace,
      binding.decisionId,
    );
    if (!partition) {
      // The approval may well have landed; we simply cannot prove the partition
      // from this response, so re-read the workspace before saying anything.
      return await reconcile(
        change,
        binding,
        "the server did not report which sessions it interrupted",
        applied,
        unresolved,
      );
    }
    return applied({
      changeId: change.id,
      interrupted: partition.interrupted,
      preserved: partition.preserved,
    });
  } catch (error) {
    // The request was already in flight, so it may have been delivered and
    // applied. This is exactly the wifi-hiccup case: never report it as "no
    // approval was recorded" without checking.
    console.warn(
      `[writai/approvals] approve of ${change.id} failed (${describe(error)}) ` +
        `— reconciling against the workspace`,
    );
    return await reconcile(
      change,
      binding,
      `the approval request failed: ${describe(error)}`,
      applied,
      unresolved,
    );
  }
}

/** 4xx means the server decided; anything else may have applied and lost the answer. */
function refusedOutright(status: number): boolean {
  // 408 and 429 are 4xx but say "try again", not "declined".
  return status >= 400 && status < 500 && status !== 408 && status !== 429;
}

/**
 * Re-read the workspace to find out whether the approval actually landed.
 *
 * The server's own `supervisor.applied_interrupts` is the record of what it
 * did, so a single follow-up GET turns "we lost the answer" into a fact. If
 * that read ALSO fails we stay indeterminate — a failed read is never evidence
 * that nothing happened.
 */
async function reconcile(
  change: PendingChange,
  binding: ApprovalBinding,
  note: string,
  applied: (receipt: ApprovalReceipt) => ApprovalResult,
  unresolved: (note: string) => ApprovalResult,
): Promise<ApprovalResult> {
  try {
    const response = await fetch(
      `${WORKSPACES_URL}/${encodeURIComponent(binding.workspaceId)}`,
      { headers: { Accept: "application/json" } },
    );
    if (!response.ok) {
      return unresolved(`${note}; re-reading it returned ${response.status}`);
    }
    const body: unknown = await response.json();
    if (!isRecord(body) || typeof body.id !== "string") {
      return unresolved(`${note}; the workspace re-read was unrecognised`);
    }
    const partition = appliedPartition(
      body as unknown as ServerWorkspace,
      binding.decisionId,
    );
    if (!partition) {
      // The workspace read cleanly and records no partition for this decision.
      // That is real evidence, but not proof: the interrupt may not have been
      // recorded yet. Report what we saw without claiming it settles anything.
      return unresolved(
        `${note}; the workspace does not yet record this decision as applied`,
      );
    }
    return applied({
      changeId: change.id,
      interrupted: partition.interrupted,
      preserved: partition.preserved,
    });
  } catch (error) {
    return unresolved(`${note}; re-reading it failed (${describe(error)})`);
  }
}

async function failureReason(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (isRecord(body)) {
      const error = isRecord(body.error) ? body.error : body;
      const code = typeof error.code === "string" ? error.code : null;
      const message = typeof error.message === "string" ? error.message : null;
      if (code || message) {
        return [code, message].filter(Boolean).join(" · ");
      }
    }
  } catch {
    // fall through to the status
  }
  return `HTTP ${response.status}`;
}

/**
 * Live refresh over the workspace SSE streams rather than polling. Started only
 * once a live read has succeeded and only for the workspaces it returned.
 */
export function subscribeToPendingChanges(
  workspaceIds: string[],
  onChanged: () => void,
): () => void {
  if (typeof EventSource === "undefined" || workspaceIds.length === 0) {
    return () => {};
  }
  const streams: EventSource[] = [];
  const handle = (): void => onChanged();
  for (const id of workspaceIds) {
    try {
      const stream = new EventSource(
        `${WORKSPACES_URL}/${encodeURIComponent(id)}/events`,
      );
      stream.addEventListener("message", handle);
      streams.push(stream);
    } catch {
      // A stream we cannot open simply does not refresh; the screen still works.
    }
  }
  return () => {
    for (const stream of streams) {
      stream.removeEventListener("message", handle);
      stream.close();
    }
  };
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Accepts `{workspaces: [...]}` (LiveWorkspaceList) or a bare array. */
function readWorkspaces(body: unknown): ServerWorkspace[] | null {
  const raw = Array.isArray(body)
    ? body
    : isRecord(body) && Array.isArray(body.workspaces)
      ? body.workspaces
      : null;
  if (!raw) {
    return null;
  }
  const workspaces: ServerWorkspace[] = [];
  for (const candidate of raw) {
    if (!isRecord(candidate) || typeof candidate.id !== "string") {
      return null;
    }
    workspaces.push(candidate as unknown as ServerWorkspace);
  }
  return workspaces;
}
