import type { WorkspaceEvent } from "../model";

export interface WorkspaceLiveUpdate {
  title: string;
  detail: string;
  tone?: "neutral" | "positive" | "negative";
}

const EVENT_TITLES: Record<string, string> = {
  "workspace.imported": "Workspace imported",
  "baseline.approved": "Baseline approved",
  "authorization.evaluated": "Plan authorization checked",
  "decision.proposed": "Decision proposal recorded",
  "decision.proposal-canceled": "Decision proposal canceled",
  "decision.approved": "Decision change approved",
  "initial-grant.verified": "Original authorization checked",
  "plan.updated": "Corrected plan saved",
  "plan.reauthorized": "Replacement authorization checked",
  "replacement-grant.verified": "New authorization checked",
};

function formatEventTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function eventTone(
  event: WorkspaceEvent,
): "neutral" | "positive" | "negative" {
  if (
    event.eventType === "initial-grant.verified" ||
    event.eventType === "replacement-grant.verified"
  ) {
    return event.data.applied === false ? "negative" : "positive";
  }
  if (
    event.eventType === "baseline.approved" ||
    event.eventType === "decision.approved"
  ) {
    return "positive";
  }
  if (
    (event.eventType === "authorization.evaluated" ||
      event.eventType === "plan.reauthorized") &&
    event.data.verdict === "ALLOW"
  ) {
    return "positive";
  }
  return "neutral";
}

function eventUpdate(event: WorkspaceEvent): WorkspaceLiveUpdate {
  let detail = event.detail;
  if (event.eventType === "decision.proposed") {
    detail =
      "The proposal is saved, but approved work has not changed.";
  } else if (event.eventType === "initial-grant.verified") {
    detail =
      event.data.applied === false
        ? "The old authorization is stale and cannot be used."
        : "The old authorization is still valid.";
  } else if (event.eventType === "plan.updated") {
    detail = "The corrected plan is saved and ready for review.";
  } else if (
    event.eventType === "plan.reauthorized" &&
    event.data.verdict === "ALLOW"
  ) {
    detail = "A new authorization was issued for the corrected plan.";
  } else if (event.eventType === "replacement-grant.verified") {
    detail =
      event.data.applied === true
        ? "The new authorization is valid. Work can continue."
        : "The new authorization was not accepted.";
  }
  return {
    title: EVENT_TITLES[event.eventType] ?? "Workspace updated",
    detail,
    tone: eventTone(event),
  };
}

export function WorkspaceActivity({
  events,
  busy,
  busyMessage,
  localUpdate,
}: {
  events: readonly WorkspaceEvent[];
  busy: boolean;
  busyMessage: string;
  localUpdate?: WorkspaceLiveUpdate | null;
}) {
  const latestEvent = events.at(-1);
  const current: WorkspaceLiveUpdate = busy
    ? {
        title: "Working on this step",
        detail: busyMessage,
        tone: "neutral",
      }
    : localUpdate ??
      (latestEvent
        ? eventUpdate(latestEvent)
        : {
            title: "Ready to begin",
            detail:
              "Upload a project document, screenshot, or structured workspace file, or use the starter example. Every run receives a fresh workspace ID automatically.",
            tone: "neutral",
          });
  const history = [...events].reverse();

  return (
    <aside className="lw-activity" aria-labelledby="workspace-activity-title">
      <div className="lw-activity__heading">
        <h2 id="workspace-activity-title">Live updates</h2>
        <span>{busy ? "Working" : "Up to date"}</span>
      </div>
      <div
        className={`lw-activity__current lw-activity__current--${current.tone ?? "neutral"}`}
        role="status"
        aria-live="polite"
      >
        <span className="lw-activity__pulse" aria-hidden="true" />
        <div>
          <strong>{current.title}</strong>
          <p>{current.detail}</p>
        </div>
      </div>

      {history.length > 0 ? (
        <details className="lw-activity__history">
          <summary>Activity history ({history.length})</summary>
          <ol>
            {history.map((event) => {
              const update = eventUpdate(event);
              return (
                <li key={`${event.sequence}-${event.eventType}`}>
                  <span
                    className={`lw-activity__mark lw-activity__mark--${update.tone ?? "neutral"}`}
                    aria-hidden="true"
                  />
                  <div>
                    <strong>{update.title}</strong>
                    <time dateTime={event.createdAt}>
                      {formatEventTime(event.createdAt)}
                    </time>
                    <p>{event.detail}</p>
                  </div>
                </li>
              );
            })}
          </ol>
        </details>
      ) : (
        <p className="lw-activity__empty">
          Updates appear here as Dragback validates, approves, and verifies
          your work.
        </p>
      )}
    </aside>
  );
}
