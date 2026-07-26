import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createLiveWorkspaceClient,
  importWorkspaceWithConflictRetry,
  mapLiveWorkspace,
  parseWorkspaceDocument,
  prepareWorkspaceDocumentRun,
  serializeWorkspaceDocument,
} from "./api";
import {
  LiveWorkspaceApiError,
  type LiveWorkspaceClient,
  type LiveWorkspaceView,
} from "./model";
import { SAMPLE_WORKSPACE } from "./sample";
import { RAW_WORKSPACE } from "./test-fixtures";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Live Workspace document parsing", () => {
  it("parses JSON and YAML into the same structured import payload", () => {
    const json = parseWorkspaceDocument(
      JSON.stringify(SAMPLE_WORKSPACE),
      "json",
    );
    const yaml = parseWorkspaceDocument(
      `
id: yaml-workspace
name: YAML workspace
authority_policy:
  product.scope: [product-admin]
baseline_decision: {id: DEC-1}
specification: {id: SPEC-1}
ticket: {id: TICKET-1}
tasks: [{id: TASK-1}]
plan: {id: PLAN-1}
`,
      "yaml",
    );
    expect(json.id).toBe("refund-operations");
    expect(yaml.id).toBe("yaml-workspace");
    expect(yaml.authority_policy["product.scope"]).toEqual([
      "product-admin",
    ]);
  });

  it("rejects empty, malformed, and non-object documents before transport", () => {
    expect(() => parseWorkspaceDocument("", "json")).toThrow(
      "Paste a workspace document",
    );
    expect(() => parseWorkspaceDocument("{", "json")).toThrow(
      "could not be parsed",
    );
    expect(() => parseWorkspaceDocument("[]", "json")).toThrow(
      "one object",
    );
  });

  it("serializes prepared uploads back to their visible JSON or YAML format", () => {
    const document = {
      ...SAMPLE_WORKSPACE,
      id: "refund-upload-run-browser-001",
    };
    const json = serializeWorkspaceDocument(document, "json");
    const yaml = serializeWorkspaceDocument(document, "yaml");

    expect(parseWorkspaceDocument(json, "json").id).toBe(document.id);
    expect(parseWorkspaceDocument(yaml, "yaml").id).toBe(document.id);
    expect(json).toContain('"id": "refund-upload-run-browser-001"');
    expect(yaml).toContain("id: refund-upload-run-browser-001");
  });

  it("prepares repeated uploads of the same YAML as separate visible runs", () => {
    const yaml = serializeWorkspaceDocument(SAMPLE_WORKSPACE, "yaml");
    const first = prepareWorkspaceDocumentRun(
      yaml,
      "yaml",
      "browser-upload-001",
    );
    const second = prepareWorkspaceDocumentRun(
      yaml,
      "yaml",
      "browser-upload-002",
    );

    expect(first.document.id).toBe(
      "refund-operations-run-browser-upload-001",
    );
    expect(second.document.id).toBe(
      "refund-operations-run-browser-upload-002",
    );
    expect(first.document.id).not.toBe(second.document.id);
    expect(parseWorkspaceDocument(first.content, "yaml").id).toBe(
      first.document.id,
    );
    expect(parseWorkspaceDocument(second.content, "yaml").id).toBe(
      second.document.id,
    );
  });

  it("retries a browser import conflict once with a fresh run ID", async () => {
    const workspace = {} as LiveWorkspaceView;
    const importWorkspace = vi
      .fn<LiveWorkspaceClient["importWorkspace"]>()
      .mockRejectedValueOnce(
        new LiveWorkspaceApiError(
          "Live Workspace already exists: refund-operations",
          "LIVE_WORKSPACE_CONFLICT",
        ),
      )
      .mockResolvedValueOnce(workspace);
    const result = await importWorkspaceWithConflictRetry(
      { importWorkspace } as unknown as LiveWorkspaceClient,
      SAMPLE_WORKSPACE,
      {},
      "conflict-retry-001",
    );

    expect(importWorkspace).toHaveBeenCalledTimes(2);
    expect(importWorkspace.mock.calls[0]?.[0].id).toBe(
      "refund-operations",
    );
    expect(importWorkspace.mock.calls[1]?.[0].id).toBe(
      "refund-operations-run-conflict-retry-001",
    );
    expect(result).toMatchObject({
      document: {
        id: "refund-operations-run-conflict-retry-001",
      },
      retried: true,
      workspace,
    });
  });
});

describe("Live Workspace API mapping", () => {
  it("keeps the immutable initial plan separate from a corrected current plan", () => {
    const workspace = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      status: "complete",
      current_plan: {
        id: "PLAN-002",
        ticket_id: "PAY-104",
        objective: "Require approval before issuing refunds",
        actions: [
          {
            id: "ACTION-002",
            description: "Wait for finance approval",
            scopes: ["refund.execution"],
            attributes: { human_approval: true },
          },
        ],
      },
    });

    expect(workspace.initialPlan.id).toBe("PLAN-001");
    expect(workspace.initialPlan.actions[0]?.description).toBe(
      "Issue automatically",
    );
    expect(workspace.currentPlan.id).toBe("PLAN-002");
    expect(workspace.currentPlan.actions[0]?.description).toBe(
      "Wait for finance approval",
    );
  });

  it("maps additive decision, authorization, executor, path, and history fields without tokens", () => {
    const workspace = mapLiveWorkspace(RAW_WORKSPACE);
    expect(workspace.latestApprovedMutation?.decision.title).toBe(
      "Refunds over $500 require human approval",
    );
    expect(workspace.initialAuthorization?.grant).toMatchObject({
      authorizationId: "AUTH-001",
      decisionSnapshot: "graph-v17",
      planHash: "abc123",
    });
    expect(workspace.initialVerification).toMatchObject({
      applied: false,
      verificationCode: "STALE_SNAPSHOT",
    });
    expect(workspace.conflictAuthorization?.invalidationPath).toEqual([
      "DEC-002",
      "DEC-001",
      "SPEC-001",
      "PAY-104",
      "TASK-003",
      "PLAN-001",
    ]);
    expect(JSON.stringify(workspace)).not.toContain("signed_token");
    expect(JSON.stringify(workspace)).not.toContain("grant_token");
  });

  it("maps the simulated supervisor and scoped subagent control state", () => {
    const workspace = mapLiveWorkspace(RAW_WORKSPACE);

    expect(workspace.supervisor).toMatchObject({
      name: "writ.ai Supervisor",
      state: "interrupting",
      adapter: "fixture-agent-runtime",
      executionMode: "simulated",
    });
    expect(workspace.supervisor?.assignments).toHaveLength(2);
    expect(workspace.supervisor?.assignments[0]).toMatchObject({
      taskId: "TASK-001",
      runtimeProvider: "codex",
      state: "continuing",
      interruptEnforced: false,
    });
    expect(workspace.supervisor?.assignments[1]).toMatchObject({
      taskId: "TASK-003",
      runtimeProvider: "claude-code",
      state: "interrupted",
      interruptEnforced: true,
      provenancePath: [
        "DEC-002",
        "DEC-001",
        "SPEC-001",
        "PAY-104",
        "TASK-003",
        "PLAN-001",
      ],
    });
  });

  it("maps redacted Callwright execution metadata without carrying provider secrets", () => {
    const raw = {
      ...RAW_WORKSPACE,
      status: "complete",
      replacement_verification: {
        applied: true,
        reason: "Grant verified; Callwright call submitted.",
        verification_code: "VALID",
        pull_request_url: null,
        execution_mode: "simulated",
        call_receipt: {
          provider: "voyagr-callwright-fixture",
          call_id: "CALL-FIXTURE-018",
          status: "submitted",
          evidence_ref: "callwright://fixture/CALL-FIXTURE-018",
          phone_number: "+12025550100",
          api_key: "secret-api-key",
          grant_token: "signed-grant-token",
        },
      },
    } as Parameters<typeof mapLiveWorkspace>[0];

    const workspace = mapLiveWorkspace(raw);

    expect(workspace.replacementVerification).toMatchObject({
      applied: true,
      verificationCode: "VALID",
      executionMode: "simulated",
      callReceipt: {
        provider: "voyagr-callwright-fixture",
        callId: "CALL-FIXTURE-018",
        status: "submitted",
        evidenceRef: "callwright://fixture/CALL-FIXTURE-018",
      },
    });
    const serialized = JSON.stringify(workspace.replacementVerification);
    expect(serialized).not.toContain("+12025550100");
    expect(serialized).not.toContain("secret-api-key");
    expect(serialized).not.toContain("signed-grant-token");
  });

  it("uses the exact live-workspace routes and structured request bodies", async () => {
    const fetchMock = vi.fn(
      async (input: string | URL | Request, _init?: RequestInit) => {
      const url = String(input);
      const body = url.endsWith("/live-workspaces")
        ? { workspaces: [RAW_WORKSPACE] }
        : RAW_WORKSPACE;
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = createLiveWorkspaceClient();

    await client.list();
    await client.importWorkspace(SAMPLE_WORKSPACE);
    await client.approveBaseline("refund-operations", {
      approvalToken: "hex-user-key",
      proposalFingerprint: `sha256:${"a".repeat(64)}`,
      proposalInstanceId: "refund-operations:baseline:1",
    });
    await client.authorizePlan("refund-operations");
    await client.proposeChange("refund-operations", {
      decision: { id: "DEC-002" },
      supersedes_id: "DEC-001",
      affected_scopes: ["refund.execution"],
    });
    await client.cancelPendingChange("refund-operations");
    await client.approveChange(
      "refund-operations",
      "DEC-002",
      {
        approvalToken: "hex-user-key",
        proposalFingerprint: `sha256:${"b".repeat(64)}`,
        proposalInstanceId: "refund-operations:proposal:1",
      },
    );
    await client.verifyInitialGrant("refund-operations");
    await client.updatePlan("refund-operations", {
      id: "PLAN-002",
    });
    await client.reauthorize("refund-operations");
    await client.verifyReplacementGrant("refund-operations");

    const calls = fetchMock.mock.calls.map(([url, init]) => ({
      url: String(url),
      method: (init as RequestInit | undefined)?.method ?? "GET",
      body: (init as RequestInit | undefined)?.body,
    }));
    expect(calls.map((call) => call.url.replace(/^.*:8002/, ""))).toEqual([
      "/live-workspaces",
      "/live-workspaces/import",
      "/live-workspaces/refund-operations/baseline/approve",
      "/live-workspaces/refund-operations/authorize",
      "/live-workspaces/refund-operations/decisions/propose",
      "/live-workspaces/refund-operations/decisions/pending",
      "/live-workspaces/refund-operations/decisions/DEC-002/approve",
      "/live-workspaces/refund-operations/grants/initial/verify",
      "/live-workspaces/refund-operations/plan",
      "/live-workspaces/refund-operations/reauthorize",
      "/live-workspaces/refund-operations/grants/replacement/verify",
    ]);
    expect(calls[2]).toMatchObject({
      method: "POST",
      body: JSON.stringify({
        approval_token: "hex-user-key",
        channel: "workspace-ui",
        evidence_ref: `workspace-ui://refund-operations/baseline/sha256:${"a".repeat(64)}`,
        confirmed_proposal_fingerprint: `sha256:${"a".repeat(64)}`,
        confirmed_proposal_instance_id: "refund-operations:baseline:1",
      }),
    });
    expect(calls[5]).toMatchObject({
      method: "DELETE",
    });
    expect(calls[6]).toMatchObject({
      method: "POST",
      body: JSON.stringify({
        approval_token: "hex-user-key",
        channel: "workspace-ui",
        evidence_ref: `workspace-ui://refund-operations/DEC-002/sha256:${"b".repeat(64)}`,
        confirmed_proposal_fingerprint: `sha256:${"b".repeat(64)}`,
        confirmed_proposal_instance_id: "refund-operations:proposal:1",
      }),
    });
    expect(calls[8]).toMatchObject({
      method: "PUT",
      body: JSON.stringify({ plan: { id: "PLAN-002" } }),
    });
  });
});
