import type {
  ScenarioDefinition,
  ScenarioEventView,
  ScenarioNarrativeStepId,
  ScenarioOutcome,
  ScenarioRunState,
} from "../model";

const STEP_INDEX: Record<ScenarioNarrativeStepId, number> = {
  before: 0,
  decision: 1,
  impact: 2,
  stopped: 3,
  corrected: 4,
};

const EVENT_STEP: Record<string, ScenarioNarrativeStepId> = {
  "authorization.issued": "before",
  "agent.work.started": "before",
  "decision.received": "decision",
  "graph.traversal.started": "impact",
  "graph.impact.identified": "impact",
  "graph.work.preserved": "impact",
  "graph.work.invalidated": "impact",
  "grant.invalidated": "stopped",
  "executor.rejected": "stopped",
  "agent.replan.required": "stopped",
  "agent.plan.corrected": "corrected",
  "plan.evaluated": "corrected",
  "authorization.reissued": "corrected",
  "executor.resumed": "corrected",
};

const EVENT_LABEL: Record<string, string> = {
  "authorization.issued": "Original plan authorized",
  "agent.work.started": "Coding agent started approved work",
  "decision.received": "New company decision approved",
  "graph.traversal.started": "Checking related active work",
  "graph.impact.identified": "Affected work discovered",
  "graph.work.preserved": "Safe work preserved",
  "graph.work.invalidated": "Conflicting work marked to stop",
  "grant.invalidated": "Original authorization became stale",
  "executor.rejected": "Executor stopped the old plan",
  "agent.replan.required": "Agent asked to correct the plan",
  "agent.plan.corrected": "Agent corrected the plan",
  "plan.evaluated": "Corrected plan checked",
  "authorization.reissued": "Fresh authorization issued",
  "executor.resumed": "Corrected work may continue",
  "scenario.failed": "Scenario stopped with an error",
};

const MILESTONE_EVENT_TYPES = new Set([
  "authorization.issued",
  "decision.received",
  "graph.impact.identified",
  "executor.rejected",
  "executor.resumed",
  "scenario.failed",
]);

function eventTone(
  eventType: string,
): "neutral" | "positive" | "negative" {
  if (
    eventType === "authorization.issued" ||
    eventType === "graph.work.preserved" ||
    eventType === "authorization.reissued" ||
    eventType === "executor.resumed"
  ) {
    return "positive";
  }
  if (
    eventType === "graph.work.invalidated" ||
    eventType === "grant.invalidated" ||
    eventType === "executor.rejected" ||
    eventType === "scenario.failed"
  ) {
    return "negative";
  }
  return "neutral";
}

function formatEventTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat(undefined, {
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
      }).format(date);
}

function eventNarrativeStep(
  event: ScenarioEventView,
  activeStep: ScenarioNarrativeStepId,
): ScenarioNarrativeStepId {
  if (event.eventType === "scenario.failed") return activeStep;
  return EVENT_STEP[event.eventType] ?? activeStep;
}

function visibleEvents(
  run: ScenarioRunState | null,
  activeStep: ScenarioNarrativeStepId,
): readonly ScenarioEventView[] {
  if (!run) return [];
  const activeIndex = STEP_INDEX[activeStep];
  return run.events.filter(
    (event) =>
      MILESTONE_EVENT_TYPES.has(event.eventType) &&
      STEP_INDEX[eventNarrativeStep(event, activeStep)] <= activeIndex,
  );
}

function OutcomeIcon({ kind }: { kind: ScenarioOutcome["kind"] }) {
  if (kind === "stopped") {
    return (
      <svg viewBox="0 0 20 20" aria-hidden="true">
        <path d="M6 6l8 8m0-8-8 8" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="m4.5 10.2 3.3 3.3 7.7-7.2" />
    </svg>
  );
}

function OutcomeRows({
  outcomes,
  includeNew = false,
}: {
  outcomes: readonly ScenarioOutcome[];
  includeNew?: boolean;
}) {
  const visible = outcomes.filter(
    (outcome) =>
      outcome.kind === "preserved" ||
      outcome.kind === "stopped" ||
      (includeNew && outcome.kind === "newly-required"),
  );
  if (visible.length === 0) return null;
  return (
    <ul className="sl-narrative-outcomes" aria-label="Work outcomes">
      {visible.map((outcome) => (
        <li
          className={`sl-narrative-outcome sl-narrative-outcome--${outcome.kind}`}
          key={`${outcome.kind}-${outcome.id}`}
        >
          <span className="sl-narrative-outcome__icon">
            <OutcomeIcon kind={outcome.kind} />
          </span>
          <span>
            <strong>
              {outcome.kind === "stopped"
                ? "Stopped"
                : outcome.kind === "preserved"
                  ? "Continues"
                  : "Changed"}
            </strong>
            <span>{outcome.label}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

function BeforeStory({
  scenario,
  run,
}: {
  scenario: ScenarioDefinition;
  run: ScenarioRunState | null;
}) {
  const focusTask =
    scenario.tasks.find((task) => task.expectedStatus === "invalidated") ??
    scenario.tasks[0];
  return (
    <>
      <div className="sl-narrative-comparison">
        <section>
          <span>Company intent</span>
          <h3>{scenario.originalDecision.text}</h3>
        </section>
        <section>
          <span>Agent work</span>
          <h3>{focusTask?.title ?? scenario.initialPlan.objective}</h3>
          <p
            className={
              run
                ? "sl-narrative-status sl-narrative-status--positive"
                : "sl-narrative-status"
            }
          >
            <span aria-hidden="true">{run ? "✓" : "○"}</span>
            {run ? "Authorized" : "Waiting to start"}
          </p>
        </section>
      </div>
      <p className="sl-narrative-explanation">
        {run
          ? "The original plan matches approved company intent. The authority has confirmed that this work may proceed."
          : "Start the scenario to ask the authority whether this plan follows from the company’s current approved decision."}
      </p>
    </>
  );
}

function DecisionStory({ scenario }: { scenario: ScenarioDefinition }) {
  return (
    <>
      <div className="sl-narrative-comparison">
        <section>
          <span>Before</span>
          <h3>{scenario.originalDecision.text}</h3>
        </section>
        <section>
          <span>New approved decision</span>
          <h3>{scenario.newDecision.text}</h3>
        </section>
      </div>
      <p className="sl-narrative-explanation">
        {scenario.newDecision.reason} The new decision does not directly name
        the downstream ticket, so Dragback checks the decision lineage next.
      </p>
    </>
  );
}

function lineageTitles(run: ScenarioRunState | null): readonly string[] {
  if (!run) return [];
  const nodesById = new Map(
    run.provenancePath.nodes.map((node) => [node.id, node]),
  );
  const primaryPath = run.outcomeSummary?.primaryProvenancePath ?? [];
  if (primaryPath.length > 0) {
    return primaryPath
      .map((nodeId) => nodesById.get(nodeId))
      .filter((node) => node && !node.synthetic)
      .map((node) => node!.title);
  }
  const seen = new Set<string>();
  return run.provenancePath.nodes
    .filter((node) => !node.synthetic)
    .map((node) => node.title)
    .filter((title) => {
      if (!title || seen.has(title)) return false;
      seen.add(title);
      return true;
    });
}

function ImpactStory({
  scenario,
  run,
}: {
  scenario: ScenarioDefinition;
  run: ScenarioRunState | null;
}) {
  const path = lineageTitles(run);
  return (
    <>
      <section className="sl-narrative-lineage" aria-labelledby="lineage-title">
        <span id="lineage-title">How Dragback found the work</span>
        <ol>
          {(path.length > 0
            ? path
            : [
                "Approved decision",
                scenario.specification.title,
                scenario.ticket.title,
                "Active task",
                "Agent plan",
              ]
          ).map((title, index) => (
            <li key={`${title}-${index}`}>
              <strong>{title}</strong>
              {index <
              (path.length > 0 ? path.length : 5) - 1 ? (
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <path d="m7 4 6 6-6 6" />
                </svg>
              ) : null}
            </li>
          ))}
        </ol>
      </section>
      <OutcomeRows outcomes={run?.outcomes ?? []} />
      <p className="sl-narrative-explanation">
        The approved decision never directly names the engineering ticket.
        Dragback reached the active work through its recorded lineage and
        changed scopes.
      </p>
    </>
  );
}

function StoppedStory({
  scenario,
  run,
}: {
  scenario: ScenarioDefinition;
  run: ScenarioRunState | null;
}) {
  const stoppedWork = run?.outcomes.find(
    (outcome) => outcome.kind === "stopped",
  );
  return (
    <>
      <div className="sl-narrative-comparison">
        <section>
          <span>Before</span>
          <h3>{scenario.originalDecision.text}</h3>
          <p>{stoppedWork?.label ?? scenario.initialPlan.objective}</p>
        </section>
        <section>
          <span>After the approved change</span>
          <h3>{scenario.newDecision.text}</h3>
          <p className="sl-narrative-status sl-narrative-status--negative">
            <span aria-hidden="true">×</span>
            Conflicting work stopped
          </p>
        </section>
      </div>
      <OutcomeRows outcomes={run?.outcomes ?? []} />
      <aside className="sl-narrative-callout sl-narrative-callout--negative">
        <strong>Original authorization rejected</strong>
        <p>
          The independent executor checked the old authorization against the
          current decision and refused to apply it.
        </p>
      </aside>
    </>
  );
}

function CorrectedStory({
  scenario,
  run,
}: {
  scenario: ScenarioDefinition;
  run: ScenarioRunState | null;
}) {
  const stoppedWork = run?.outcomes.find(
    (outcome) => outcome.kind === "stopped",
  );
  return (
    <>
      <div className="sl-narrative-comparison">
        <section>
          <span>Original plan</span>
          <h3>{stoppedWork?.label ?? scenario.initialPlan.objective}</h3>
          <p className="sl-narrative-status sl-narrative-status--negative">
            <span aria-hidden="true">×</span>
            No longer authorized
          </p>
        </section>
        <section>
          <span>Corrected plan</span>
          <h3>{scenario.expectedCorrectedBehavior}</h3>
          <p className="sl-narrative-status sl-narrative-status--positive">
            <span aria-hidden="true">✓</span>
            Fresh authorization accepted
          </p>
        </section>
      </div>
      <OutcomeRows outcomes={run?.outcomes ?? []} includeNew />
      <aside className="sl-narrative-callout sl-narrative-callout--positive">
        <strong>Safe work may continue</strong>
        <p>
          The corrected plan follows the new approved decision, received fresh
          authorization, and passed the executor’s independent check.
        </p>
      </aside>
    </>
  );
}

function storyCopy(
  activeStep: ScenarioNarrativeStepId,
  hasRun: boolean,
): {
  heading: string;
  instruction: string;
  next: string;
} {
  if (!hasRun) {
    return {
      heading: "Ready to begin",
      instruction:
        "Start with the company’s approved decision and the agent’s original plan.",
      next:
        "Dragback asks the authority whether the original plan is allowed under current company intent.",
    };
  }
  switch (activeStep) {
    case "before":
      return {
        heading: "Before the change",
        instruction:
          "Review what the company approved and what the coding agent is currently building.",
        next:
          "An authorized company decision changes. Dragback records it and begins checking related active work.",
      };
    case "decision":
      return {
        heading: "The company intent changed",
        instruction:
          "Compare the prior decision with the newly approved requirement.",
        next:
          "Reveal the exact active work Dragback found through the decision lineage.",
      };
    case "impact":
      return {
        heading: "Dragback found the affected work",
        instruction:
          "See which work conflicts with the change and which work remains safe.",
        next:
          "The independent executor checks whether the original authorization is still usable.",
      };
    case "stopped":
      return {
        heading: "Unsafe work is stopped",
        instruction:
          "The old authorization failed while unaffected work remained available.",
        next:
          "The agent keeps the safe work, corrects the conflict, and requests fresh authorization.",
      };
    case "corrected":
      return {
        heading: "Corrected work may continue",
        instruction:
          "The revised plan now follows the latest approved company decision.",
        next:
          "The scenario is complete. Run it again or open technical evidence for the graph, authorization checks, and event ledger.",
      };
  }
}

function LiveUpdates({
  run,
  activeStep,
  busy,
}: {
  run: ScenarioRunState | null;
  activeStep: ScenarioNarrativeStepId;
  busy: boolean;
}) {
  const events = visibleEvents(run, activeStep);
  return (
    <aside className="sl-live-updates" aria-labelledby="live-updates-title">
      <div className="sl-live-updates__heading">
        <h2 id="live-updates-title">Live updates</h2>
        <p>Confirmed by the running services</p>
      </div>
      {busy ? (
        <p className="sl-live-updates__working" role="status">
          <span aria-hidden="true" />
          Dragback is checking…
        </p>
      ) : null}
      {events.length > 0 ? (
        <ol aria-live="polite" aria-relevant="additions">
          {events.map((event, index) => {
            const tone = eventTone(event.eventType);
            return (
              <li
                className={[
                  `sl-live-update sl-live-update--${tone}`,
                  index === events.length - 1 ? "is-latest" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                key={`${event.sequence}-${event.eventType}`}
              >
                <span className="sl-live-update__dot" aria-hidden="true" />
                <div>
                  <time dateTime={event.createdAt}>
                    {formatEventTime(event.createdAt)}
                  </time>
                  <strong>
                    {EVENT_LABEL[event.eventType] ?? event.label}
                  </strong>
                </div>
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="sl-live-updates__empty">
          Start the scenario to see authorization, decision, impact, and
          executor updates as they are confirmed.
        </p>
      )}
    </aside>
  );
}

export interface ScenarioNarrativeProps {
  scenario: ScenarioDefinition;
  run: ScenarioRunState | null;
  activeStep: ScenarioNarrativeStepId;
  busy?: boolean;
  primaryAction?: {
    label: string;
    onClick: () => void;
    disabled?: boolean;
    busy?: boolean;
  };
  onOpenTechnicalEvidence: () => void;
}

export function ScenarioNarrative({
  scenario,
  run,
  activeStep,
  busy = false,
  primaryAction,
  onOpenTechnicalEvidence,
}: ScenarioNarrativeProps) {
  const copy = storyCopy(activeStep, Boolean(run));
  const stepNumber = STEP_INDEX[activeStep] + 1;

  return (
    <div className="sl-narrative-layout" aria-busy={busy}>
      <section className="sl-narrative-story">
        <div
          className="sl-narrative-story__heading"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          <span>
            {run ? `Step ${stepNumber} of 5` : "Guided scenario"}
          </span>
          <h2>{copy.heading}</h2>
          <p>{copy.instruction}</p>
        </div>

        <div className="sl-narrative-story__body">
          {activeStep === "before" ? (
            <BeforeStory scenario={scenario} run={run} />
          ) : activeStep === "decision" ? (
            <DecisionStory scenario={scenario} />
          ) : activeStep === "impact" ? (
            <ImpactStory scenario={scenario} run={run} />
          ) : activeStep === "stopped" ? (
            <StoppedStory scenario={scenario} run={run} />
          ) : (
            <CorrectedStory scenario={scenario} run={run} />
          )}
        </div>

        <section
          className="sl-narrative-next"
          aria-labelledby="narrative-next-title"
        >
          <h3 id="narrative-next-title">
            {activeStep === "corrected" ? "What this proved" : "What happens next"}
          </h3>
          <p>{copy.next}</p>
          {primaryAction ? (
            <button
              className="sl-button sl-button--primary"
              type="button"
              onClick={primaryAction.onClick}
              disabled={primaryAction.disabled}
            >
              {primaryAction.busy ? "Dragback is working…" : primaryAction.label}
            </button>
          ) : null}
        </section>

        {run ? (
          <button
            className="sl-narrative-evidence-link"
            type="button"
            onClick={onOpenTechnicalEvidence}
            disabled={busy}
          >
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <path d="m5 7.5 5 5 5-5" />
            </svg>
            Open technical evidence
          </button>
        ) : null}
      </section>

      <LiveUpdates run={run} activeStep={activeStep} busy={busy} />
    </div>
  );
}
