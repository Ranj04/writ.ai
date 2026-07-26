import type { GrantView } from "../model";
import { formatCategory } from "../utils";

function GrantSummary({
  grant,
  label,
}: {
  grant?: GrantView;
  label: string;
}) {
  if (!grant) {
    return (
      <div className="sl-grant-summary sl-grant-summary--empty">
        <span>{label}</span>
        <p>Not issued</p>
      </div>
    );
  }

  return (
    <div className={`sl-grant-summary sl-grant-summary--${grant.status}`}>
      <span>{label}</span>
      <div>
        <strong>{grant.graphSnapshot}</strong>
        <em>{formatCategory(grant.status)}</em>
      </div>
      <p>
        {grant.planId}
        {grant.scope.length > 0 ? ` · ${grant.scope.join(", ")}` : ""}
      </p>
      {grant.verificationCode ? (
        <small className="sl-grant-summary__verification">
          Executor verification: {grant.verificationCode}
        </small>
      ) : null}
    </div>
  );
}

export function GrantTransition({
  originalGrant,
  replacementGrant,
}: {
  originalGrant?: GrantView;
  replacementGrant?: GrantView;
}) {
  return (
    <section className="sl-grant-transition" aria-label="Authorization transition">
      <GrantSummary grant={originalGrant} label="Old grant" />
      <div className="sl-grant-transition__arrow" aria-hidden="true">
        <svg viewBox="0 0 34 20">
          <path d="M1 10h30m-6-6 6 6-6 6" />
        </svg>
      </div>
      <GrantSummary grant={replacementGrant} label="New grant" />
    </section>
  );
}
