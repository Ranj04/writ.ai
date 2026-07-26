import type {
  LiveWorkspaceView,
  WorkspaceArtifact,
  WorkspaceExecution,
} from "../model";
import { CodeDocumentEditor } from "./CodeDocumentEditor";

const CALLWRIGHT_PROVIDER = "voyagr-callwright";

function taskOutcome(
  task: WorkspaceArtifact,
  invalidated: ReadonlySet<string>,
): "invalidated" | "preserved" {
  return invalidated.has(task.id) ? "invalidated" : "preserved";
}

function primaryPath(workspace: LiveWorkspaceView): readonly string[] {
  if (workspace.conflictAuthorization?.invalidationPath.length) {
    return workspace.conflictAuthorization.invalidationPath;
  }
  const paths = workspace.invalidationReport?.paths ?? [];
  return [...paths].sort(
    (left, right) =>
      left.node_ids.length - right.node_ids.length ||
      left.artifact_id.localeCompare(right.artifact_id),
  )[0]?.node_ids ?? [];
}

function taskCount(count: number): string {
  return `${count} ${count === 1 ? "task" : "tasks"}`;
}

interface CorrectedPlanPreview {
  objective: string;
  actions: string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function correctedPlanPreview(content: string): CorrectedPlanPreview | null {
  try {
    const parsed: unknown = JSON.parse(content);
    if (
      !isRecord(parsed) ||
      typeof parsed.objective !== "string" ||
      !parsed.objective.trim() ||
      !Array.isArray(parsed.actions)
    ) {
      return null;
    }
    const actions = parsed.actions
      .map((action) =>
        isRecord(action) && typeof action.description === "string"
          ? action.description.trim()
          : "",
      )
      .filter(Boolean);
    return actions.length > 0
      ? { objective: parsed.objective.trim(), actions }
      : null;
  } catch {
    return null;
  }
}

function usesCallwright(workspace: LiveWorkspaceView): boolean {
  return [workspace.initialPlan, workspace.currentPlan].some((plan) =>
    plan.actions.some(
      (action) => action.attributes.provider === CALLWRIGHT_PROVIDER,
    ),
  );
}

function CallwrightReceipt({
  execution,
}: {
  execution: WorkspaceExecution;
}) {
  const receipt = execution.callReceipt;
  if (!receipt) return null;
  const mode =
    execution.executionMode === "live"
      ? "Live"
      : execution.executionMode === "simulated"
        ? "Simulated"
        : "Mode unavailable";

  return (
    <section
      className="lw-callwright-receipt"
      aria-labelledby="callwright-receipt-title"
    >
      <div className="lw-callwright-receipt__heading">
        <div>
          <span>VOYAGR Callwright</span>
          <h4 id="callwright-receipt-title">Authorized call submitted</h4>
        </div>
        <strong>{mode}</strong>
      </div>
      <dl>
        <div>
          <dt>Call ID</dt>
          <dd>
            <code>{receipt.callId}</code>
          </dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>{receipt.status}</dd>
        </div>
        <div>
          <dt>Provider</dt>
          <dd>{receipt.provider}</dd>
        </div>
        <div>
          <dt>Evidence reference</dt>
          <dd>
            <code>{receipt.evidenceRef}</code>
          </dd>
        </div>
      </dl>
      <p>
        This redacted receipt contains execution metadata only. Phone numbers,
        credentials, transcripts, and signed grants remain server-side.
      </p>
    </section>
  );
}

export function WorkspaceImpact({
  workspace,
  planContent,
  busy,
  evidenceOpen,
  onPlanContentChange,
  onSaveAndReauthorize,
  onReauthorize,
  onVerifyReplacement,
  onDownloadReport,
  onToggleEvidence,
}: {
  workspace: LiveWorkspaceView;
  planContent: string;
  busy: boolean;
  evidenceOpen: boolean;
  onPlanContentChange: (content: string) => void;
  onSaveAndReauthorize: () => void;
  onReauthorize: () => void;
  onVerifyReplacement: () => void;
  onDownloadReport: () => void;
  onToggleEvidence: () => void;
}) {
  const report = workspace.invalidationReport;
  const invalidatedIds = new Set(
    report?.invalidated_task_ids ?? report?.stopped_work_artifact_ids ?? [],
  );
  const invalidatedCount = workspace.tasks.filter((task) =>
    invalidatedIds.has(task.id),
  ).length;
  const preservedCount = workspace.tasks.length - invalidatedCount;
  const decision =
    workspace.latestApprovedMutation?.decision ??
    workspace.pendingMutation?.decision;
  const oldVerification = workspace.initialVerification;
  const replacementVerification = workspace.replacementVerification;
  const callwrightPlan = usesCallwright(workspace);
  const complete = workspace.status === "complete";
  const planEditable = workspace.status === "initial-grant-rejected";
  const replacementFailed =
    workspace.status === "reauthorized" &&
    replacementVerification !== null &&
    replacementVerification !== undefined &&
    !replacementVerification.applied;
  const path = primaryPath(workspace);
  const planPreview = planEditable
    ? correctedPlanPreview(planContent)
    : null;

  return (
    <section
      className="lw-impact"
      aria-labelledby="workspace-outcome-title"
    >
      <div
        className={`lw-outcome-summary ${complete ? "lw-outcome-summary--complete" : ""}`}
      >
        <span className="lw-outcome-summary__mark" aria-hidden="true">
          {complete ? "✓" : "!"}
        </span>
        <div>
          <h2 id="workspace-outcome-title">
            {complete
              ? "The new authorization is valid."
              : "The original authorization is stale."}
          </h2>
          <p>
            {taskCount(invalidatedCount)} stopped.{" "}
            {taskCount(preservedCount)}{" "}
            {preservedCount === 1 ? "remains" : "remain"} valid.
          </p>
        </div>
      </div>

      <section
        className="lw-impact-decision"
        aria-labelledby="impact-decision-title"
      >
        <span>Approved decision</span>
        <h3 id="impact-decision-title">
          {decision?.title ?? "Approved decision changed"}
        </h3>
        <p>
          {decision?.text ??
            "The approved decision changed the active graph snapshot."}
        </p>
      </section>

      {callwrightPlan && oldVerification && !oldVerification.applied ? (
        <aside className="lw-callwright-stopped" aria-live="polite">
          <span>VOYAGR Callwright</span>
          <strong>Callwright not invoked</strong>
          <p>
            The executor rejected the stale authorization before any call
            request reached the provider.
          </p>
        </aside>
      ) : null}

      <section
        className="lw-task-outcomes"
        aria-labelledby="selective-impact-title"
      >
        <div className="lw-section-heading">
          <div>
            <h3 id="selective-impact-title">Work affected by the change</h3>
            <p>
              Dragback stops only tasks whose scopes conflict with the new
              decision.
            </p>
          </div>
        </div>
        <ul>
          {workspace.tasks.map((task) => {
            const outcome = taskOutcome(task, invalidatedIds);
            return (
              <li key={task.id} className={`lw-task-outcome lw-task-outcome--${outcome}`}>
                <span className="lw-task-outcome__mark" aria-hidden="true">
                  {outcome === "preserved" ? "✓" : "!"}
                </span>
                <div>
                  <strong>{task.title}</strong>
                  <span>
                    {outcome === "preserved" ? "Preserved" : "Stopped"}
                  </span>
                  <p>
                    {outcome === "preserved"
                      ? "Its scope did not change."
                      : "Its scope conflicts with the approved decision."}
                  </p>
                </div>
              </li>
            );
          })}
        </ul>
      </section>

      <section
        className="lw-correct-plan"
        aria-labelledby="correct-plan-title"
      >
        <div className="lw-section-heading">
          <div>
            <h3 id="correct-plan-title">
              {complete
                ? "Correction verified"
                : planEditable
                  ? "Update the stopped work"
                  : workspace.status === "plan-updated"
                    ? "Correction saved"
                    : replacementFailed
                      ? callwrightPlan
                        ? "Protected execution needs review"
                        : "Authorization check needs retry"
                      : "Verify the new authorization"}
            </h3>
            <p>
              {complete
                ? "The executor accepted the authorization for this exact corrected plan."
                : planEditable
                  ? "Dragback prepared a correction from the approved decision. Review it, then save it for a new authorization."
                  : workspace.status === "plan-updated"
                    ? "The saved plan is locked while the authority evaluates it."
                    : replacementFailed
                      ? callwrightPlan
                        ? "The last executor check did not apply. Recheck this same authorization; Dragback will not issue a new grant or replay an ambiguous call."
                        : "The last executor check did not apply. Retry this same authorization; Dragback will not issue a new grant."
                      : "The corrected plan has an authorization. One executor check remains."}
            </p>
          </div>
          <code>{workspace.currentPlan.id}</code>
        </div>

        {planEditable ? (
          <>
            {planPreview ? (
              <div className="lw-plan-preview">
                <span>Proposed corrected plan</span>
                <strong>{planPreview.objective}</strong>
                <ul>
                  {planPreview.actions.map((action, index) => (
                    <li key={`${action}-${index}`}>{action}</li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="lw-plan-preview-error" role="alert">
                The technical plan is not valid JSON yet. Open it below to
                correct the document.
              </p>
            )}
            <details
              className="lw-plan-technical-editor"
              {...(planPreview ? {} : { open: true })}
            >
              <summary>Edit technical plan JSON</summary>
              <CodeDocumentEditor
                id="corrected-plan-document"
                label="Corrected plan JSON"
                value={planContent}
                onChange={onPlanContentChange}
                disabled={busy}
                rows={14}
              />
            </details>
          </>
        ) : (
          <div className="lw-saved-plan">
            <div>
              <span>Objective</span>
              <strong>{workspace.currentPlan.objective}</strong>
            </div>
            <div>
              <span>Plan actions</span>
              <strong>{workspace.currentPlan.actions.length}</strong>
            </div>
            <div>
              <span>Graph snapshot</span>
              <strong>{workspace.graphVersion}</strong>
            </div>
          </div>
        )}

        {complete && replacementVerification ? (
          <CallwrightReceipt execution={replacementVerification} />
        ) : null}

        <div className="lw-correct-plan__actions">
          {workspace.status === "initial-grant-rejected" ? (
            <button
              className="sl-button sl-button--primary"
              type="button"
              disabled={busy}
              onClick={onSaveAndReauthorize}
            >
              {busy ? "Saving and authorizing…" : "Save and authorize correction"}
            </button>
          ) : workspace.status === "plan-updated" ? (
            <button
              className="sl-button sl-button--primary"
              type="button"
              disabled={busy}
              onClick={onReauthorize}
            >
              {busy ? "Authorizing saved plan…" : "Authorize saved plan"}
            </button>
          ) : workspace.status === "reauthorized" && replacementFailed ? (
            <button
              className="sl-button sl-button--primary"
              type="button"
              disabled={busy}
              onClick={onVerifyReplacement}
            >
              {busy
                ? callwrightPlan
                  ? "Rechecking protected execution…"
                  : "Rechecking authorization…"
                : callwrightPlan
                  ? "Recheck protected execution"
                  : "Retry same authorization"}
            </button>
          ) : workspace.status === "reauthorized" ? (
            <button
              className="sl-button sl-button--primary"
              type="button"
              disabled={busy}
              onClick={onVerifyReplacement}
            >
              {busy
                ? callwrightPlan
                  ? "Placing authorized call…"
                  : "Checking new authorization…"
                : callwrightPlan
                  ? "Place authorized call"
                  : "Verify new authorization"}
            </button>
          ) : (
            <button
              className="sl-button sl-button--primary"
              type="button"
              disabled={busy}
              onClick={onDownloadReport}
            >
              Download verification report
            </button>
          )}
          <button
            className="sl-button sl-button--secondary"
            id="workspace-evidence-toggle"
            type="button"
            aria-expanded={evidenceOpen}
            aria-controls="workspace-technical-evidence"
            onClick={onToggleEvidence}
          >
            {evidenceOpen ? "Hide technical evidence" : "View technical evidence"}
          </button>
        </div>
        <p className="lw-stage-note">
          Original authorization:{" "}
          <strong>
            {oldVerification
              ? `Rejected · ${oldVerification.verificationCode}`
              : "Verification pending"}
          </strong>
          . New authorization:{" "}
          <strong>
            {replacementVerification
              ? `${replacementVerification.applied ? "Applied" : "Rejected"} · ${replacementVerification.verificationCode}`
              : workspace.replacementAuthorization?.grant
                ? "Issued · verification pending"
                : "Not issued"}
          </strong>
          .
        </p>
      </section>

      {evidenceOpen ? (
        <section
          className="lw-technical-evidence"
          id="workspace-technical-evidence"
          aria-labelledby="workspace-technical-evidence-title"
          tabIndex={-1}
        >
          <div className="lw-technical-evidence__heading">
            <div>
              <h3 id="workspace-technical-evidence-title">
                Technical evidence
              </h3>
              <p>
                Deterministic provenance and authorization metadata. Raw grant
                tokens are never exposed.
              </p>
            </div>
            <button type="button" onClick={onToggleEvidence}>
              Close
            </button>
          </div>

          <div
            className="lw-shortest-path"
            role="region"
            aria-label="Shortest graph-derived path"
            tabIndex={0}
          >
            <h4>Shortest graph-derived path</h4>
            {path.length > 0 ? (
              <ol>
                {path.map((nodeId, index) => (
                  <li key={`${nodeId}-${index}`}>
                    <code>{nodeId}</code>
                    {index < path.length - 1 ? (
                      <svg viewBox="0 0 30 18" aria-hidden="true">
                        <path d="M2 9h24m-5-4 5 4-5 4" />
                      </svg>
                    ) : null}
                  </li>
                ))}
              </ol>
            ) : (
              <p>The authority did not return a provenance path.</p>
            )}
          </div>

          <dl>
            <div>
              <dt>Graph snapshot</dt>
              <dd>
                <code>{workspace.graphVersion}</code>
              </dd>
            </div>
            <div>
              <dt>Original authorization</dt>
              <dd>
                <code>
                  {workspace.initialAuthorization?.grant?.authorizationId ??
                    "Unavailable"}
                </code>
              </dd>
            </div>
            <div>
              <dt>Original plan hash</dt>
              <dd>
                <code>
                  {workspace.initialAuthorization?.grant?.planHash ??
                    "Unavailable"}
                </code>
              </dd>
            </div>
            <div>
              <dt>Evidence references</dt>
              <dd>
                {(workspace.conflictAuthorization?.evidenceRefs ?? []).join(
                  ", ",
                ) || "Unavailable"}
              </dd>
            </div>
          </dl>
          <p>Grant signatures and raw tokens are intentionally not exposed.</p>
        </section>
      ) : null}
    </section>
  );
}
