import type { PlanView } from "../model";
import { formatCategory } from "../utils";

function Plan({
  plan,
  label,
}: {
  plan?: PlanView;
  label: string;
}) {
  return (
    <article className="sl-plan">
      <div className="sl-plan__heading">
        <span>{label}</span>
        {plan ? <strong>{plan.id}</strong> : null}
      </div>
      {plan ? (
        <>
          <p>{plan.objective}</p>
          <ol>
            {plan.steps.map((step, index) => (
              <li key={`${plan.id}-${index}`}>{step}</li>
            ))}
          </ol>
          <small>
            Source: {formatCategory(plan.source)}
            {plan.scope.length > 0 ? ` · Scope: ${plan.scope.join(", ")}` : ""}
          </small>
        </>
      ) : (
        <p className="sl-muted">No plan has been returned for this stage.</p>
      )}
    </article>
  );
}

export function PlanComparison({
  originalPlan,
  correctedPlan,
}: {
  originalPlan?: PlanView;
  correctedPlan?: PlanView;
}) {
  return (
    <section className="sl-plan-comparison" aria-labelledby="plan-comparison-title">
      <div className="sl-section-heading">
        <div>
          <h2 id="plan-comparison-title">The plan changes. Valid work remains.</h2>
          <p>
            Corrective wording may be fixture-driven; authorization still comes
            from the deterministic authority.
          </p>
        </div>
      </div>
      <div className="sl-plan-comparison__columns">
        <Plan plan={originalPlan} label="Original plan" />
        <Plan
          plan={correctedPlan}
          label={
            correctedPlan?.source === "fixture"
              ? "Fixture-generated corrective plan"
              : "Corrected plan"
          }
        />
      </div>
    </section>
  );
}
