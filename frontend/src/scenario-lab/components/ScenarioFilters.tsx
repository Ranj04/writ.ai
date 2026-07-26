import type {
  RiskLevel,
  ScenarioCategory,
  ScenarioFiltersValue,
  ScenarioResultStatus,
} from "../model";
import { formatCategory } from "../utils";

const CATEGORIES: readonly ScenarioCategory[] = [
  "compliance",
  "security",
  "product",
  "infrastructure",
  "privacy",
  "finance",
  "access-control",
  "data-governance",
];

const RISK_LEVELS: readonly RiskLevel[] = ["low", "medium", "high", "critical"];

const RESULTS: readonly ScenarioResultStatus[] = [
  "not-run",
  "passed",
  "failed",
];

export function ScenarioFilters({
  value,
  onChange,
  showCategory = true,
}: {
  value: ScenarioFiltersValue;
  onChange: (next: ScenarioFiltersValue) => void;
  showCategory?: boolean;
}) {
  return (
    <fieldset className="sl-filters">
      <legend className="sl-visually-hidden">Filter scenarios</legend>

      <label className="sl-search-filter">
        <span className="sl-visually-hidden">Find a scenario</span>
        <svg viewBox="0 0 20 20" aria-hidden="true">
          <circle cx="8.5" cy="8.5" r="5.75" />
          <path d="m13 13 4 4" />
        </svg>
        <input
          type="search"
          placeholder="Find a scenario"
          value={value.query}
          onChange={(event) =>
            onChange({
              ...value,
              query: event.target.value,
            })
          }
        />
      </label>

      {showCategory ? (
        <label className="sl-filter">
          <span>Category</span>
          <select
            value={value.category}
            onChange={(event) =>
              onChange({
                ...value,
                category: event.target.value as ScenarioCategory | "all",
              })
            }
          >
            <option value="all">All categories</option>
            {CATEGORIES.map((category) => (
              <option value={category} key={category}>
                {formatCategory(category)}
              </option>
            ))}
          </select>
        </label>
      ) : null}

      <label className="sl-filter">
        <span>Risk</span>
        <select
          value={value.riskLevel}
          onChange={(event) =>
            onChange({
              ...value,
              riskLevel: event.target.value as RiskLevel | "all",
            })
          }
        >
          <option value="all">All risk levels</option>
          {RISK_LEVELS.map((risk) => (
            <option value={risk} key={risk}>
              {formatCategory(risk)}
            </option>
          ))}
        </select>
      </label>

      <label className="sl-filter">
        <span>Result</span>
        <select
          value={value.result}
          onChange={(event) =>
            onChange({
              ...value,
              result: event.target.value as ScenarioResultStatus | "all",
            })
          }
        >
          <option value="all">All results</option>
          {RESULTS.map((result) => (
            <option value={result} key={result}>
              {formatCategory(result)}
            </option>
          ))}
        </select>
      </label>
    </fieldset>
  );
}
