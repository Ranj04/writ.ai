import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  approveChange,
  fetchPendingChanges,
  subscribeToPendingChanges,
} from "./api";
import { FIXTURE_PENDING_CHANGE } from "./fixtures";

const WORKSPACE = {
  id: "csv-exports",
  baseline_decision: {
    id: "DEC-004",
    attributes: {
      requirements: { "export.authorization": { audience: "all_users" } },
    },
  },
  tasks: [
    { id: "TASK-101", title: "TASK-101 · generate CSV files" },
    { id: "TASK-102", title: "TASK-102 · expose export to all users" },
  ],
  pending_mutation: { decision: { id: "DEC-018" }, supersedes_id: "DEC-004" },
  supervisor: {
    assignments: [
      { id: "A1", task_id: "TASK-102", agent_name: "Priya Raman" },
      { id: "A2", task_id: "TASK-101", agent_name: "Ana Silva" },
    ],
  },
};

const PREVIEW = {
  pending: {
    workspace_id: "csv-exports",
    decision_id: "DEC-018",
    supersedes_id: "DEC-004",
    affected_scopes: ["export.authorization"],
    permission_id: "approve_compliance",
    source_ref: "slack://compliance/decision-018",
    title: "Dana Kaur",
    text: "Approved — exports must be admin-only.",
    effective_at: "2026-07-24T14:41:00Z",
    requirements: { "export.authorization": { audience: "admin_only" } },
    proposal_fingerprint: `sha256:${"a".repeat(64)}`,
    proposal_instance_id: "proposal-1",
  },
  interrupted_assignment_ids: ["A1"],
  preserved_assignment_ids: ["A2"],
  assignment_provenance_paths: { A1: ["DEC-018", "TASK-102"] },
  correlation_id: "c-1",
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

/** Routes the two GETs the live path makes. */
function routed(
  workspaces: unknown,
  preview: unknown,
  previewStatus = 200,
): (url: string) => Promise<Response> {
  return async (url: string) =>
    url.includes("/preview")
      ? jsonResponse(preview, previewStatus)
      : jsonResponse(workspaces);
}

let warn: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  warn = vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("fetchPendingChanges", () => {
  it("reads the workspace list and its preview, and reports live", async () => {
    const fetchMock = vi.fn(routed({ workspaces: [WORKSPACE] }, PREVIEW));
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchPendingChanges();
    expect(result.source).toBe("live");
    expect(result.changes).toHaveLength(1);
    expect(result.changes[0].id).toBe("csv-exports:DEC-018");
    expect(result.workspaceIds).toEqual(["csv-exports"]);
    expect(warn).not.toHaveBeenCalled();

    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls[0]).toContain("/live-workspaces");
    expect(urls[1]).toContain(
      "/live-workspaces/csv-exports/decisions/DEC-018/preview",
    );
  });

  it("treats an empty queue as a real answer, not a fallback", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ workspaces: [] })),
    );
    const result = await fetchPendingChanges();
    expect(result.source).toBe("live");
    expect(result.changes).toEqual([]);
    expect(warn).not.toHaveBeenCalled();
  });

  it("ignores workspaces with nothing pending", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          workspaces: [{ ...WORKSPACE, pending_mutation: null }],
        }),
      ),
    );
    const result = await fetchPendingChanges();
    expect(result.source).toBe("live");
    expect(result.changes).toEqual([]);
  });

  it("falls back to the fixture when the agent service is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const result = await fetchPendingChanges();
    expect(result.source).toBe("fixture");
    expect(result.changes[0].id).toBe(FIXTURE_PENDING_CHANGE.id);
    expect(String(warn.mock.calls[0][0])).toContain("FIXTURE");
  });

  it("falls back on a non-2xx workspace list", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({}, 500)));
    expect((await fetchPendingChanges()).source).toBe("fixture");
    expect(warn).toHaveBeenCalledOnce();
  });

  it("falls back when the list body is unrecognised", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ workspaces: [{ nope: true }] })),
    );
    expect((await fetchPendingChanges()).source).toBe("fixture");
  });

  it("falls back when every preview fails rather than showing a half-read change", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(routed({ workspaces: [WORKSPACE] }, {}, 404)),
    );
    const result = await fetchPendingChanges();
    expect(result.source).toBe("fixture");
    expect(result.changes[0].id).toBe(FIXTURE_PENDING_CHANGE.id);
  });
});

describe("approveChange", () => {
  /** Fetch the queue first so the change is bound to its live proposal. */
  async function liveChange() {
    vi.stubGlobal("fetch", vi.fn(routed({ workspaces: [WORKSPACE] }, PREVIEW)));
    const result = await fetchPendingChanges();
    warn.mockClear();
    return result.changes[0];
  }

  /** The workspace as it reads AFTER the change was applied. */
  function appliedWorkspace() {
    return {
      ...WORKSPACE,
      pending_mutation: null,
      supervisor: {
        ...WORKSPACE.supervisor,
        applied_interrupts: [
          {
            decision_id: "DEC-018",
            interrupted_assignment_ids: ["A1"],
            preserved_assignment_ids: ["A2"],
          },
        ],
      },
    };
  }

  it("posts the approval when an identity is present in the runtime", async () => {
    const change = await liveChange();
    const approved = {
      ...WORKSPACE,
      pending_mutation: null,
      supervisor: {
        ...WORKSPACE.supervisor,
        applied_interrupts: [
          {
            decision_id: "DEC-018",
            interrupted_assignment_ids: ["A1"],
            preserved_assignment_ids: ["A2"],
          },
        ],
      },
    };
    const fetchMock = vi.fn(async () => jsonResponse(approved));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("__WRITAI_APPROVAL_TOKEN__", "token-from-the-operator");

    const result = await approveChange(change);
    expect(result.source).toBe("live");
    expect(result.note).toBeUndefined();
    expect(result.receipt.interrupted.map((p) => p.name)).toEqual([
      "Priya Raman",
    ]);
    expect(result.receipt.preserved.map((p) => p.name)).toEqual(["Ana Silva"]);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toContain(
      "/live-workspaces/csv-exports/decisions/DEC-018/approve",
    );
    expect(init.method).toBe("POST");
    const sent = JSON.parse(String(init.body));
    expect(sent.channel).toBe("workspace-ui");
    expect(sent.approval_token).toBe("token-from-the-operator");
    // The server rejects an approval that does not echo the exact proposal.
    expect(sent.confirmed_proposal_fingerprint).toBe(
      PREVIEW.pending.proposal_fingerprint,
    );
    expect(sent.confirmed_proposal_instance_id).toBe(
      PREVIEW.pending.proposal_instance_id,
    );
    expect(sent.evidence_ref).toContain("workspace-ui://approvals/csv-exports");
  });

  it("reports what the SERVER applied, not what the preview predicted", async () => {
    const change = await liveChange();
    expect(change.blastRadius.interrupted).toHaveLength(1);
    // The server interrupted nobody, contradicting the preview. The receipt
    // must follow the server.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          ...WORKSPACE,
          supervisor: {
            ...WORKSPACE.supervisor,
            applied_interrupts: [
              {
                decision_id: "DEC-018",
                interrupted_assignment_ids: [],
                preserved_assignment_ids: ["A1", "A2"],
              },
            ],
          },
        }),
      ),
    );
    vi.stubGlobal("__WRITAI_APPROVAL_TOKEN__", "token");

    const result = await approveChange(change);
    expect(result.source).toBe("live");
    expect(result.receipt.interrupted).toEqual([]);
    expect(result.receipt.preserved).toHaveLength(2);
  });

  it("rehearses, and never posts, when no identity is present", async () => {
    const change = await liveChange();
    vi.stubGlobal("__WRITAI_APPROVAL_TOKEN__", undefined);
    const fetchMock = vi.fn(async () => jsonResponse({}));
    vi.stubGlobal("fetch", fetchMock);

    const result = await approveChange(change);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.source).toBe("fixture");
    expect(result.note).toContain("writai approve");
    expect(warn).toHaveBeenCalledOnce();
  });

  it("rehearses a refusal rather than claiming the change was applied", async () => {
    const change = await liveChange();
    vi.stubGlobal("__WRITAI_APPROVAL_TOKEN__", "token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          { error: { code: "MISSING_PROPOSAL_CONFIRMATION", message: "stale" } },
          409,
        ),
      ),
    );
    const result = await approveChange(change);
    expect(result.source).toBe("fixture");
    expect(result.note).toContain("MISSING_PROPOSAL_CONFIRMATION");
  });

  it("does not claim a partition the server did not report", async () => {
    // INT-5. The POST succeeded, so the change may well have been applied — the
    // response simply did not carry the partition. Re-reading the workspace
    // does not settle it either, so this stays INDETERMINATE. The one thing it
    // must never do is report a possibly-real mutation as a rehearsal.
    const change = await liveChange();
    vi.stubGlobal("__WRITAI_APPROVAL_TOKEN__", "token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ ...WORKSPACE, supervisor: {} })),
    );
    const result = await approveChange(change);
    expect(result.outcome).toBe("indeterminate");
    expect(result.source).toBe("live");
    expect(result.note).toContain("did not report");
    expect(result.note).toContain("does not yet record this decision as applied");
  });

  it("resolves a lost response by re-reading the workspace", async () => {
    // The wifi-hiccup case: the approval landed, the answer did not come back.
    // One follow-up GET turns "we lost the answer" into a fact.
    const change = await liveChange();
    vi.stubGlobal("__WRITAI_APPROVAL_TOKEN__", "token");
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        call += 1;
        if (call === 1) {
          throw new TypeError("NetworkError when attempting to fetch resource.");
        }
        return jsonResponse(appliedWorkspace());
      }),
    );

    const result = await approveChange(change);
    expect(call).toBe(2);
    expect(result.outcome).toBe("applied");
    expect(result.source).toBe("live");
    expect(result.receipt.interrupted.length).toBeGreaterThan(0);
  });

  it("stays indeterminate when the re-read also fails", async () => {
    const change = await liveChange();
    vi.stubGlobal("__WRITAI_APPROVAL_TOKEN__", "token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("offline");
      }),
    );

    const result = await approveChange(change);
    expect(result.outcome).toBe("indeterminate");
    // Never "not applied" on the strength of a failed read.
    expect(result.source).not.toBe("fixture");
    expect(result.note).toContain("re-reading it failed");
  });

  it("treats a 4xx refusal as a rehearsal but a 5xx as unresolved", async () => {
    const refused = await liveChange();
    vi.stubGlobal("__WRITAI_APPROVAL_TOKEN__", "token");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ error: { code: "NOPE" } }, 403)),
    );
    expect((await approveChange(refused)).outcome).toBe("rehearsal");

    const wobbled = await liveChange();
    vi.stubGlobal("__WRITAI_APPROVAL_TOKEN__", "token");
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        call += 1;
        return call === 1
          ? jsonResponse({ error: { code: "BOOM" } }, 503)
          : jsonResponse(appliedWorkspace());
      }),
    );
    const result = await approveChange(wobbled);
    expect(result.outcome).toBe("applied");
  });

  it("never posts a change that came from the fixture", async () => {
    vi.stubGlobal("__WRITAI_APPROVAL_TOKEN__", "token");
    const fetchMock = vi.fn(async () => jsonResponse({}));
    vi.stubGlobal("fetch", fetchMock);

    const result = await approveChange(FIXTURE_PENDING_CHANGE);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.source).toBe("fixture");
    expect(result.note).toContain("fixture");
  });

  it("a fixture card cannot borrow a live change's approval credentials", async () => {
    // INT-4. `PendingChange.id` is the composite "{workspaceId}:{decisionId}",
    // and the fixture deliberately reuses the live ids. A binding map keyed by
    // that string let a fixture card find the live binding a previous load left
    // behind and POST a real approval from a screen labelled "fixture data".
    // Bindings are keyed by object identity now, so it cannot.
    const live = await liveChange();
    vi.stubGlobal("__WRITAI_APPROVAL_TOKEN__", "token");

    const impostor = { ...FIXTURE_PENDING_CHANGE, id: live.id };
    const fetchMock = vi.fn(async () => jsonResponse(appliedWorkspace()));
    vi.stubGlobal("fetch", fetchMock);

    const result = await approveChange(impostor);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.outcome).toBe("rehearsal");
    expect(result.source).toBe("fixture");
  });

  it("a stale change object keeps its own binding, not a newer one", async () => {
    // The other half of INT-4: two loads in flight must not cross-contaminate.
    const first = await liveChange();
    const second = await liveChange();
    expect(first).not.toBe(second);
    expect(first.id).toBe(second.id);

    vi.stubGlobal("__WRITAI_APPROVAL_TOKEN__", "token");
    const fetchMock = vi.fn(async () => jsonResponse(appliedWorkspace()));
    vi.stubGlobal("fetch", fetchMock);

    // Both are live objects, so both may approve — the point is that each
    // carries its own credentials rather than sharing one mutable slot.
    expect((await approveChange(first)).outcome).toBe("applied");
    expect((await approveChange(second)).outcome).toBe("applied");
  });

  it("mirrors the partition it was given without recomputing one", async () => {
    // Not a claim about a server receipt — approve is not wired (ASSUMPTIONS A2).
    // What this pins is that the rehearsal invents no partition of its own: a
    // regression that filtered or re-derived the lists client-side would fail.
    const skewed = {
      ...FIXTURE_PENDING_CHANGE,
      blastRadius: {
        interrupted: [FIXTURE_PENDING_CHANGE.blastRadius.interrupted[0]],
        preserved: FIXTURE_PENDING_CHANGE.blastRadius.preserved,
      },
    };
    const result = await approveChange(skewed);
    expect(result.receipt.interrupted).toEqual(skewed.blastRadius.interrupted);
    expect(result.receipt.preserved).toEqual(skewed.blastRadius.preserved);
  });
});

describe("subscribeToPendingChanges", () => {
  it("is a no-op where EventSource does not exist", () => {
    const unsubscribe = subscribeToPendingChanges(["csv-exports"], () => {
      throw new Error("should not fire");
    });
    expect(() => unsubscribe()).not.toThrow();
  });

  it("is a no-op when there are no live workspaces to watch", () => {
    vi.stubGlobal(
      "EventSource",
      class {
        addEventListener() {}
        removeEventListener() {}
        close() {}
      },
    );
    const onChanged = vi.fn();
    subscribeToPendingChanges([], onChanged)();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("opens one stream per workspace and closes them all on unsubscribe", () => {
    const opened: string[] = [];
    const listeners: (() => void)[] = [];
    const close = vi.fn();
    class FakeEventSource {
      constructor(url: string) {
        opened.push(url);
      }
      addEventListener(_type: string, handler: () => void): void {
        listeners.push(handler);
      }
      removeEventListener(): void {}
      close = close;
    }
    vi.stubGlobal("EventSource", FakeEventSource);

    const onChanged = vi.fn();
    const unsubscribe = subscribeToPendingChanges(["a", "b"], onChanged);
    expect(opened).toHaveLength(2);
    expect(opened[0]).toContain("/live-workspaces/a/events");
    listeners[0]();
    expect(onChanged).toHaveBeenCalledOnce();

    unsubscribe();
    expect(close).toHaveBeenCalledTimes(2);
  });
});
