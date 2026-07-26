import type { PendingChange } from "../model";
import { headlineFor, totalSessions } from "../model";

/**
 * Rendered only when more than one change is waiting. One pending change needs
 * no queue, and an empty list of tabs above a single card is noise.
 */
export function PendingQueue({
  changes,
  selectedId,
  onSelect,
}: {
  changes: PendingChange[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  if (changes.length < 2) {
    return null;
  }
  // Plain buttons with aria-pressed rather than role="tab": complete tab
  // semantics need a tabpanel, aria-controls and roving focus, and a half-built
  // tablist reads worse to a screen reader than an honest toggle group.
  return (
    <div className="ap-queue" role="group" aria-label="Pending changes">
      {changes.map((change) => (
        <button
          key={change.id}
          type="button"
          className="ap-queue-item"
          aria-pressed={change.id === selectedId}
          onClick={() => onSelect(change.id)}
        >
          <b>{headlineFor(change)}</b>
          <span>
            {change.decision.id} · {change.blastRadius.interrupted.length} of{" "}
            {totalSessions(change.blastRadius)} sessions
          </span>
        </button>
      ))}
    </div>
  );
}
