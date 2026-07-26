import type {
  ProvenanceNode,
  ProvenancePath,
  ProvenanceNodeStatus,
} from "../model";
import { formatCategory } from "../utils";

interface GraphLayer {
  id: string;
  label: string;
  matches: (node: ProvenanceNode) => boolean;
}

const GRAPH_LAYERS: readonly GraphLayer[] = [
  {
    id: "decision",
    label: "Decisions",
    matches: (node) => node.kind.toLocaleLowerCase() === "decision",
  },
  {
    id: "specification",
    label: "Specification",
    matches: (node) => node.kind.toLocaleLowerCase() === "specification",
  },
  {
    id: "ticket",
    label: "Ticket",
    matches: (node) => node.kind.toLocaleLowerCase() === "ticket",
  },
  {
    id: "task",
    label: "Sibling tasks",
    matches: (node) => node.kind.toLocaleLowerCase() === "task",
  },
  {
    id: "plan",
    label: "Agent plans",
    matches: (node) =>
      ["agentplan", "agent plan", "plan"].includes(
        node.kind.toLocaleLowerCase(),
      ),
  },
  {
    id: "grant",
    label: "Authorizations",
    matches: (node) => node.kind.toLocaleLowerCase() === "grant",
  },
];

const STATUS_ORDER: Record<ProvenanceNodeStatus, number> = {
  changed: 0,
  superseded: 1,
  preserved: 2,
  valid: 3,
  "needs-review": 4,
  stopped: 5,
  rejected: 6,
  pending: 7,
  reauthorized: 8,
};

function sortNodes(nodes: readonly ProvenanceNode[]): ProvenanceNode[] {
  return [...nodes].sort(
    (left, right) =>
      STATUS_ORDER[left.status] - STATUS_ORDER[right.status] ||
      left.id.localeCompare(right.id),
  );
}

function layerNodes(path: ProvenancePath) {
  const assigned = new Set<string>();
  const layers = GRAPH_LAYERS.map((layer) => {
    const nodes = path.nodes.filter((node) => layer.matches(node));
    nodes.forEach((node) => assigned.add(node.id));
    return { ...layer, nodes: sortNodes(nodes) };
  }).filter((layer) => layer.nodes.length > 0);
  const otherNodes = path.nodes.filter((node) => !assigned.has(node.id));
  if (otherNodes.length > 0) {
    layers.push({
      id: "other",
      label: "Other artifacts",
      matches: () => false,
      nodes: sortNodes(otherNodes),
    });
  }
  return layers;
}

function GraphNode({ node }: { node: ProvenanceNode }) {
  return (
    <li>
      <article
        className={`sl-provenance-node sl-provenance-node--${node.status}`}
        aria-label={`${node.kind} ${node.id}: ${formatCategory(node.status)}`}
      >
        <span className="sl-provenance-node__kind">
          {formatCategory(node.kind)}
        </span>
        <strong>{node.id}</strong>
        <p>{node.title}</p>
        <span className="sl-provenance-node__status">
          {formatCategory(node.status)}
        </span>
        {node.synthetic ? (
          <small>Payload-derived display node</small>
        ) : null}
      </article>
    </li>
  );
}

export function ProvenanceChain({ path }: { path: ProvenancePath }) {
  if (path.nodes.length === 0) {
    return (
      <div className="sl-empty-state sl-empty-state--compact">
        <p>The authority has not returned graph artifacts yet.</p>
      </div>
    );
  }

  const layers = layerNodes(path);

  return (
    <div className="sl-provenance" aria-label="Layered impact graph">
      <div className="sl-provenance__canvas">
        {layers.map((layer, index) => (
          <div className="sl-provenance__layer-step" key={layer.id}>
            <section
              className={`sl-provenance__layer sl-provenance__layer--${layer.id}`}
              aria-labelledby={`provenance-layer-${layer.id}`}
            >
              <header>
                <h3 id={`provenance-layer-${layer.id}`}>{layer.label}</h3>
                <span>{layer.nodes.length}</span>
              </header>
              <ul>
                {layer.nodes.map((node) => (
                  <GraphNode node={node} key={node.id} />
                ))}
              </ul>
            </section>
            {index < layers.length - 1 ? (
              <div className="sl-provenance__flow" aria-hidden="true">
                <svg viewBox="0 0 36 18">
                  <path d="M1 9h32m-6-6 6 6-6 6" />
                </svg>
              </div>
            ) : null}
          </div>
        ))}
      </div>

      <section
        className="sl-relationships"
        aria-labelledby="typed-relationships-title"
      >
        <div className="sl-relationships__heading">
          <h3 id="typed-relationships-title">Typed relationships</h3>
          <span>{path.edges.length} edges</span>
        </div>
        <ul>
          {path.edges.map((edge, index) => (
            <li key={`${edge.sourceId}-${edge.relation}-${edge.targetId}-${index}`}>
              <code>{edge.sourceId}</code>
              <strong>{formatCategory(edge.relation)}</strong>
              <code>{edge.targetId}</code>
              {edge.synthetic ? <em>Display link</em> : null}
              {edge.evidenceRef ? (
                <small title={edge.evidenceRef}>{edge.evidenceRef}</small>
              ) : null}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
