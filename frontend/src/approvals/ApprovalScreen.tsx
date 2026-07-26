import { useEffect, useState } from "react";
import type { ApprovalOutcome } from "./api";
import { ApprovalAction } from "./components/ApprovalAction";
import type { ApprovalPhase } from "./components/ApprovalAction";
import { BlastRadius } from "./components/BlastRadius";
import { PendingQueue } from "./components/PendingQueue";
import { ProvenanceRail } from "./components/ProvenanceRail";
import { RequirementDelta } from "./components/RequirementDelta";
import { SlackSource } from "./components/SlackSource";
import { SourceBadge } from "./components/SourceBadge";
import type {
  ApprovalReceipt,
  DataSource,
  PendingChange,
} from "./model";
import { headlineFor, inWords, totalSessions } from "./model";

/** The pause between the rail firing and the number settling, from the mock. */
const SETTLE_MS = 900;

/**
 * Only definitive outcomes may settle into applied/no-op copy. Keeping this
 * guard inside the scheduled callback boundary makes it impossible for an
 * indeterminate approval to become a rehearsal merely because 900ms elapsed.
 */
export function scheduleApprovalSettlement(
  outcome: ApprovalOutcome,
  onSettled: () => void,
): () => void {
  if (outcome === "indeterminate") {
    return () => {};
  }
  const timer = setTimeout(onSettled, SETTLE_MS);
  return () => clearTimeout(timer);
}

export function ApprovalScreen({
  change,
  changes,
  dataSource,
  phase,
  receipt,
  receiptSource,
  receiptOutcome,
  receiptNote,
  onSelect,
  onApprove,
  onReject,
  onReplay,
}: {
  change: PendingChange;
  changes: PendingChange[];
  dataSource: DataSource;
  phase: ApprovalPhase;
  receipt: ApprovalReceipt | null;
  receiptSource: DataSource;
  receiptOutcome: ApprovalOutcome;
  receiptNote?: string;
  onSelect: (id: string) => void;
  onApprove: () => void;
  onReject: () => void;
  onReplay: () => void;
}) {
  const fired = phase === "approved";
  const confirmedApplied = fired && receiptOutcome === "applied";
  const rehearsal = fired && receiptOutcome === "rehearsal";
  const unresolved = fired && receiptOutcome === "indeterminate";
  const [settled, setSettled] = useState(false);

  // Keyed on the change id as well as `fired`, so a refresh that swaps the
  // selected change cannot let the previous card's timer settle the new one.
  useEffect(() => {
    setSettled(false);
    if (!fired) {
      return;
    }
    return scheduleApprovalSettlement(receiptOutcome, () => setSettled(true));
  }, [fired, receiptOutcome, change.id]);

  const total = totalSessions(change.blastRadius);
  const showSettledImpact = settled && (confirmedApplied || rehearsal);
  const applied = receipt
    ? { interrupted: receipt.interrupted, preserved: receipt.preserved }
    : change.blastRadius;

  return (
    <div className="ap-page">
      <div className="ap-eyebrows">
        <span className="ap-eyebrow">
          <i className={confirmedApplied ? "ap-dot ap-dot--live" : "ap-dot"} />
          {!fired
            ? "Pending change · awaiting your approval"
            : receiptOutcome === "indeterminate"
              ? "Sent · outcome not confirmed"
              : rehearsal
                ? "Rehearsal · nothing was applied"
                : "Applied · every session was told"}
        </span>
        <SourceBadge source={dataSource} />
      </div>

      <h1>{headlineFor(change)}</h1>
      <p className="ap-sub">
        Dragback found an approved decision in Slack that changes work{" "}
        {inWords(total)} {total === 1 ? "person is" : "people are"} doing right
        now.
      </p>

      <PendingQueue
        changes={changes}
        selectedId={change.id}
        onSelect={onSelect}
      />

      <div className={fired && !unresolved ? "ap-card ap-card--fired" : "ap-card"}>
        <SlackSource source={change.source} />
        <RequirementDelta decision={change.decision} />
        <ProvenanceRail path={change.provenancePath} />
        <BlastRadius
          radius={showSettledImpact ? applied : change.blastRadius}
          applied={showSettledImpact}
          rehearsal={rehearsal}
          unresolved={unresolved}
        />
        <ApprovalAction
          phase={phase}
          permission={change.approverPermission}
          receipt={receipt}
          receiptSource={receiptSource}
          receiptOutcome={receiptOutcome}
          receiptNote={receiptNote}
          onApprove={onApprove}
          onReject={onReject}
          onReplay={onReplay}
        />
      </div>

      <p className="ap-viewswitch">
        Interrupted and wondering why? <a href="/approvals/why">/approvals/why</a>
      </p>
    </div>
  );
}
