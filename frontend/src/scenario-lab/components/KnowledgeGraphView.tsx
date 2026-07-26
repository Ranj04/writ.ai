import { useEffect, useMemo, useState } from "react";
import type {
  ProvenanceEdge,
  ProvenanceNode,
  ProvenanceNodeStatus,
  ScenarioDefinition,
  ScenarioNarrativeStepId,
  ScenarioRunState,
} from "../model";
import { formatCategory } from "../utils";

const NODE_WIDTH = 138;
const NODE_HEIGHT = 86;
const CANVAS_WIDTH = 870;
const MIN_CANVAS_HEIGHT = 540;

const STATUS_LABEL: Record<ProvenanceNodeStatus, string> = {
  valid: "Connected",
  preserved: "Continues",
  changed: "Approved change",
  superseded: "Replaced",
  "needs-review": "Needs review",
  stopped: "Stopped",
  rejected: "Rejected",
  pending: "Pending",
  reauthorized: "Reauthorized",
};

const STEP_INDEX: Record<ScenarioNarrativeStepId, number> = {
  before: 1,
  decision: 2,
  impact: 3,
  stopped: 4,
  corrected: 5,
};

const LAYER_LABELS = [
  "Decisions",
  "Specification",
  "Ticket",
  "Tasks",
  "Plan and authorization",
] as const;

function nodeLayer(node: ProvenanceNode): number {
  const kind = node.kind.toLocaleLowerCase();
  if (kind === "decision") return 0;
  if (kind === "specification") return 1;
  if (kind === "ticket") return 2;
  if (kind === "task") return 3;
  return 4;
}

function statusForStep(
  node: ProvenanceNode,
  activeStep: ScenarioNarrativeStepId,
): ProvenanceNodeStatus {
  if (activeStep !== "decision") return node.status;
  if (node.status === "changed" || node.status === "superseded") {
    return node.status;
  }
  if (node.status === "rejected" || node.status === "reauthorized") {
    return node.status;
  }
  return "valid";
}

function statusOrder(status: ProvenanceNodeStatus): number {
  return {
    changed: 0,
    superseded: 1,
    preserved: 2,
    valid: 3,
    "needs-review": 4,
    stopped: 5,
    rejected: 6,
    pending: 7,
    reauthorized: 8,
  }[status];
}

interface PositionedNode {
  node: ProvenanceNode;
  status: ProvenanceNodeStatus;
  x: number;
  y: number;
}

function positionNodes(
  nodes: readonly ProvenanceNode[],
  activeStep: ScenarioNarrativeStepId,
) {
  const layers = Array.from({ length: LAYER_LABELS.length }, () => [] as ProvenanceNode[]);
  nodes.forEach((node) => layers[nodeLayer(node)].push(node));
  layers.forEach((layer) =>
    layer.sort(
      (left, right) =>
        statusOrder(statusForStep(left, activeStep)) -
          statusOrder(statusForStep(right, activeStep)) ||
        left.id.localeCompare(right.id),
    ),
  );
  const maxLayerSize = Math.max(1, ...layers.map((layer) => layer.length));
  const height = Math.max(
    MIN_CANVAS_HEIGHT,
    maxLayerSize * NODE_HEIGHT + (maxLayerSize + 1) * 18,
  );
  const xStep = (CANVAS_WIDTH - NODE_WIDTH - 32) / (layers.length - 1);
  const positioned = new Map<string, PositionedNode>();
  layers.forEach((layer, layerIndex) => {
    const available = height - layer.length * NODE_HEIGHT;
    const gap = layer.length > 0 ? available / (layer.length + 1) : 0;
    layer.forEach((node, nodeIndex) => {
      positioned.set(node.id, {
        node,
        status: statusForStep(node, activeStep),
        x: 16 + layerIndex * xStep,
        y: gap + nodeIndex * (NODE_HEIGHT + gap),
      });
    });
  });
  return { height, positioned };
}

function friendlyRelation(value: string): string {
  return (
    {
      SUPERSEDES: "Replaces",
      AMENDS: "Updates",
      CONTRADICTS: "Conflicts with",
      BASIS_FOR: "Defines",
      CREATES: "Creates",
      DECOMPOSES_TO: "Contains",
      CURRENTLY_DRIVES: "Guides",
      IMPLEMENTS: "Implements",
      SUPPORTED_BY: "Supported by",
      PLAN_ACTION_FOR: "Plans work for",
      PLAN_FOR: "Plans",
      ISSUED_FOR: "Authorizes",
    }[value] ?? formatCategory(value)
  );
}

function nodeExplanation(
  node: ProvenanceNode,
  status: ProvenanceNodeStatus,
): string {
  if (status === "changed") {
    return "This is the newly approved decision that changed company intent.";
  }
  if (status === "superseded") {
    return "This earlier decision remains visible for provenance, but it is no longer authoritative.";
  }
  if (status === "preserved") {
    return "This work is connected to the decision, but its scopes do not conflict with the changed rule.";
  }
  if (status === "stopped") {
    return "This work overlaps the changed scope and can no longer continue under the old plan.";
  }
  if (status === "needs-review") {
    return "This artifact contains affected work and must be checked before execution continues.";
  }
  if (status === "rejected") {
    return "The independent executor rejected this old authorization against the current decision graph.";
  }
  if (status === "reauthorized") {
    return node.kind.toLocaleLowerCase() === "grant"
      ? "This runtime authorization matches the corrected plan and the current decision graph."
      : "This corrected plan matches the current decision graph and received fresh authorization.";
  }
  if (node.kind.toLocaleLowerCase() === "grant") {
    return "This runtime authorization binds the plan to a specific decision snapshot.";
  }
  return "This artifact is part of the recorded lineage that connects company intent to active work.";
}

function defaultSelectedNode(
  scenario: ScenarioDefinition,
  run: ScenarioRunState,
  activeStep: ScenarioNarrativeStepId,
): string {
  const nodes = run.provenancePath.nodes;
  if (activeStep === "corrected") {
    return (
      nodes.find((node) => node.status === "reauthorized")?.id ??
      nodes.find(
        (node) =>
          node.synthetic &&
          node.kind.toLocaleLowerCase().includes("plan") &&
          node.id === run.correctedPlan?.id,
      )?.id ??
      scenario.newDecision.id
    );
  }
  if (activeStep === "stopped") {
    return (
      nodes.find((node) => node.status === "rejected")?.id ??
      nodes.find((node) => node.status === "stopped")?.id ??
      scenario.newDecision.id
    );
  }
  if (activeStep === "impact") {
    return (
      nodes.find(
        (node) =>
          node.status === "stopped" &&
          node.kind.toLocaleLowerCase() === "task",
      )?.id ?? scenario.newDecision.id
    );
  }
  if (activeStep === "decision") return scenario.newDecision.id;
  return scenario.originalDecision.id;
}

function edgePath(source: PositionedNode, target: PositionedNode): string {
  if (Math.abs(source.x - target.x) < 2) {
    const sourceCenterX = source.x + NODE_WIDTH / 2;
    const sourceBottom = source.y + NODE_HEIGHT;
    const targetTop = target.y;
    const loopX = source.x + NODE_WIDTH + 28;
    return `M ${sourceCenterX} ${sourceBottom} C ${loopX} ${sourceBottom + 20}, ${loopX} ${targetTop - 20}, ${sourceCenterX} ${targetTop}`;
  }
  const sourceX = source.x + NODE_WIDTH;
  const sourceY = source.y + NODE_HEIGHT / 2;
  const targetX = target.x;
  const targetY = target.y + NODE_HEIGHT / 2;
  const middleX = (sourceX + targetX) / 2;
  return `M ${sourceX} ${sourceY} C ${middleX} ${sourceY}, ${middleX} ${targetY}, ${targetX} ${targetY}`;
}

function edgeLabelPoint(
  source: PositionedNode,
  target: PositionedNode,
): { x: number; y: number } {
  if (Math.abs(source.x - target.x) < 2) {
    return {
      x: source.x + NODE_WIDTH + 28,
      y: (source.y + target.y + NODE_HEIGHT) / 2,
    };
  }
  return {
    x: (source.x + NODE_WIDTH + target.x) / 2,
    y: (source.y + target.y + NODE_HEIGHT) / 2 - 7,
  };
}

function pathEdgeKeys(path: readonly string[]): Set<string> {
  const keys = new Set<string>();
  for (let index = 0; index < path.length - 1; index += 1) {
    keys.add(`${path[index]}:${path[index + 1]}`);
  }
  return keys;
}

function statusSymbol(status: ProvenanceNodeStatus): string {
  if (status === "stopped" || status === "rejected") return "×";
  if (status === "needs-review" || status === "pending") return "!";
  if (status === "superseded") return "↺";
  return "✓";
}

function graphSummary(
  run: ScenarioRunState,
  activeStep: ScenarioNarrativeStepId,
): string {
  const actualOutcomes = run.outcomes.filter(
    (outcome) => outcome.basis === "actual",
  );
  const preserved = actualOutcomes.filter(
    (outcome) => outcome.kind === "preserved",
  ).length;
  const stopped = actualOutcomes.filter(
    (outcome) => outcome.kind === "stopped",
  ).length;
  const pathLength =
    run.outcomeSummary?.primaryProvenancePath.filter((id) =>
      run.provenancePath.nodes.some(
        (node) => node.id === id && !node.synthetic,
      ),
    ).length ?? 0;
  if (activeStep === "corrected") {
    return `1 fresh authorization accepted · ${preserved} continue · ${stopped} stopped`;
  }
  if (activeStep === "stopped") {
    return `1 stale authorization rejected · ${preserved} continue · ${stopped} stopped`;
  }
  if (activeStep === "impact") {
    return `${pathLength} linked artifacts · ${preserved} continue · ${stopped} stopped`;
  }
  const artifactCount = run.provenancePath.nodes.filter(
    (node) => !node.synthetic,
  ).length;
  const relationshipCount = run.provenancePath.edges.filter(
    (edge) => !edge.synthetic,
  ).length;
  return `${artifactCount} graph artifacts · ${relationshipCount} typed relationships`;
}

function nextCopy(activeStep: ScenarioNarrativeStepId): string {
  return {
    before:
      "Approve the new decision and watch the graph move from the baseline to the current state.",
    decision:
      "Reveal which connected work conflicts with the changed scope and which work remains safe.",
    impact:
      "Ask the independent executor whether the original authorization is still usable.",
    stopped:
      "Correct the plan and request a fresh authorization against the current decision graph.",
    corrected:
      "The old path was stopped and the corrected plan is now bound to the current graph.",
  }[activeStep];
}

function NodeInspector({
  node,
  status,
  path,
  primaryAction,
  busy,
  activeStep,
  onOpenTechnicalEvidence,
}: {
  node: ProvenanceNode;
  status: ProvenanceNodeStatus;
  path: ScenarioRunState["provenancePath"];
  primaryAction?: {
    label: string;
    onClick: () => void;
    disabled?: boolean;
    busy?: boolean;
  };
  busy: boolean;
  activeStep: ScenarioNarrativeStepId;
  onOpenTechnicalEvidence: () => void;
}) {
  const nodesById = new Map(path.nodes.map((item) => [item.id, item]));
  const incoming = path.edges.filter((edge) => edge.targetId === node.id);
  const outgoing = path.edges.filter((edge) => edge.sourceId === node.id);
  const connectedEdges = [...incoming, ...outgoing];
  const evidenceRefs = Array.from(
    new Set(
      connectedEdges.flatMap((edge) =>
        edge.evidenceRef ? [edge.evidenceRef] : [],
      ),
    ),
  );

  const relationshipRows = (
    edges: readonly ProvenanceEdge[],
    direction: "incoming" | "outgoing",
  ) =>
    edges.slice(0, 5).map((edge, index) => {
      const otherId =
        direction === "incoming" ? edge.sourceId : edge.targetId;
      const other = nodesById.get(otherId);
      return (
        <li key={`${edge.sourceId}-${edge.relation}-${edge.targetId}-${index}`}>
          <strong>{friendlyRelation(edge.relation)}</strong>
          <span>{other?.title ?? otherId}</span>
        </li>
      );
    });

  return (
    <aside className="sl-graph-inspector" aria-live="polite">
      <div className={`sl-graph-inspector__status sl-graph-tone--${status}`}>
        <span aria-hidden="true">{statusSymbol(status)}</span>
        {formatCategory(node.kind)}
      </div>
      <h3>{node.title}</h3>
      <p className="sl-graph-inspector__explanation">
        {nodeExplanation(node, status)}
      </p>

      <dl className="sl-graph-inspector__facts">
        <div>
          <dt>Type</dt>
          <dd>{formatCategory(node.kind)}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd className={`sl-graph-tone--${status}`}>
            {STATUS_LABEL[status]}
          </dd>
        </div>
        <div>
          <dt>Scope</dt>
          <dd>
            {node.scopes && node.scopes.length > 0
              ? node.scopes.join(", ")
              : "Not scope-bound"}
          </dd>
        </div>
        <div>
          <dt>Source</dt>
          <dd>{node.synthetic ? "Runtime overlay" : "Graph artifact"}</dd>
        </div>
      </dl>

      <section className="sl-graph-inspector__relationships">
        <h4>Incoming relationships</h4>
        {incoming.length > 0 ? (
          <ul>{relationshipRows(incoming, "incoming")}</ul>
        ) : (
          <p>Starting point in this view.</p>
        )}
      </section>

      <section className="sl-graph-inspector__relationships">
        <h4>Outgoing relationships</h4>
        {outgoing.length > 0 ? (
          <ul>{relationshipRows(outgoing, "outgoing")}</ul>
        ) : (
          <p>No downstream relationship in this view.</p>
        )}
      </section>

      <details className="sl-graph-inspector__technical">
        <summary>Technical fields</summary>
        <dl>
          <div>
            <dt>Node ID</dt>
            <dd>
              <code>{node.id}</code>
            </dd>
          </div>
          {node.invalidatedScopes && node.invalidatedScopes.length > 0 ? (
            <div>
              <dt>Invalidated scopes</dt>
              <dd>{node.invalidatedScopes.join(", ")}</dd>
            </div>
          ) : null}
          {evidenceRefs.length > 0 ? (
            <div>
              <dt>Source reference</dt>
              <dd>
                <code>{evidenceRefs[0]}</code>
              </dd>
            </div>
          ) : null}
        </dl>
      </details>

      <section className="sl-graph-inspector__next">
        <h4>
          {activeStep === "corrected"
            ? "What this proved"
            : "Watch the graph change"}
        </h4>
        <p>{nextCopy(activeStep)}</p>
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
        <button
          className="sl-graph-inspector__proof"
          type="button"
          onClick={onOpenTechnicalEvidence}
          disabled={busy}
        >
          View technical proof
        </button>
      </section>
    </aside>
  );
}

export interface KnowledgeGraphViewProps {
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

export function KnowledgeGraphView({
  scenario,
  run,
  activeStep,
  busy = false,
  primaryAction,
  onOpenTechnicalEvidence,
}: KnowledgeGraphViewProps) {
  const preferredSelection = run
    ? defaultSelectedNode(scenario, run, activeStep)
    : "";
  const [selectedNodeId, setSelectedNodeId] = useState(preferredSelection);

  useEffect(() => {
    setSelectedNodeId(preferredSelection);
  }, [preferredSelection, run?.runId]);

  const layout = useMemo(
    () =>
      run
        ? positionNodes(run.provenancePath.nodes, activeStep)
        : { height: MIN_CANVAS_HEIGHT, positioned: new Map<string, PositionedNode>() },
    [activeStep, run],
  );

  if (!run) {
    return (
      <section
        className="sl-knowledge-graph sl-knowledge-graph--empty"
        id="scenario-graph-panel"
        aria-labelledby="knowledge-graph-title"
      >
        <div className="sl-knowledge-graph__heading">
          <div>
            <h2 id="knowledge-graph-title">Decision knowledge graph</h2>
            <p>
              Start the scenario to load the backend-returned artifacts and
              typed relationships for this run.
            </p>
          </div>
          <span>{scenario.originalDecision.graphSnapshot}</span>
        </div>
        <div className="sl-knowledge-graph__empty">
          <p>
            The graph will begin with approved company intent connected to its
            specification, ticket, tasks, and active agent plan.
          </p>
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
        </div>
        <p className="sl-knowledge-graph__storage">
          Examples use isolated in-memory graphs. The same graph-store
          contract supports Neo4j for persistent deployments.
        </p>
      </section>
    );
  }

  const selected =
    layout.positioned.get(selectedNodeId) ??
    layout.positioned.get(preferredSelection) ??
    layout.positioned.values().next().value;
  if (!selected) return null;

  const primaryPath =
    activeStep === "impact" ||
    activeStep === "stopped" ||
    activeStep === "corrected"
      ? run.outcomeSummary?.primaryProvenancePath ?? []
      : [];
  const primaryEdges = pathEdgeKeys(primaryPath);
  const connectedToSelected = new Set(
    run.provenancePath.edges.flatMap((edge) =>
      edge.sourceId === selected.node.id || edge.targetId === selected.node.id
        ? [`${edge.sourceId}:${edge.targetId}`]
      : [],
    ),
  );
  const focusSelectedEdges = connectedToSelected.size <= 4;
  const persistedNodes = run.provenancePath.nodes.filter(
    (node) => !node.synthetic,
  );
  const beforeSnapshot = scenario.originalDecision.graphSnapshot;
  const graphVersionCopy =
    run.graphSnapshot === beforeSnapshot
      ? run.graphSnapshot
      : `${beforeSnapshot} → ${run.graphSnapshot}`;

  return (
    <section
      className="sl-knowledge-graph"
      id="scenario-graph-panel"
      aria-labelledby="knowledge-graph-title"
      aria-busy={busy}
    >
      <div className="sl-knowledge-graph__heading">
        <div>
          <span>Step {STEP_INDEX[activeStep]} of 5</span>
          <h2 id="knowledge-graph-title">Decision knowledge graph</h2>
          <p>See how one approved decision reached active work.</p>
        </div>
        <div className="sl-knowledge-graph__summary">
          <strong>{graphVersionCopy}</strong>
          <span>{graphSummary(run, activeStep)}</span>
        </div>
      </div>

      <div className="sl-knowledge-graph__layout">
        <div className="sl-knowledge-graph__visual">
          <div
            className="sl-knowledge-graph__viewport"
            aria-label={`Decision lineage with ${persistedNodes.length} graph artifacts`}
          >
            <div className="sl-knowledge-graph__scroll-content">
              <div className="sl-knowledge-graph__layer-labels" aria-hidden="true">
                {LAYER_LABELS.map((label) => (
                  <span key={label}>{label}</span>
                ))}
              </div>
              <div
                className="sl-knowledge-graph__canvas"
                style={{ width: CANVAS_WIDTH, height: layout.height }}
              >
              <svg
                viewBox={`0 0 ${CANVAS_WIDTH} ${layout.height}`}
                role="img"
                aria-label="Typed relationships between decisions, delivery work, plans, and authorizations"
              >
                <defs>
                  <marker
                    id="sl-graph-arrow"
                    markerWidth="7"
                    markerHeight="7"
                    refX="6"
                    refY="3.5"
                    orient="auto"
                  >
                    <path d="M0 0 7 3.5 0 7Z" />
                  </marker>
                </defs>
                {run.provenancePath.edges.map((edge, index) => {
                  const source = layout.positioned.get(edge.sourceId);
                  const target = layout.positioned.get(edge.targetId);
                  if (!source || !target) return null;
                  const key = `${edge.sourceId}:${edge.targetId}`;
                  const labelPoint = edgeLabelPoint(source, target);
                  const isPrimary = primaryEdges.has(key);
                  const isConnected =
                    focusSelectedEdges && connectedToSelected.has(key);
                  return (
                    <g
                      className={[
                        "sl-knowledge-edge",
                        isPrimary ? "is-primary" : "",
                        isConnected ? "is-selected" : "",
                        edge.synthetic ? "is-runtime" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      key={`${key}:${edge.relation}:${index}`}
                    >
                      <path
                        d={edgePath(source, target)}
                        markerEnd="url(#sl-graph-arrow)"
                      />
                      {activeStep !== "corrected" &&
                      (isPrimary || isConnected) ? (
                        <text x={labelPoint.x} y={labelPoint.y}>
                          {friendlyRelation(edge.relation)}
                        </text>
                      ) : null}
                    </g>
                  );
                })}
              </svg>

                {Array.from(layout.positioned.values()).map((positioned) => {
                  const { node, status, x, y } = positioned;
                  const isPrimary = primaryPath.includes(node.id);
                  return (
                    <button
                      className={[
                        "sl-knowledge-node",
                        `sl-knowledge-node--${status}`,
                        node.synthetic ? "is-runtime" : "",
                        isPrimary ? "is-primary" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      style={{ left: x, top: y, width: NODE_WIDTH, height: NODE_HEIGHT }}
                      type="button"
                      key={node.id}
                      aria-pressed={selected.node.id === node.id}
                      aria-label={`${formatCategory(node.kind)} ${node.title}, ${STATUS_LABEL[status]}`}
                      onClick={() => setSelectedNodeId(node.id)}
                    >
                      <span className="sl-knowledge-node__kind">
                        <span aria-hidden="true">{statusSymbol(status)}</span>
                        {formatCategory(node.kind)}
                      </span>
                      <strong>{node.title}</strong>
                      <small>{STATUS_LABEL[status]}</small>
                    </button>
                  );
                })}
              </div>
            </div>
          </div>

          <div className="sl-knowledge-graph__legend" aria-label="Graph status legend">
            <span className="sl-graph-tone--changed">Changed</span>
            <span className="sl-graph-tone--preserved">Continues</span>
            <span className="sl-graph-tone--stopped">Stopped</span>
            <span className="sl-graph-tone--reauthorized">Reauthorized</span>
            <span className="is-runtime">Dashed = runtime overlay</span>
          </div>
        </div>

        <NodeInspector
          node={selected.node}
          status={selected.status}
          path={run.provenancePath}
          primaryAction={primaryAction}
          busy={busy}
          activeStep={activeStep}
          onOpenTechnicalEvidence={onOpenTechnicalEvidence}
        />
      </div>

      <p className="sl-knowledge-graph__storage">
        Rendered from backend-returned graph artifacts and exact typed
        relationships. Examples use isolated in-memory graphs; the same
        graph-store contract supports Neo4j for persistent deployments.
      </p>
    </section>
  );
}
