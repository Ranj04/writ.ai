import type {
  ScenarioDefinition,
  ScenarioNarrativeStepId,
  ScenarioRunState,
} from "../model";
import { formatCategory, narrativeStepForRun } from "../utils";
import {
  EvidenceWorkspace,
  type EvidenceSection,
} from "./EvidenceWorkspace";
import { KnowledgeGraphView } from "./KnowledgeGraphView";
import { ScenarioNarrative } from "./ScenarioNarrative";
import { ScenarioNarrativeRail } from "./ScenarioNarrativeRail";
import {
  SCENARIO_LAYER_PANEL_IDS,
  ScenarioLayerNav,
  type ScenarioDetailLayer,
} from "./ScenarioLayerNav";

export interface ScenarioRunViewProps {
  scenario: ScenarioDefinition;
  run: ScenarioRunState | null;
  narrativeStep?: ScenarioNarrativeStepId;
  busy?: boolean;
  onBack: () => void;
  backLabel?: string;
  onReset: () => void;
  onOpenEvidence: () => void;
  detailLayer?: ScenarioDetailLayer;
  evidenceReturnLayer?: "story" | "graph";
  evidenceSection?: EvidenceSection;
  onDetailLayerChange?: (layer: ScenarioDetailLayer) => void;
  onShowEvidence?: (section: Exclude<EvidenceSection, null>) => void;
  primaryAction?: {
    label: string;
    onClick: () => void;
    disabled?: boolean;
    busy?: boolean;
  };
}

export function ScenarioRunView({
  scenario,
  run,
  narrativeStep: requestedNarrativeStep,
  busy = false,
  onBack,
  backLabel = "All scenarios",
  onReset,
  onOpenEvidence,
  detailLayer = "story",
  evidenceReturnLayer = "story",
  evidenceSection = null,
  onDetailLayerChange,
  primaryAction,
}: ScenarioRunViewProps) {
  const narrativeStep =
    requestedNarrativeStep ??
    narrativeStepForRun(
      run,
      run?.activeStage === "decision-changed",
    );
  const runDisplayStatus = !run
    ? "Not started"
    : run.status === "passed"
      ? "Complete"
      : formatCategory(run.status);

  return (
    <article className="sl-page sl-run-view" aria-labelledby="scenario-run-title">
      <header className="sl-run-heading sl-run-heading--narrative">
        <div>
          <button
            className="sl-back-link"
            type="button"
            onClick={onBack}
            disabled={busy}
          >
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <path d="m12.5 4.5-5.5 5.5 5.5 5.5" />
            </svg>
            {backLabel}
          </button>
          <h1 id="scenario-run-title" tabIndex={-1}>
            {scenario.name}
          </h1>
          <p>Watch an approved decision change move through active work.</p>
        </div>
        <div className="sl-run-context" aria-label="Scenario context">
          <span>{formatCategory(scenario.category)}</span>
          <span>{formatCategory(scenario.riskLevel)} risk</span>
          <span
            className={`sl-run-context__status sl-run-context__status--${run?.status ?? "not-run"}`}
          >
            {runDisplayStatus}
          </span>
        </div>
      </header>

      {detailLayer !== "evidence" ? (
        <ScenarioLayerNav
          activeLayer={detailLayer}
          onChange={(layer) => onDetailLayerChange?.(layer)}
          disabled={busy}
        />
      ) : null}

      {detailLayer === "story" ? (
        <div id={SCENARIO_LAYER_PANEL_IDS.story}>
          <ScenarioNarrativeRail
            activeStep={narrativeStep}
            runStatus={run?.status ?? "not-run"}
          />
          <ScenarioNarrative
            scenario={scenario}
            run={run}
            activeStep={narrativeStep}
            busy={busy}
            primaryAction={primaryAction}
            onOpenTechnicalEvidence={() =>
              onDetailLayerChange?.("evidence")
            }
          />
          <footer className="sl-narrative-footer">
            {run?.status === "running" ? (
              <button
                className="sl-button sl-button--quiet"
                type="button"
                onClick={onReset}
                disabled={busy}
              >
                Reset scenario
              </button>
            ) : null}
            <p>
              Scenario inputs are fixture-driven. Authority checks, graph
              traversal, selective invalidation, and executor verification are
              real.
            </p>
          </footer>
        </div>
      ) : detailLayer === "graph" ? (
        <KnowledgeGraphView
          scenario={scenario}
          run={run}
          activeStep={narrativeStep}
          busy={busy}
          primaryAction={primaryAction}
          onOpenTechnicalEvidence={() =>
            onDetailLayerChange?.("evidence")
          }
        />
      ) : (
        <section
          className="sl-technical-evidence"
          id={SCENARIO_LAYER_PANEL_IDS.evidence}
          aria-labelledby="technical-evidence-title"
        >
          <button
            className="sl-back-link sl-technical-evidence__back"
            type="button"
            onClick={() => onDetailLayerChange?.(evidenceReturnLayer)}
            disabled={busy}
          >
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <path d="m12.5 4.5-5.5 5.5 5.5 5.5" />
            </svg>
            {evidenceReturnLayer === "graph"
              ? "Back to impact map"
              : "Back to guided story"}
          </button>
          <div className="sl-technical-evidence__intro">
            <h2 id="technical-evidence-title">Technical evidence</h2>
            <p>
              Inspect the graph path, ordered service events, authorization
              checks, and evaluation behind the plain-language story.
            </p>
          </div>
          <EvidenceWorkspace
            scenario={scenario}
            run={run}
            openSection={evidenceSection}
            onOpenDrawer={onOpenEvidence}
          />
        </section>
      )}
    </article>
  );
}
