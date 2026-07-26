import { useCallback, useEffect, useRef, useState } from "react";
import "./approvals.css";
import { ApprovalScreen } from "./ApprovalScreen";
import { WhyView } from "./WhyView";
import { approveChange, fetchPendingChanges, subscribeToPendingChanges } from "./api";
import type { ApprovalOutcome } from "./api";
import type { ApprovalPhase } from "./components/ApprovalAction";
import { ApprovalsHeader } from "./components/ApprovalsHeader";
import { EmptyQueue } from "./components/EmptyQueue";
import type { ApprovalReceipt, DataSource, PendingChange } from "./model";

export function isWhyPath(pathname: string): boolean {
  return pathname.startsWith("/approvals/why");
}

export function ApprovalsRoute() {
  const [changes, setChanges] = useState<PendingChange[] | null>(null);
  const [dataSource, setDataSource] = useState<DataSource>("fixture");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [phase, setPhase] = useState<ApprovalPhase>("pending");
  const [receipt, setReceipt] = useState<ApprovalReceipt | null>(null);
  const [receiptSource, setReceiptSource] = useState<DataSource>("fixture");
  const [receiptOutcome, setReceiptOutcome] =
    useState<ApprovalOutcome>("rehearsal");
  const [receiptNote, setReceiptNote] = useState<string | undefined>(undefined);
  const [workspaceIds, setWorkspaceIds] = useState<string[]>([]);
  const live = dataSource === "live";
  const mounted = useRef(true);
  // Monotonic per-load token: SSE can trigger a refresh while the previous one
  // is in flight, and without this an older response can overwrite a newer one.
  const generation = useRef(0);

  const load = useCallback(async () => {
    generation.current += 1;
    const mine = generation.current;
    const result = await fetchPendingChanges();
    if (!mounted.current || mine !== generation.current) {
      return;
    }
    setChanges(result.changes);
    setDataSource(result.source);
    setWorkspaceIds(result.workspaceIds);
    setSelectedId((current) =>
      current && result.changes.some((change) => change.id === current)
        ? current
        : (result.changes[0]?.id ?? null),
    );
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      mounted.current = false;
    };
  }, [load]);

  // Live refresh over the existing SSE stream rather than polling, and only
  // once a live read has actually succeeded.
  useEffect(() => {
    if (!live) {
      return;
    }
    return subscribeToPendingChanges(workspaceIds, () => {
      void load();
    });
  }, [live, workspaceIds, load]);

  const selected =
    changes?.find((change) => change.id === selectedId) ?? changes?.[0] ?? null;

  // A refresh that swaps the change out from under an approved card would
  // otherwise leave the old receipt rendered against the new change.
  const receiptFor = useRef<string | null>(null);
  useEffect(() => {
    if (receipt && receiptFor.current !== selected?.id) {
      setPhase("pending");
      setReceipt(null);
      setReceiptNote(undefined);
    }
  }, [selected?.id, receipt]);

  const onApprove = useCallback(async () => {
    if (!selected) {
      return;
    }
    setPhase("approving");
    const result = await approveChange(selected);
    if (!mounted.current) {
      return;
    }
    receiptFor.current = selected.id;
    setReceipt(result.receipt);
    setReceiptSource(result.source);
    setReceiptOutcome(result.outcome);
    setReceiptNote(result.note);
    setPhase("approved");
  }, [selected]);

  const onReplay = useCallback(() => {
    setPhase("pending");
    setReceipt(null);
    setReceiptNote(undefined);
  }, []);

  const onSelect = useCallback((id: string) => {
    setSelectedId(id);
    setPhase("pending");
    setReceipt(null);
    setReceiptNote(undefined);
  }, []);

  if (!changes) {
    return (
      <div className="ap-root">
        <ApprovalsHeader view="approvals" />
        <div className="ap-page">
          <p className="ap-status">Loading pending changes…</p>
        </div>
      </div>
    );
  }

  if (!selected) {
    return (
      <div className="ap-root">
        <ApprovalsHeader view="approvals" />
        <div className="ap-page">
          <EmptyQueue dataSource={dataSource} />
        </div>
      </div>
    );
  }

  const why = isWhyPath(window.location.pathname);

  return (
    <div className="ap-root">
      <ApprovalsHeader view={why ? "why" : "approvals"} />
      {why ? (
        <WhyView change={selected} dataSource={dataSource} />
      ) : (
        <ApprovalScreen
          change={selected}
          changes={changes}
          dataSource={dataSource}
          phase={phase}
          receipt={receipt}
          receiptSource={receiptSource}
          receiptOutcome={receiptOutcome}
          receiptNote={receiptNote}
          onSelect={onSelect}
          onApprove={() => {
            void onApprove();
          }}
          onReject={() => setPhase("rejected")}
          onReplay={onReplay}
        />
      )}
    </div>
  );
}
