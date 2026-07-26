import type { ApprovalOutcome } from "../api";
import type { ApprovalReceipt, DataSource } from "../model";

export type ApprovalPhase = "pending" | "approving" | "approved" | "rejected";

function Check() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M3 8.5l3.2 3.2L13 5"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * Approve posts; it does not decide. The receipt renders the counts the server
 * returned, and when the approval was a fixture rehearsal it says so rather than
 * dressing a replay up as a live result.
 *
 * Three outcomes, not two. `indeterminate` is the case where the approval was
 * sent and the answer was lost: the change may well have landed and sessions
 * may be being redirected right now. Showing "no approval was recorded" there
 * would contradict what the room can see, so it says the truth instead — sent,
 * unconfirmed, go and look.
 */
export function ApprovalAction({
  phase,
  permission,
  receipt,
  receiptSource,
  receiptOutcome,
  receiptNote,
  onApprove,
  onReject,
  onReplay,
}: {
  phase: ApprovalPhase;
  permission: string;
  receipt: ApprovalReceipt | null;
  receiptSource: DataSource;
  receiptOutcome: ApprovalOutcome;
  receiptNote?: string;
  onApprove: () => void;
  onReject: () => void;
  onReplay: () => void;
}) {
  if (phase === "approved" && receipt) {
    const rehearsal = receiptOutcome === "rehearsal";
    const unresolved = receiptOutcome === "indeterminate";
    const className = rehearsal
      ? "ap-receipt ap-receipt--rehearsal"
      : unresolved
        ? "ap-receipt ap-receipt--unresolved"
        : "ap-receipt";
    return (
      <div className="ap-sect ap-action">
        <span className={className}>
          <Check />
          {rehearsal
            ? `Would redirect ${receipt.interrupted.length}, preserve ${receipt.preserved.length}`
            : unresolved
              ? `Sent · up to ${receipt.interrupted.length} sessions may have been redirected`
              : `Applied · ${receipt.interrupted.length} sessions redirected, ${receipt.preserved.length} preserved`}
          {rehearsal ? (
            <span className="ap-receipt-note">
              rehearsal · no approval was recorded
              {receiptNote ? ` · ${receiptNote}` : ""}
            </span>
          ) : null}
          {unresolved ? (
            <span className="ap-receipt-note">
              sent, but the outcome could not be confirmed · this may already
              have been applied — check `writai dev status` before approving
              again
              {receiptNote ? ` · ${receiptNote}` : ""}
            </span>
          ) : null}
          <button
            type="button"
            className="ap-ghost ap-ghost--small"
            onClick={onReplay}
          >
            Replay
          </button>
        </span>
      </div>
    );
  }

  if (phase === "rejected") {
    return (
      <div className="ap-sect ap-action">
        <span className="ap-rejected">
          Not approved · nothing was changed and no session was interrupted
        </span>
        <button
          type="button"
          className="ap-ghost ap-ghost--small"
          onClick={onReplay}
        >
          Replay
        </button>
      </div>
    );
  }

  return (
    <div className="ap-sect ap-action">
      <button
        type="button"
        className="ap-primary"
        onClick={onApprove}
        disabled={phase === "approving"}
      >
        {phase === "approving" ? "Approving…" : "Approve change"}
      </button>
      <button
        type="button"
        className="ap-ghost"
        onClick={onReject}
        disabled={phase === "approving"}
      >
        Reject
      </button>
      {/*
        "Requires", not "You hold". `permission_id` is the permission the change
        needs; the browser has not authenticated anyone and cannot know whether
        the viewer holds it. The server checks it at approval time.
      */}
      <span className="ap-note">
        Requires <code>{permission}</code>
      </span>
    </div>
  );
}
