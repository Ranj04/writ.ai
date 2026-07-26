import type { LiveWorkspaceView } from "../model";

function ArtifactRow({
  label,
  id,
  title,
  detail,
}: {
  label: string;
  id: string;
  title: string;
  detail: string;
}) {
  return (
    <div className="lw-artifact-row">
      <span>{label}</span>
      <code>{id}</code>
      <div>
        <strong>{title}</strong>
        <p>{detail}</p>
      </div>
    </div>
  );
}

export function WorkspaceBaseline({
  workspace,
  approvalToken,
  busy,
  onApprovalTokenChange,
  onApprove,
}: {
  workspace: LiveWorkspaceView;
  approvalToken: string;
  busy: boolean;
  onApprovalTokenChange: (token: string) => void;
  onApprove: () => void;
}) {
  const roles = Array.from(
    new Set(Object.values(workspace.authorityPolicy).flat()),
  ).sort();
  return (
    <section className="lw-stage-content" aria-labelledby="baseline-review-title">
      <div className="lw-stage-content__main">
        <div className="lw-section-heading">
          <div>
            <h2 id="baseline-review-title">Confirm the before state</h2>
            <p>
              Review the existing evidence, unchanged ticket, assigned coding
              agent, and initial plan before approving the baseline.
            </p>
          </div>
          <span>{workspace.tasks.length} tasks</span>
        </div>
        <details className="lw-disclosure">
          <summary>Review all imported artifacts</summary>
          <div className="lw-artifact-list">
            <ArtifactRow
              label="Decision proposal"
              id={workspace.baselineDecision.id}
              title={workspace.baselineDecision.title}
              detail={workspace.baselineDecision.text}
            />
            <ArtifactRow
              label="Specification"
              id={workspace.specification.id}
              title={workspace.specification.title}
              detail={workspace.specification.text}
            />
            <ArtifactRow
              label="Ticket"
              id={workspace.ticket.id}
              title={workspace.ticket.title}
              detail={workspace.ticket.text}
            />
            {workspace.tasks.map((task) => (
              <ArtifactRow
                key={task.id}
                label="Task"
                id={task.id}
                title={task.title}
                detail={task.scopes.join(", ")}
              />
            ))}
          </div>
        </details>
      </div>

      <div className="lw-action-panel" aria-labelledby="baseline-action-title">
        <div>
          <h3 id="baseline-action-title">Approve this baseline</h3>
          <p>
            Authenticate with a Hexclave user API key. writ.ai derives the
            user and checks the required permission on the server.
          </p>
        </div>
        <div className="lw-action-panel__controls">
          <p>
            Required permission: <code>{roles.join(" or ")}</code>
          </p>
          <label htmlFor="baseline-approval-token">
            Hexclave user API key
          </label>
          <input
            id="baseline-approval-token"
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
            {busy ? "Approving baseline…" : "Approve baseline"}
          </button>
        </div>
      </div>
      <p className="lw-stage-note">
        Approval is required because the newest decision is not automatically
        authoritative.
      </p>
    </section>
  );
}
