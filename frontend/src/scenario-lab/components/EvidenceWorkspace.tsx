import { useEffect, useRef } from "react";
import type {
  ScenarioDefinition,
  ScenarioEvaluation,
  ScenarioRunState,
} from "../model";
import { formatCategory } from "../utils";
import { CheckMark } from "./StatusMark";
import { ExecutionTimeline } from "./ExecutionTimeline";
import { GrantTransition } from "./GrantTransition";
import { PlanComparison } from "./PlanComparison";
import { ProvenanceChain } from "./ProvenanceChain";

export type EvidenceSection = "graph" | "timeline" | "evaluation" | null;

export const EVIDENCE_DISCLOSURE_IDS = {
  graph: "scenario-evidence-graph",
  timeline: "scenario-evidence-timeline",
  evaluation: "scenario-evidence-evaluation",
} as const;

function EvaluationDetails({
  evaluation,
}: {
  evaluation: ScenarioEvaluation;
}) {
  return (
    <div
      className={`sl-evaluation sl-evaluation--${evaluation.status}`}
      aria-label="Scenario evaluation checks"
    >
      <div className="sl-evaluation__heading">
        <div>
          <span>Scenario evaluation</span>
          <h3>{formatCategory(evaluation.status)}</h3>
        </div>
        <strong>{evaluation.runtimeMs.toFixed(0)} ms</strong>
      </div>
      <ul>
        {evaluation.checks.map((check) => (
          <li key={check.id}>
            <CheckMark passed={check.passed} />
            <span>
              <strong>{check.label}</strong>
              <small>
                Expected {check.expected}; received {check.actual}
              </small>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function EvidenceWorkspace({
  scenario,
  run,
  openSection = null,
  onOpenDrawer,
}: {
  scenario: ScenarioDefinition;
  run: ScenarioRunState | null;
  openSection?: EvidenceSection;
  onOpenDrawer: () => void;
}) {
  const graphSummaryRef = useRef<HTMLElement>(null);
  const timelineSummaryRef = useRef<HTMLElement>(null);
  const evaluationSummaryRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!openSection) return;
    const target = {
      graph: graphSummaryRef.current,
      timeline: timelineSummaryRef.current,
      evaluation: evaluationSummaryRef.current,
    }[openSection];
    target?.focus();
  }, [openSection, run?.runId]);

  if (!run) {
    return (
      <section className="sl-evidence-workspace">
        <div className="sl-empty-state">
          <h2>No run evidence yet.</h2>
          <p>
            Start the scenario to record graph, authority, agent-loop, and
            executor evidence.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="sl-evidence-workspace" aria-labelledby="evidence-view-title">
      <div className="sl-evidence-workspace__heading">
        <div>
          <h2 id="evidence-view-title">Proof details</h2>
          <p>
            Backend-returned graph, authorization, event, and evaluation
            records for this run. This is session evidence, not a permanent
            evidence store.
          </p>
        </div>
        <span>Session-only history</span>
      </div>

      <details
        id={EVIDENCE_DISCLOSURE_IDS.graph}
        className="sl-disclosure"
        key={`${run.runId}-graph-${openSection === "graph"}`}
        open={openSection === "graph"}
      >
        <summary ref={graphSummaryRef}>
          <span>View complete graph</span>
          <small>
            {run.provenancePath.nodes.length} nodes ·{" "}
            {run.provenancePath.edges.length} edges
          </small>
        </summary>
        <div className="sl-disclosure__body">
          <ProvenanceChain path={run.provenancePath} />
        </div>
      </details>

      <details className="sl-disclosure">
        <summary>
          <span>Plans and authorization grants</span>
          <small>
            {run.correctedPlan
              ? "Fixture-generated corrective plan"
              : "Original plan"}
          </small>
        </summary>
        <div className="sl-disclosure__body">
          <PlanComparison
            originalPlan={run.originalPlan}
            correctedPlan={run.correctedPlan}
          />
          <GrantTransition
            originalGrant={run.originalGrant}
            replacementGrant={run.replacementGrant}
          />
        </div>
      </details>

      <details
        id={EVIDENCE_DISCLOSURE_IDS.timeline}
        className="sl-disclosure"
        key={`${run.runId}-timeline-${openSection === "timeline"}`}
        open={openSection === "timeline"}
      >
        <summary ref={timelineSummaryRef}>
          <span>Show execution timeline</span>
          <small>
            {run.events.length} ordered event{run.events.length === 1 ? "" : "s"} ·
            Agent {run.agentLoopState ?? "unknown"}
          </small>
        </summary>
        <div className="sl-disclosure__body">
          <ExecutionTimeline
            events={run.events}
            activeLoopState={run.agentLoopState}
          />
        </div>
      </details>

      {run.evaluation ? (
        <details
          id={EVIDENCE_DISCLOSURE_IDS.evaluation}
          className="sl-disclosure"
          key={`${run.runId}-evaluation-${openSection === "evaluation"}`}
          open={openSection === "evaluation" || run.status === "failed"}
        >
          <summary ref={evaluationSummaryRef}>
            <span>Scenario evaluation</span>
            <small>
              {run.evaluation.checks.filter((check) => check.passed).length}/
              {run.evaluation.checks.length} checks passed
            </small>
          </summary>
          <div className="sl-disclosure__body">
            <EvaluationDetails evaluation={run.evaluation} />
          </div>
        </details>
      ) : null}

      <div className="sl-evidence-workspace__raw">
        <div>
          <strong>Raw technical evidence</strong>
          <span>
            Inspect IDs, scopes, hashes, evidence references, and snapshot-bound
            metadata. Signed grant tokens are never exposed.
          </span>
        </div>
        <button
          className="sl-button sl-button--secondary"
          type="button"
          onClick={onOpenDrawer}
        >
          Inspect raw verification metadata
        </button>
      </div>
      <p className="sl-run-note">
        Corrective wording for {scenario.name} is fixture-generated; deterministic
        code still decides every verdict and executor result.
      </p>
    </section>
  );
}
