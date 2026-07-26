import type { ProvenanceNode } from "../model";

/**
 * One vertical rail, not a graph. The amber gradient travels down it once when
 * the card is fired; the branch the change never reached stays green.
 *
 * `affected` is decided by the server's traversal. This component reads the
 * flag; it does not intersect scopes.
 */
export function ProvenanceRail({
  path,
  label = "How it reaches the work",
}: {
  path: ProvenanceNode[];
  label?: string;
}) {
  return (
    <div className="ap-sect">
      <p className="ap-label">{label}</p>
      <div className="ap-chain">
        <div className="ap-rail">
          <i />
        </div>
        {path.map((node) => (
          <div
            key={node.id}
            className={node.affected ? "ap-node" : "ap-node ap-node--calm"}
          >
            <b>{node.title}</b>
            <span>{node.detail}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
