import { Fragment } from "react";
import type { ScenarioRunSummary } from "../model";
import { formatCategory, summarizeRuns } from "../utils";
import { StatusMark } from "./StatusMark";

function runtime(value: number | null): string {
  return value === null ? "—" : `${value.toFixed(0)} ms`;
}

function percentage(value: number | null): string {
  if (value === null) return "N/A";
  return `${value.toFixed(value === 100 ? 0 : 1)}%`;
}

function oldGrantResult(
  run: ScenarioRunSummary,
): { label: string; detail: string } {
  const code = run.oldGrantVerificationCode;
  if (code) {
    return code === "VALID"
      ? { label: "Verified", detail: code }
      : { label: "Rejected", detail: code };
  }
  if (run.oldGrantRejected) {
    return { label: "Rejected", detail: "Verification code unavailable" };
  }
  if (run.status === "failed") {
    return { label: "Not reached", detail: "No executor verification" };
  }
  return { label: "Not rejected", detail: "Verification code unavailable" };
}

function replacementGrantResult(
  run: ScenarioRunSummary,
): { label: string; detail: string } {
  const code = run.replacementGrantVerificationCode;
  if (code) {
    return code === "VALID"
      ? { label: "Applied", detail: code }
      : { label: "Rejected", detail: code };
  }
  if (run.reauthorizationSucceeded) {
    return { label: "Applied", detail: "Verification code unavailable" };
  }
  if (run.replacementAuthorizationVerdict) {
    return {
      label: formatCategory(run.replacementAuthorizationVerdict),
      detail: "No valid replacement grant",
    };
  }
  if (run.status === "failed") {
    return { label: "Not reached", detail: "No replacement verification" };
  }
  return { label: "Not applied", detail: "No valid replacement" };
}

export interface RunReportProps {
  runs: readonly ScenarioRunSummary[];
  onInspect: (runId: string, scenarioId: string) => void;
  onRunAll: () => void;
  runAllBusy?: boolean;
  unavailableRunIds?: ReadonlySet<string>;
  progress?: {
    completed: number;
    total: number;
  } | null;
}

export function RunReport({
  runs,
  onInspect,
  onRunAll,
  runAllBusy = false,
  unavailableRunIds = new Set<string>(),
  progress,
}: RunReportProps) {
  const metrics = summarizeRuns(runs);

  return (
    <section
      className="sl-page sl-report"
      aria-labelledby="run-report-title"
      aria-busy={runAllBusy}
    >
      <div className="sl-page-heading">
        <div>
          <h1 id="run-report-title" tabIndex={-1}>
            Scenario run report
          </h1>
          <p>
            Actual authority outcomes compared with each scenario’s deterministic
            expectations.
          </p>
        </div>
        <button
          className="sl-button sl-button--primary"
          type="button"
          onClick={onRunAll}
          disabled={runAllBusy}
        >
          {runAllBusy ? "Running batch…" : "Run all scenarios"}
        </button>
      </div>

      <dl className="sl-report-metrics">
        <div>
          <dt>Scenarios passed</dt>
          <dd>
            {metrics.passed}/{metrics.completed}
          </dd>
        </div>
        <div>
          <dt>Invalidation recall</dt>
          <dd>{percentage(metrics.invalidationRecall)}</dd>
        </div>
        <div>
          <dt>Preservation recall</dt>
          <dd>{percentage(metrics.preservationRecall)}</dd>
        </div>
        <div>
          <dt>Old-grant rejection</dt>
          <dd>{percentage(metrics.grantRejectionRate)}</dd>
        </div>
        <div>
          <dt>Reauthorization</dt>
          <dd>{percentage(metrics.reauthorizationRate)}</dd>
        </div>
        <div>
          <dt>False positives</dt>
          <dd>{metrics.falsePositiveInvalidations}</dd>
        </div>
        <div>
          <dt>Average runtime</dt>
          <dd>{runtime(metrics.averageRuntimeMs)}</dd>
        </div>
      </dl>

      <div className="sl-report-session-note">
        <strong>Session-only history</strong>
        <span>
          Results are stored in the current agent-service process and clear when
          that service restarts.
        </span>
        {progress ? (
          <span role="status" aria-live="polite">
            Latest batch returned {progress.completed}/{progress.total} scenarios
          </span>
        ) : null}
      </div>

      {runs.length > 0 ? (
        <div
          className="sl-table-wrap sl-report-table-wrap"
          role="region"
          aria-label="Scenario run results. Scroll horizontally for all columns."
          tabIndex={0}
        >
          <table className="sl-table sl-report-table">
            <caption className="sl-visually-hidden">
              Scenario evaluation results
            </caption>
            <thead>
              <tr>
                <th scope="col">Scenario</th>
                <th scope="col">Result</th>
                <th scope="col">Preserved tasks</th>
                <th scope="col">Invalidated tasks</th>
                <th scope="col">Plan status</th>
                <th scope="col">Old grant</th>
                <th scope="col">Replacement grant</th>
                <th scope="col">Runtime</th>
                <th scope="col">
                  <span className="sl-visually-hidden">Inspect</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => {
                const canInspect =
                  run.inspectable && !unavailableRunIds.has(run.runId);
                const oldGrant = oldGrantResult(run);
                const replacementGrant = replacementGrantResult(run);
                return (
                  <Fragment key={run.runId}>
                    <tr>
                      <th scope="row">
                        <strong>{run.scenarioName}</strong>
                        <small>{formatCategory(run.category)}</small>
                      </th>
                      <td>
                        <StatusMark
                          tone={
                            run.status === "passed" ? "positive" : "negative"
                          }
                          label={formatCategory(run.status)}
                        />
                      </td>
                      <td>
                        <span className="sl-report-value">
                          <strong>{run.preservedActual}</strong>
                          <small>{run.preservedExpected} expected</small>
                        </span>
                      </td>
                      <td>
                        <span className="sl-report-value">
                          <strong>{run.stoppedActual}</strong>
                          <small>{run.stoppedExpected} expected</small>
                        </span>
                      </td>
                      <td>
                        <span className="sl-report-value">
                          <strong>{run.planStatus ?? "Not reported"}</strong>
                          {run.needsReviewArtifactIds?.length ? (
                            <small>
                              {run.needsReviewArtifactIds.join(", ")}
                            </small>
                          ) : null}
                        </span>
                      </td>
                      <td>
                        <span className="sl-report-value">
                          <strong>{oldGrant.label}</strong>
                          <small>{oldGrant.detail}</small>
                        </span>
                      </td>
                      <td>
                        <span className="sl-report-value">
                          <strong>{replacementGrant.label}</strong>
                          <small>{replacementGrant.detail}</small>
                        </span>
                      </td>
                      <td>{runtime(run.runtimeMs)}</td>
                      <td className="sl-table__action">
                        <button
                          className="sl-text-button"
                          type="button"
                          onClick={() => onInspect(run.runId, run.scenarioId)}
                          disabled={runAllBusy || !canInspect}
                          title={
                            canInspect
                              ? `Inspect run ${run.runId}`
                              : "Detailed run state is unavailable"
                          }
                        >
                          {canInspect ? "Inspect" : "Unavailable"}
                        </button>
                      </td>
                    </tr>
                    {run.failureReasons.length > 0 ? (
                      <tr className="sl-failure-detail-row">
                        <td colSpan={9}>
                          <details>
                            <summary>
                              Failure detail ({run.failureReasons.length})
                            </summary>
                            <ul>
                              {run.failureReasons.map((reason, index) => (
                                <li key={`${run.runId}-failure-${index}`}>
                                  {reason}
                                </li>
                              ))}
                            </ul>
                          </details>
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
        <div className="sl-empty-state">
          <h2>No evaluation runs yet.</h2>
          <p>Run the scenario set to produce measured results.</p>
        </div>
      )}
    </section>
  );
}
