import type { DecisionDelta } from "../model";

/** The requirement delta, exactly as the server computed it. */
export function RequirementDelta({ decision }: { decision: DecisionDelta }) {
  return (
    <div className="ap-sect">
      <p className="ap-label">What changes</p>
      <div className="ap-delta">
        <div className="ap-delta-row ap-delta-row--was">
          <span className="ap-arrow">was</span>
          <code>
            {decision.scope} → {decision.was}
          </code>
        </div>
        <div className="ap-delta-row ap-delta-row--now">
          <span className="ap-arrow">now</span>
          <code>
            {decision.scope} → {decision.now}
          </code>
        </div>
      </div>
    </div>
  );
}
