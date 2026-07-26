export type ScenarioDetailLayer = "story" | "graph" | "evidence";

export const SCENARIO_LAYER_CONTROL_IDS = {
  story: "scenario-story-control",
  graph: "scenario-graph-control",
  evidence: "scenario-evidence-control",
} as const;

export const SCENARIO_LAYER_PANEL_IDS = {
  story: "scenario-story-panel",
  graph: "scenario-graph-panel",
  evidence: "scenario-evidence-panel",
} as const;

export function ScenarioLayerNav({
  activeLayer,
  onChange,
  disabled = false,
}: {
  activeLayer: ScenarioDetailLayer;
  onChange: (layer: ScenarioDetailLayer) => void;
  disabled?: boolean;
}) {
  return (
    <nav className="sl-layer-nav" aria-label="Scenario detail">
      {(["story", "graph"] as const).map((layer) => (
        <button
          type="button"
          key={layer}
          id={SCENARIO_LAYER_CONTROL_IDS[layer]}
          aria-controls={SCENARIO_LAYER_PANEL_IDS[layer]}
          aria-current={activeLayer === layer ? "page" : undefined}
          onClick={() => onChange(layer)}
          disabled={disabled}
        >
          {layer === "story" ? "Guided story" : "Impact map"}
        </button>
      ))}
    </nav>
  );
}
