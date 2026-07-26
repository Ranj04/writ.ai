import { ProvenanceRail } from "./components/ProvenanceRail";
import { RequirementDelta } from "./components/RequirementDelta";
import { SlackSource } from "./components/SlackSource";
import { SourceBadge } from "./components/SourceBadge";
import { WorkOutcome } from "./components/WorkOutcome";
import type { DataSource, PendingChange } from "./model";
import { headlineFor } from "./model";

/**
 * The developer view: "why did my agent change?"
 *
 * Same components, reordered for the person on the receiving end. It leads with
 * what survived and what has to be redone, weighted equally, then explains
 * itself backwards to the message someone posted. There is no approve control
 * here — the interrupted developer is not the approver.
 */
export function WhyView({
  change,
  dataSource,
}: {
  change: PendingChange;
  dataSource: DataSource;
}) {
  return (
    <div className="ap-page">
      <div className="ap-eyebrows">
        <span className="ap-eyebrow">
          <i className="ap-dot" />
          Why this change reaches your work
        </span>
        <SourceBadge source={dataSource} />
      </div>

      <h1>{headlineFor(change)}</h1>
      {/*
        This view renders a change that is still PENDING, so it describes what
        the change would do, not what happened. It has no session identity — the
        browser cannot know which of the affected assignments is yours — so it
        never says "your agent was told".
      */}
      <p className="ap-sub">
        Approved in #{change.source.channel} at {change.source.timestamp}, from{" "}
        {change.source.author}. Nobody edited your ticket — Dragback reaches the
        affected task through {change.decision.id}, and each affected agent is
        told at its next tool call.
      </p>

      <div className="ap-card">
        <div className="ap-sect">
          <p className="ap-label">What the affected agents are told</p>
          <div className="ap-why-lead">
            <b>
              {change.decision.scope} is now {change.decision.now}
            </b>
            <span>
              It was {change.decision.was} when you started. {change.decision.id}{" "}
              supersedes {change.decision.supersedes} for this scope only.
            </span>
          </div>
        </div>
        <WorkOutcome change={change} />
        <RequirementDelta decision={change.decision} />
        <ProvenanceRail
          path={change.provenancePath}
          label="How it reaches the affected task"
        />
        <SlackSource source={change.source} label="Who decided it" />
      </div>

      <p className="ap-viewswitch">
        The approver&rsquo;s view of the same change:{" "}
        <a href="/approvals">/approvals</a>
      </p>
    </div>
  );
}
