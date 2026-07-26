import type { LiveWorkspaceView } from "../model";

export function WorkspaceAuthorization({
  workspace,
  busy,
  onAuthorize,
}: {
  workspace: LiveWorkspaceView;
  busy: boolean;
  onAuthorize: () => void;
}) {
  return (
    <section className="lw-stage-content" aria-labelledby="plan-review-title">
      <div className="lw-stage-content__main">
        <div className="lw-section-heading">
          <div>
            <h2 id="plan-review-title">Check the plan to be authorized</h2>
            <p>
              Every action must match the approved baseline before the
              authority can approve it.
            </p>
          </div>
          <code>{workspace.currentPlan.id}</code>
        </div>
        <div className="lw-plan-summary">
          <div>
            <span>Objective</span>
            <strong>{workspace.currentPlan.objective}</strong>
          </div>
          <ol>
            {workspace.currentPlan.actions.map((action, index) => (
              <li key={action.id}>
                <span>{index + 1}</span>
                <div>
                  <code>{action.id}</code>
                  <strong>{action.description}</strong>
                  <small>{action.scopes.join(", ")}</small>
                </div>
              </li>
            ))}
          </ol>
        </div>
      </div>

      <div className="lw-action-panel" aria-labelledby="authorize-action-title">
        <div>
          <h3 id="authorize-action-title">Request authorization</h3>
          <p>
            The result will be bound to {workspace.graphVersion}, ticket{" "}
            {workspace.currentPlan.ticketId}, and the exact plan hash.
          </p>
        </div>
        <div className="lw-action-panel__controls">
          <button
            className="sl-button sl-button--primary"
            type="button"
            disabled={busy}
            onClick={onAuthorize}
          >
            {busy ? "Authorizing plan…" : "Authorize this plan"}
          </button>
        </div>
      </div>
      <p className="lw-stage-note">
        The browser cannot approve its own plan. The authority service owns
        that decision.
      </p>
    </section>
  );
}
