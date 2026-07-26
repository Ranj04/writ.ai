import type { LiveWorkspaceView } from "../model";
import { CodeDocumentEditor } from "./CodeDocumentEditor";

interface DecisionPreview {
  id: string;
  title: string;
  text: string;
  affectedScopes: readonly string[];
}

function decisionPreview(content: string): DecisionPreview | null {
  try {
    const value = JSON.parse(content) as {
      decision?: {
        id?: unknown;
        title?: unknown;
        text?: unknown;
      };
      affected_scopes?: unknown;
    };
    if (
      typeof value.decision?.id !== "string" ||
      typeof value.decision.title !== "string" ||
      typeof value.decision.text !== "string"
    ) {
      return null;
    }
    return {
      id: value.decision.id,
      title: value.decision.title,
      text: value.decision.text,
      affectedScopes: Array.isArray(value.affected_scopes)
        ? value.affected_scopes.filter(
            (scope): scope is string => typeof scope === "string",
          )
        : [],
    };
  } catch {
    return null;
  }
}

export function WorkspaceChange({
  workspace,
  content,
  approvalToken,
  busy,
  onContentChange,
  onApprovalTokenChange,
  onPropose,
  onCancel,
  onApprove,
  onVerify,
}: {
  workspace: LiveWorkspaceView;
  content: string;
  approvalToken: string;
  busy: boolean;
  onContentChange: (content: string) => void;
  onApprovalTokenChange: (token: string) => void;
  onPropose: () => void;
  onCancel: () => void;
  onApprove: () => void;
  onVerify: () => void;
}) {
  const roles = Array.from(
    new Set(Object.values(workspace.authorityPolicy).flat()),
  ).sort();
  const pending = workspace.pendingMutation;
  const approved = workspace.latestApprovedMutation;
  const proposed = workspace.status === "change-proposed" && pending;
  const applied = workspace.status === "change-applied";
  const preview = decisionPreview(content);

  return (
    <section className="lw-stage-content" aria-labelledby="change-title">
      <div className="lw-stage-content__main">
        <div className="lw-section-heading">
          <div>
            <h2 id="change-title">
              {proposed
                ? "Review the decision proposal"
                : applied
                  ? "The approved decision changed"
                  : "Review the proposed decision change"}
            </h2>
            <p>
              {applied
                ? "The approved decision is now active. The original authorization still needs an independent check."
                : "A proposal cannot change the graph until an authoritative role approves it."}
            </p>
          </div>
          <span>{workspace.graphVersion}</span>
        </div>
        {proposed ? (
          <div className="lw-change-comparison">
            <article>
              <span>Approved baseline</span>
              <code>{workspace.baselineDecision.id}</code>
              <h3>{workspace.baselineDecision.title}</h3>
              <p>{workspace.baselineDecision.text}</p>
            </article>
            <svg viewBox="0 0 40 20" aria-hidden="true">
              <path d="M2 10h34m-7-6 7 6-7 6" />
            </svg>
            <article>
              <span>Decision proposal</span>
              <code>{pending.decision.id}</code>
              <h3>{pending.decision.title}</h3>
              <p>{pending.decision.text}</p>
            </article>
          </div>
        ) : applied ? (
          <div className="lw-decision-applied">
            <div>
              <span>Approved decision</span>
              <h3>
                {approved?.decision.title ??
                  "Approved decision created a new graph snapshot."}
              </h3>
              {approved?.decision.text ? (
                <p>{approved.decision.text}</p>
              ) : null}
              <p>
                The ticket was not edited. writ.ai found affected work through
                the provenance graph.
              </p>
            </div>
          </div>
        ) : (
          <>
            <article className="lw-change-preview">
              <span>Decision proposal</span>
              <code>{preview?.id ?? "Check JSON"}</code>
              <h3>{preview?.title ?? "The proposal needs a title"}</h3>
              <p>
                {preview?.text ??
                  "Open the JSON editor below to finish this proposal."}
              </p>
              {preview?.affectedScopes.length ? (
                <small>
                  Affected scope: {preview.affectedScopes.join(", ")}
                </small>
              ) : null}
            </article>
            <details className="lw-disclosure">
              <summary>Edit proposal JSON</summary>
              <p>
                Update the decision, superseded decision ID, and affected
                scopes before submitting.
              </p>
              <CodeDocumentEditor
                id="workspace-change-document"
                label="Decision proposal JSON"
                value={content}
                onChange={onContentChange}
                disabled={busy}
                rows={16}
              />
            </details>
          </>
        )}
      </div>

      <div className="lw-action-panel" aria-labelledby="change-action-title">
        <div>
          <h3 id="change-action-title">
            {proposed
              ? "Approve this change"
              : applied
                ? "Check the old authorization"
                : "Submit as a proposal"}
          </h3>
          <p>
            {proposed
              ? "writ.ai authenticates the user, then checks permission, scope containment, confidence, and requirement shape."
              : applied
                ? "The executor checks the stored authorization against the new decision version. Secret tokens stay on the server."
                : "Submitting records a proposal only. Active work remains authorized until approval."}
          </p>
        </div>
        <div className="lw-action-panel__controls">
        {proposed ? (
          <>
            <p>
              Required permission: <code>{roles.join(" or ")}</code>
            </p>
            <label htmlFor="change-approval-token">
              Hexclave user API key
            </label>
            <input
              id="change-approval-token"
              type="password"
              autoComplete="off"
              value={approvalToken}
              disabled={busy}
              onChange={(event) =>
                onApprovalTokenChange(event.target.value)
              }
            />
            <button
              className="sl-button sl-button--primary"
              type="button"
              disabled={busy || !approvalToken.trim()}
              onClick={onApprove}
            >
              {busy ? "Approving change…" : "Approve decision change"}
            </button>
            <button
              className="sl-button sl-button--quiet"
              type="button"
              disabled={busy}
              onClick={onCancel}
            >
              Cancel proposal
            </button>
          </>
        ) : applied ? (
          <button
            className="sl-button sl-button--primary"
            type="button"
            disabled={busy}
            onClick={onVerify}
            >
              {busy
                ? "Checking old authorization…"
                : "Check original authorization"}
            </button>
        ) : (
          <button
            className="sl-button sl-button--primary"
            type="button"
            disabled={busy}
            onClick={onPropose}
            >
              {busy ? "Submitting proposal…" : "Submit proposal"}
            </button>
        )}
        </div>
      </div>
    </section>
  );
}
