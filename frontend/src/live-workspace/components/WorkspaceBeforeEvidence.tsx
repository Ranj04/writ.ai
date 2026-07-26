import type { LiveWorkspaceView } from "../model";

function textAttribute(
  attributes: Record<string, unknown>,
  key: string,
): string | null {
  const value = attributes[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function assignedAgentFor(workspace: LiveWorkspaceView): string {
  return (
    textAttribute(workspace.ticket.attributes, "assigned_agent") ??
    textAttribute(workspace.ticket.attributes, "assignee") ??
    "Coding Agent (demo default)"
  );
}

function agentInitials(name: string): string {
  const initials = name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("");
  return initials || "CA";
}

function baselineState(workspace: LiveWorkspaceView): {
  label: string;
  snapshot: string;
} {
  const authorization = workspace.initialAuthorization;
  if (authorization?.verdict === "ALLOW") {
    return {
      label: "Initial plan authorized",
      snapshot:
        authorization.grant?.decisionSnapshot ??
        authorization.graphVersion,
    };
  }
  if (workspace.baselineApproved) {
    return {
      label: "Baseline approved · plan pending",
      snapshot: workspace.graphVersion,
    };
  }
  return {
    label: "Imported evidence · review required",
    snapshot: workspace.graphVersion,
  };
}

export function WorkspaceBeforeEvidence({
  workspace,
  titleId,
}: {
  workspace: LiveWorkspaceView;
  titleId: string;
}) {
  const agentName = assignedAgentFor(workspace);
  const state = baselineState(workspace);

  return (
    <section className="lw-before-evidence" aria-labelledby={titleId}>
      <header className="lw-before-evidence__heading">
        <div>
          <span>Before the new decision</span>
          <h2 id={titleId}>Existing evidence, ticket, and agent plan</h2>
          <p>
            This is the exact baseline Dragback compares with a later approved
            change. The ticket remains unchanged.
          </p>
        </div>
        <div className="lw-before-evidence__state">
          <strong>{state.label}</strong>
          <code>{state.snapshot}</code>
        </div>
      </header>

      <div className="lw-before-evidence__sources">
        <article>
          <span>Stored baseline evidence</span>
          <blockquote>{workspace.baselineDecision.text}</blockquote>
          <code>
            {workspace.baselineDecision.sourceRef ??
              "No source reference supplied"}
          </code>
        </article>
        <article>
          <span>Unchanged ticket</span>
          <code>{workspace.ticket.id}</code>
          <h4>{workspace.ticket.title}</h4>
          <p>{workspace.ticket.text || workspace.ticket.title}</p>
        </article>
        <article className="lw-before-evidence__agent">
          <span>Registered agent profile</span>
          <div>
            <b aria-hidden="true">{agentInitials(agentName)}</b>
            <div>
              <strong>{agentName}</strong>
              <small>Agent runtime · receives supervised assignments</small>
            </div>
          </div>
          <p>
            Dragback creates scoped subagent runs from this plan. The runtime
            can propose work, but it cannot authorize itself.
          </p>
        </article>
      </div>

      <div className="lw-before-evidence__plan">
        <div>
          <span>Stored initial plan</span>
          <code>{workspace.initialPlan.id}</code>
          <strong>{workspace.initialPlan.objective}</strong>
        </div>
        <ol>
          {workspace.initialPlan.actions.map((action, index) => (
            <li key={action.id}>
              <span>{index + 1}</span>
              <div>
                <strong>{action.description}</strong>
                <small>{action.scopes.join(", ")}</small>
              </div>
            </li>
          ))}
        </ol>
      </div>

      <p className="lw-before-evidence__note">
        A new decision is checked against this stored plan. If its approved
        scope reaches the plan through the graph, Dragback requires the
        assigned agent to replan.
      </p>
    </section>
  );
}
