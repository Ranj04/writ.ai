import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import {
  ApprovalScreen,
  scheduleApprovalSettlement,
} from "./ApprovalScreen";
import { WhyView } from "./WhyView";
import { isWhyPath } from "./ApprovalsRoute";
import { BlastRadius } from "./components/BlastRadius";
import { EmptyQueue } from "./components/EmptyQueue";
import { PendingQueue } from "./components/PendingQueue";
import { FIXTURE_PENDING_CHANGE } from "./fixtures";
import type { ApprovalReceipt, PendingChange } from "./model";

const CHANGE = FIXTURE_PENDING_CHANGE;

const SECOND_CHANGE: PendingChange = {
  ...CHANGE,
  id: "CHANGE-DEC-021",
  source: {
    ...CHANGE.source,
    text: "Approved — uploads are limited to PDFs, effective immediately.",
  },
  decision: {
    id: "DEC-021",
    supersedes: "DEC-011",
    scope: "upload.format",
    was: "any file type",
    now: "PDF only",
  },
};

const RECEIPT: ApprovalReceipt = {
  changeId: CHANGE.id,
  interrupted: CHANGE.blastRadius.interrupted,
  preserved: CHANGE.blastRadius.preserved,
};

function screen(overrides: Partial<Parameters<typeof ApprovalScreen>[0]> = {}) {
  return renderToStaticMarkup(
    <ApprovalScreen
      change={CHANGE}
      changes={[CHANGE]}
      dataSource="fixture"
      phase="pending"
      receipt={null}
      receiptSource="fixture"
      receiptOutcome="rehearsal"
      onSelect={() => {}}
      onApprove={() => {}}
      onReject={() => {}}
      onReplay={() => {}}
      {...overrides}
    />,
  );
}

describe("ApprovalScreen", () => {
  const markup = screen();

  it("leads with the decision and the message that carried it", () => {
    expect(markup).toContain("Exports must be admin-only");
    expect(markup).toContain(
      "Approved — exports must be admin-only, effective immediately.",
    );
    expect(markup).toContain("Dana Kaur");
    expect(markup).toContain("no ticket referenced · no one tagged");
  });

  it("shows the requirement delta both ways round", () => {
    expect(markup).toContain("export.authorization → all users");
    expect(markup).toContain("export.authorization → admins only");
  });

  it("renders the chain as one rail, unaffected branch included", () => {
    for (const node of CHANGE.provenancePath) {
      expect(markup).toContain(node.title);
    }
    expect(markup).toContain("ap-node--calm");
  });

  it("renders the blast radius the server sent, and the number", () => {
    expect(markup).toContain("3 of 5");
    expect(markup).toContain("Priya Raman");
    expect(markup).toContain("Marcus Obi");
    expect(markup).toContain("Dan Levy");
    expect(markup).toContain("Ana Silva");
    expect(markup).toContain("Jonas Tan");
    expect(markup).toContain("five people are doing right now");
  });

  it("offers the approval control and names the permission", () => {
    expect(markup).toContain("Approve change");
    expect(markup).toContain("Reject");
    expect(markup).toContain("approve_compliance");
  });

  it("marks fixture data as fixture data", () => {
    expect(markup).toContain("fixture data");
    expect(screen({ dataSource: "live" })).toContain("live data");
  });

  it("fires the card once approved, and offers the replay control", () => {
    const applied = screen({
      phase: "approved",
      receipt: RECEIPT,
      receiptSource: "live",
      receiptOutcome: "applied",
    });
    expect(applied).toContain("ap-card--fired");
    expect(applied).toContain("Applied · 3 sessions redirected, 2 preserved");
    expect(applied).toContain("Replay");
    expect(applied).not.toContain("Approve change");
  });

  it("never dresses a fixture rehearsal up as a live approval", () => {
    const rehearsed = screen({
      phase: "approved",
      receipt: RECEIPT,
      receiptSource: "fixture",
    });
    expect(rehearsed).toContain("rehearsal · no approval was recorded");
    // The note alone is not enough: the rest of the card must not claim it
    // happened either.
    expect(rehearsed).not.toContain("Applied ·");
    expect(rehearsed).not.toContain("every session was told");
    expect(rehearsed).not.toContain("3 stopped");
    expect(rehearsed).not.toContain("sessions redirected");
    expect(rehearsed).toContain("Rehearsal · nothing was applied");
    expect(rehearsed).toContain("Would redirect 3, preserve 2");
    // The settled hero copy is behind a 900ms timer, so it is unreachable in a
    // static render — BlastRadius is exercised directly for that, below.

    const real = screen({
      phase: "approved",
      receipt: RECEIPT,
      receiptSource: "live",
      receiptOutcome: "applied",
      dataSource: "live",
    });
    expect(real).not.toContain("rehearsal");
    expect(real).toContain("Applied · 3 sessions redirected, 2 preserved");
  });

  it("says an unconfirmed approval is unconfirmed, not that nothing happened", () => {
    // INT-5. The approval was sent and the answer was lost. It may well have
    // landed, and agents may be being redirected behind this screen — so the
    // one thing this must never say is that no approval was recorded.
    const unresolved = screen({
      phase: "approved",
      receipt: RECEIPT,
      receiptSource: "live",
      receiptOutcome: "indeterminate",
      receiptNote: "the approval request failed: NetworkError",
      dataSource: "live",
    });

    expect(unresolved).toContain("could not be confirmed");
    expect(unresolved).toContain("may have been redirected");
    expect(unresolved).toContain("NetworkError");
    // Neither of the two confident claims is allowed here.
    expect(unresolved).not.toContain("no approval was recorded");
    expect(unresolved).not.toContain("Applied ·");
    expect(unresolved).not.toContain("nothing was applied");
  });

  it("does not settle an indeterminate approval after the 900ms delay", () => {
    vi.useFakeTimers();
    try {
      const onSettled = vi.fn();
      const cancel = scheduleApprovalSettlement(
        "indeterminate",
        onSettled,
      );

      vi.advanceTimersByTime(901);

      expect(onSettled).not.toHaveBeenCalled();
      const unresolved = screen({
        phase: "approved",
        receipt: RECEIPT,
        receiptSource: "live",
        receiptOutcome: "indeterminate",
        receiptNote: "the response was lost",
        dataSource: "live",
      });
      expect(unresolved).toContain("ap-receipt--unresolved");
      expect(unresolved).toContain("may have been interrupted");
      expect(unresolved).toContain("Potentially redirected");
      expect(unresolved).not.toContain("ap-card--fired");
      for (const forbidden of [
        "Nothing was applied",
        "nothing was applied",
        "no session was interrupted",
        "3 stopped",
        "3 would stop",
        "Each received",
        "will be interrupted and redirected",
        "Rehearsal ·",
        "Applied ·",
      ]) {
        expect(unresolved).not.toContain(forbidden);
      }
      cancel();
    } finally {
      vi.useRealTimers();
    }
  });

  it("never claims the viewer holds the permission it has not checked", () => {
    expect(markup).toContain("Requires");
    expect(markup).not.toContain("You hold");
  });

  it("names why an approval was only a rehearsal when the reason is known", () => {
    const rehearsed = screen({
      phase: "approved",
      receipt: RECEIPT,
      receiptSource: "fixture",
      receiptNote: "the web channel has no approval identity yet",
      dataSource: "live",
    });
    expect(rehearsed).toContain("the web channel has no approval identity yet");
  });

  it("says plainly that a rejection changed nothing", () => {
    const rejected = screen({ phase: "rejected" });
    expect(rejected).toContain("nothing was changed and no session was interrupted");
  });

  it("disables approval while the post is in flight", () => {
    expect(screen({ phase: "approving" })).toContain("disabled");
  });
});

describe("BlastRadius settled copy", () => {
  const settled = (rehearsal: boolean) =>
    renderToStaticMarkup(
      <BlastRadius radius={CHANGE.blastRadius} applied rehearsal={rehearsal} />,
    );

  it("reports what happened after a real approval", () => {
    const markup = settled(false);
    expect(markup).toContain("3 stopped");
    expect(markup).toContain("were never touched");
    expect(markup).not.toContain("would stop");
  });

  it("reports what WOULD happen after a rehearsal, and that nothing did", () => {
    const markup = settled(true);
    expect(markup).toContain("3 would stop");
    expect(markup).toContain("Nothing was applied");
    expect(markup).not.toContain("3 stopped");
    expect(markup).not.toContain("Each received");
  });

  it("counts from the server's lists in both states", () => {
    for (const rehearsal of [true, false]) {
      const markup = settled(rehearsal);
      expect(markup).toContain("Priya Raman");
      expect(markup).toContain("Ana Silva");
    }
  });
});

describe("EmptyQueue", () => {
  it("says nothing is pending without claiming anything was applied", () => {
    const markup = renderToStaticMarkup(<EmptyQueue dataSource="live" />);
    expect(markup).toContain("No pending changes");
    expect(markup).toContain("not holding any change for approval");
    // The empty queue is a statement about the queue, not about what happened
    // to any decision.
    expect(markup).not.toContain("applied");
    expect(markup).not.toContain("Applied");
  });

  it("keeps the fixture/live badge, which it used to drop", () => {
    expect(renderToStaticMarkup(<EmptyQueue dataSource="fixture" />)).toContain(
      "fixture data",
    );
    expect(renderToStaticMarkup(<EmptyQueue dataSource="live" />)).toContain(
      "live data",
    );
  });
});

describe("PendingQueue", () => {
  it("stays out of the way when only one change is waiting", () => {
    expect(
      renderToStaticMarkup(
        <PendingQueue changes={[CHANGE]} selectedId={CHANGE.id} onSelect={() => {}} />,
      ),
    ).toBe("");
  });

  it("lists every waiting change and marks the selected one", () => {
    const markup = renderToStaticMarkup(
      <PendingQueue
        changes={[CHANGE, SECOND_CHANGE]}
        selectedId={SECOND_CHANGE.id}
        onSelect={() => {}}
      />,
    );
    expect(markup).toContain("Exports must be admin-only");
    expect(markup).toContain("Uploads are limited to PDFs");
    expect(markup).toContain('aria-pressed="true"');
    // Half-built tab semantics read worse than an honest toggle group.
    expect(markup).not.toContain('role="tab"');
  });
});

describe("WhyView", () => {
  const markup = renderToStaticMarkup(
    <WhyView change={CHANGE} dataSource="fixture" />,
  );

  it("answers why the agent changed", () => {
    expect(markup).toContain("Why this change reaches your work");
    expect(markup).toContain("export.authorization is now admins only");
    expect(markup).toContain("DEC-018");
  });

  it("describes a pending change without claiming it already happened", () => {
    // This view renders a change awaiting approval and has no session identity.
    expect(markup).not.toContain("was redirected");
    expect(markup).not.toContain("your agent was told");
    expect(markup).toContain("told at its next tool call");
  });

  it("gives preserved work the same weight as invalidated work", () => {
    expect(markup).toContain("Needs redoing");
    expect(markup).toContain("Still stands");
    expect(markup).toContain("TASK-102 · expose export to all users");
    expect(markup).toContain("TASK-101 · generate CSV files");
    expect(markup).toContain("ap-work-col--invalidated");
    expect(markup).toContain("ap-work-col--preserved");
  });

  it("offers the interrupted developer no approval control", () => {
    expect(markup).not.toContain("Approve change");
  });
});

describe("isWhyPath", () => {
  it("separates the developer view from the approver view", () => {
    expect(isWhyPath("/approvals/why")).toBe(true);
    expect(isWhyPath("/approvals")).toBe(false);
  });
});
