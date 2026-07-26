import { Fragment } from "react";
import type {
  ScenarioDefinition,
  ScenarioFiltersValue,
  ScenarioResultStatus,
} from "../model";
import {
  filterScenarios,
  formatCategory,
  formatResult,
} from "../utils";
import { ScenarioFilters } from "./ScenarioFilters";
import { StatusMark, type StatusTone } from "./StatusMark";

function resultTone(status: ScenarioResultStatus): StatusTone {
  if (status === "passed") return "positive";
  if (status === "failed") return "negative";
  if (status === "running") return "warning";
  return "neutral";
}

export interface ScenarioCatalogProps {
  scenarios: readonly ScenarioDefinition[];
  filters: ScenarioFiltersValue;
  onFiltersChange: (value: ScenarioFiltersValue) => void;
  selectedScenarioId?: string;
  onSelectedChange: (scenarioId: string) => void;
  onOpen: (scenarioId: string) => void;
  onRunAll?: () => void;
  runAllBusy?: boolean;
}

export function ScenarioCatalog({
  scenarios,
  filters,
  onFiltersChange,
  selectedScenarioId,
  onSelectedChange,
  onOpen,
  onRunAll,
  runAllBusy = false,
}: ScenarioCatalogProps) {
  const visibleScenarios = filterScenarios(scenarios, filters);
  const categories = Array.from(new Set(scenarios.map((scenario) => scenario.category)));

  return (
    <section className="sl-page sl-catalog" aria-labelledby="scenario-catalog-title">
      <div className="sl-page-heading">
        <div>
          <h1 id="scenario-catalog-title" tabIndex={-1}>
            Examples
          </h1>
          <p>
            Explore seeded requirement changes using the same deterministic
            authority flow as your workspace.
          </p>
        </div>
        {onRunAll ? (
          <button
            className="sl-button sl-button--primary"
            type="button"
            onClick={onRunAll}
            disabled={runAllBusy || scenarios.length === 0}
          >
            {runAllBusy ? "Running scenarios…" : "Run all scenarios"}
          </button>
        ) : null}
      </div>

      <div className="sl-catalog-layout">
        <aside className="sl-category-rail" aria-label="Scenario categories">
          <h2>Categories</h2>
          <button
            type="button"
            aria-pressed={filters.category === "all"}
            onClick={() => onFiltersChange({ ...filters, category: "all" })}
          >
            <span>All scenarios</span>
            <strong>{scenarios.length}</strong>
          </button>
          {categories.map((category) => {
            const count = scenarios.filter(
              (scenario) => scenario.category === category,
            ).length;
            return (
              <button
                type="button"
                aria-pressed={filters.category === category}
                onClick={() => onFiltersChange({ ...filters, category })}
                key={category}
              >
                <span>{formatCategory(category)}</span>
                <strong>{count}</strong>
              </button>
            );
          })}
        </aside>

        <div className="sl-catalog-main">
          <ScenarioFilters
            value={filters}
            onChange={onFiltersChange}
            showCategory={false}
          />

          {visibleScenarios.length > 0 ? (
            <div className="sl-table-wrap">
              <table className="sl-table sl-scenario-table">
            <caption className="sl-visually-hidden">
              Available writ.ai evaluation scenarios
            </caption>
            <thead>
              <tr>
                <th scope="col">Scenario</th>
                <th scope="col">Category</th>
                <th scope="col">Risk</th>
                <th scope="col">Last result</th>
                <th scope="col">
                  <span className="sl-visually-hidden">Open</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visibleScenarios.map((scenario) => {
                const result = scenario.lastResult ?? "not-run";
                const selected = scenario.id === selectedScenarioId;
                const preserved = scenario.expectedOutcomes.filter(
                  (outcome) => outcome.kind === "preserved",
                ).length;
                const stopped = scenario.expectedOutcomes.filter(
                  (outcome) => outcome.kind === "stopped",
                ).length;
                return (
                  <Fragment key={scenario.id}>
                    <tr className={selected ? "is-selected" : undefined}>
                      <th scope="row">
                        <button
                          className="sl-scenario-link"
                          type="button"
                          aria-expanded={selected}
                          onClick={() => onSelectedChange(scenario.id)}
                        >
                          <span>{scenario.name}</span>
                          <small>{scenario.description}</small>
                          <span
                            className="sl-scenario-mobile-meta"
                            aria-label={`${formatCategory(scenario.category)}, ${formatCategory(
                              scenario.riskLevel,
                            )} risk, ${formatResult(result)}`}
                          >
                            <span>{formatCategory(scenario.category)}</span>
                            <span
                              className={`sl-scenario-mobile-meta__risk sl-scenario-mobile-meta__risk--${scenario.riskLevel}`}
                            >
                              {formatCategory(scenario.riskLevel)} risk
                            </span>
                            <span
                              className={`sl-scenario-mobile-meta__result sl-scenario-mobile-meta__result--${result}`}
                            >
                              {formatResult(result)}
                            </span>
                          </span>
                        </button>
                      </th>
                      <td>{formatCategory(scenario.category)}</td>
                      <td>
                        <span
                          className={`sl-risk sl-risk--${scenario.riskLevel}`}
                        >
                          {formatCategory(scenario.riskLevel)}
                        </span>
                      </td>
                      <td>
                        <StatusMark
                          tone={resultTone(result)}
                          label={formatResult(result)}
                        />
                      </td>
                      <td className="sl-table__action">
                        <button
                          className="sl-icon-button"
                          type="button"
                          aria-expanded={selected}
                          onClick={() => onSelectedChange(scenario.id)}
                          aria-label={`${selected ? "Collapse" : "Preview"} ${scenario.name}`}
                        >
                          <svg
                            className={selected ? "is-expanded" : undefined}
                            viewBox="0 0 20 20"
                            aria-hidden="true"
                          >
                            <path d="m7.5 4.5 5.5 5.5-5.5 5.5" />
                          </svg>
                        </button>
                      </td>
                    </tr>
                    {selected ? (
                      <tr className="sl-scenario-detail-row">
                        <td colSpan={5}>
                          <div className="sl-scenario-detail">
                            <div>
                              <span>Approved change</span>
                              <p>{scenario.newDecision.text}</p>
                            </div>
                            <dl>
                              <div>
                                <dt>Preserve</dt>
                                <dd>{preserved}</dd>
                              </div>
                              <div>
                                <dt>Invalidate tasks</dt>
                                <dd>{stopped}</dd>
                              </div>
                            </dl>
                            <button
                              className="sl-button sl-button--primary"
                              type="button"
                              onClick={() => onOpen(scenario.id)}
                            >
                              Open scenario
                            </button>
                          </div>
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
              </table>
            </div>
          ) : (
            <div className="sl-empty-state" role="status">
              <h2>No scenarios match these filters.</h2>
              <p>Change a filter to return to the full evaluation set.</p>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
