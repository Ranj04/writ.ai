import type {
  ScenarioNarrativeStepId,
  ScenarioResultStatus,
} from "../model";
import {
  narrativeProgress,
  SCENARIO_NARRATIVE_STEPS,
} from "../utils";

export function ScenarioNarrativeRail({
  activeStep,
  runStatus = "not-run",
}: {
  activeStep: ScenarioNarrativeStepId;
  runStatus?: ScenarioResultStatus;
}) {
  return (
    <nav className="sl-narrative-rail" aria-label="Scenario story progress">
      <ol>
        {SCENARIO_NARRATIVE_STEPS.map((step, index) => {
          const progress = narrativeProgress(
            step.id,
            activeStep,
            runStatus,
          );
          const failed = progress === "failed";
          return (
            <li
              className={`sl-narrative-step sl-narrative-step--${progress}`}
              key={step.id}
              aria-current={
                progress === "current" || failed ? "step" : undefined
              }
              aria-label={failed ? `${step.label}: failed` : undefined}
            >
              <span className="sl-narrative-step__number" aria-hidden="true">
                {progress === "complete" ? (
                  <svg viewBox="0 0 18 18">
                    <path d="m4.2 9.4 3 3 6.6-6.4" />
                  </svg>
                ) : failed ? (
                  <svg viewBox="0 0 18 18">
                    <path d="m5 5 8 8m0-8-8 8" />
                  </svg>
                ) : (
                  index + 1
                )}
              </span>
              <strong>{step.label}</strong>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
