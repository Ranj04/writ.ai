import type { LiveWorkspaceStatus } from "../model";
import {
  WORKSPACE_STAGES,
  workspaceStageProgress,
} from "../state";

export function WorkspaceStageRail({
  status,
}: {
  status?: LiveWorkspaceStatus;
}) {
  return (
    <section
      className="lw-stage-rail"
      aria-labelledby="workspace-progress-title"
    >
      <h2 className="sl-visually-hidden" id="workspace-progress-title">
        Live Workspace progress
      </h2>
      <ol>
        {WORKSPACE_STAGES.map((stage, index) => {
          const progress = workspaceStageProgress(stage.id, status);
          return (
            <li
              key={stage.id}
              className={`lw-stage lw-stage--${progress}`}
              aria-current={
                progress === "current" || progress === "attention"
                  ? "step"
                  : undefined
              }
            >
              <span className="lw-stage__number" aria-hidden="true">
                {progress === "complete" ? (
                  <svg viewBox="0 0 18 18">
                    <path d="m4.2 9.4 3 3 6.6-6.4" />
                  </svg>
                ) : (
                  index + 1
                )}
              </span>
              <span className="lw-stage__copy">
                <strong>{stage.label}</strong>
                <span className="sl-visually-hidden">
                  {progress === "complete"
                    ? "Completed"
                    : progress === "attention"
                      ? "Needs attention"
                      : progress === "current"
                        ? "Current step"
                        : "Upcoming"}
                </span>
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
