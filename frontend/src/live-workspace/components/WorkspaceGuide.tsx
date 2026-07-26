import type { WorkspaceGuide as WorkspaceGuideModel } from "../state";

export function WorkspaceGuide({
  guide,
  busy,
}: {
  guide: WorkspaceGuideModel;
  busy: boolean;
}) {
  return (
    <section
      className={`lw-guide lw-guide--${guide.tone}`}
      aria-labelledby="workspace-stage-title"
    >
      <div className="lw-guide__step">
        <span>
          Step {guide.step} of {guide.totalSteps} ·{" "}
          {busy
            ? "Dragback is working"
            : guide.tone === "complete"
              ? "Finished"
              : "Do this now"}
        </span>
        <span>{busy ? "Working" : guide.stateLabel}</span>
      </div>
      <div className="lw-guide__body">
        <div>
          <h2 id="workspace-stage-title" tabIndex={-1}>
            {guide.title}
          </h2>
          <p>{busy ? guide.busyMessage : guide.instruction}</p>
        </div>
        <div className="lw-guide__next">
          <strong>{busy ? "Please wait" : "What happens next"}</strong>
          <p>{busy ? "Keep this page open while Dragback finishes the current check." : guide.next}</p>
        </div>
      </div>
    </section>
  );
}
