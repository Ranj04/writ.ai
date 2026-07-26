import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { mapLiveWorkspace } from "./api";
import { LiveWorkspace } from "./LiveWorkspace";
import type { LiveWorkspaceClient } from "./model";
import { WorkspaceImpact } from "./components/WorkspaceImpact";
import { WorkspaceImportForm } from "./components/WorkspaceImportForm";
import { WorkspaceChange } from "./components/WorkspaceChange";
import { WorkspaceBeforeEvidence } from "./components/WorkspaceBeforeEvidence";
import { WorkspaceGuide } from "./components/WorkspaceGuide";
import { WorkspaceActivity } from "./components/WorkspaceActivity";
import { WorkspaceSupervisor } from "./components/WorkspaceSupervisor";
import { workspaceGuide, workspaceReadiness } from "./state";
import { SAMPLE_WORKSPACE, SAMPLE_WORKSPACE_JSON } from "./sample";
import { RAW_WORKSPACE } from "./test-fixtures";

const client = {} as LiveWorkspaceClient;

describe("Live Workspace components", () => {
  it("renders the approved import information architecture and accessible form", () => {
    const html = renderToStaticMarkup(
      <WorkspaceImportForm
        content={SAMPLE_WORKSPACE_JSON}
        sourceName="dragback.json"
        format="json"
        readiness={workspaceReadiness(SAMPLE_WORKSPACE)}
        extractedDraft={false}
        extractionWarnings={[]}
        draftReviewed={false}
        busy={false}
        fileReading={false}
        errorMessage=""
        validationIssues={[]}
        onContentChange={() => undefined}
        onDraftReviewedChange={() => undefined}
        onFile={() => undefined}
        onSubmit={() => undefined}
        onDownloadTemplate={() => undefined}
        onDownloadDocumentTemplate={() => undefined}
        onDismissError={() => undefined}
      />,
    );
    expect(html).toContain("Choose a workspace file");
    expect(html).toContain(
      'accept=".yaml,.yml,.json,.pdf,.docx,.md,.markdown,.txt,.png,.jpg,.jpeg,.webp',
    );
    expect(html).toContain("Review or edit the workspace document");
    expect(html).toContain("What Dragback needs");
    expect(html).toContain("Download VOYAGR demo JSON");
    expect(html).toContain("Download document template");
    expect(html).toContain("or a screenshot");
    expect(html).toContain("Validate and continue");
    expect(html).toContain("Server validation is next");
  });

  it("labels an uploaded YAML document as YAML while preserving the same form", () => {
    const html = renderToStaticMarkup(
      <WorkspaceImportForm
        content="id: refund-operations"
        sourceName="dragback.yaml"
        format="yaml"
        readiness={workspaceReadiness({})}
        extractedDraft={false}
        extractionWarnings={[]}
        draftReviewed={false}
        busy={false}
        fileReading={false}
        errorMessage=""
        validationIssues={[]}
        onContentChange={() => undefined}
        onDraftReviewedChange={() => undefined}
        onFile={() => undefined}
        onSubmit={() => undefined}
        onDownloadTemplate={() => undefined}
        onDownloadDocumentTemplate={() => undefined}
        onDismissError={() => undefined}
      />,
    );
    expect(html).toContain("Workspace YAML");
    expect(html).toContain("dragback.yaml · fresh run ID assigned");
    expect(html).not.toContain("Workspace JSON");
  });

  it("explains that an extracted document is an unapproved draft", () => {
    const html = renderToStaticMarkup(
      <WorkspaceImportForm
        content={SAMPLE_WORKSPACE_JSON}
        sourceName="requirements.pdf"
        format="json"
        readiness={workspaceReadiness(SAMPLE_WORKSPACE)}
        extractedDraft
        extractionWarnings={["No Ticket section was found."]}
        draftReviewed={false}
        busy={false}
        fileReading={false}
        errorMessage=""
        validationIssues={[]}
        onContentChange={() => undefined}
        onDraftReviewedChange={() => undefined}
        onFile={() => undefined}
        onSubmit={() => undefined}
        onDownloadTemplate={() => undefined}
        onDownloadDocumentTemplate={() => undefined}
        onDismissError={() => undefined}
      />,
    );
    expect(html).toContain("Document converted to a reviewable draft");
    expect(html).toContain("Screenshot text is read with local OCR");
    expect(html).toContain("workspace snapshot");
    expect(html).toContain("Before the new");
    expect(html).toContain("cannot approve this draft automatically");
    expect(html).toContain("I reviewed the extracted decision");
    expect(html).toContain("No Ticket section was found");
    expect(html).toContain("Review draft and continue");
    expect(html).toContain('type="checkbox"');
  });

  it("keeps an uploaded PDF visible beside its extracted draft", () => {
    const html = renderToStaticMarkup(
      <WorkspaceImportForm
        content={SAMPLE_WORKSPACE_JSON}
        sourceName="refund-policy.pdf"
        format="json"
        readiness={workspaceReadiness(SAMPLE_WORKSPACE)}
        sourcePreview={{ kind: "pdf", url: "blob:refund-policy" }}
        extractedDraft
        extractionWarnings={[]}
        draftReviewed={false}
        busy={false}
        fileReading={false}
        errorMessage=""
        validationIssues={[]}
        onContentChange={() => undefined}
        onDraftReviewedChange={() => undefined}
        onFile={() => undefined}
        onSubmit={() => undefined}
        onDownloadTemplate={() => undefined}
        onDownloadDocumentTemplate={() => undefined}
        onDismissError={() => undefined}
      />,
    );

    expect(html).toContain("Original PDF");
    expect(html).toContain("Not uploaded yet");
    expect(html).toContain('data="blob:refund-policy"');
    expect(html).toContain('aria-label="Preview of refund-policy.pdf"');
  });

  it("blocks stale workspace submission while screenshot OCR is running", () => {
    const html = renderToStaticMarkup(
      <WorkspaceImportForm
        content={SAMPLE_WORKSPACE_JSON}
        sourceName="ticket.png"
        format="json"
        readiness={workspaceReadiness(SAMPLE_WORKSPACE)}
        extractedDraft={false}
        extractionWarnings={[]}
        draftReviewed={false}
        busy={false}
        fileReading
        errorMessage=""
        validationIssues={[]}
        onContentChange={() => undefined}
        onDraftReviewedChange={() => undefined}
        onFile={() => undefined}
        onSubmit={() => undefined}
        onDownloadTemplate={() => undefined}
        onDownloadDocumentTemplate={() => undefined}
        onDismissError={() => undefined}
      />,
    );
    expect(html).toContain("Reading file…");
    expect(html).toContain('id="workspace-file"');
    expect(html.match(/disabled=""/g)?.length).toBeGreaterThanOrEqual(4);
  });

  it("renders Workspace as the active home with Examples available", () => {
    const html = renderToStaticMarkup(
      <LiveWorkspace client={client} servicesOnline={3} servicesTotal={3} />,
    );
    expect(html).toContain("Live Workspace");
    expect(html).toContain('href="/" aria-current="page"');
    expect(html).toContain(">Workspace</a>");
    expect(html).toContain(">Examples</button>");
    expect(html).not.toContain("Guided Proof");
    expect(html).not.toContain(">Scenario Lab</button>");
    expect(html).not.toContain("Run report");
    expect(html).toContain("Step 1 of 5");
    expect(html).toContain("What happens next");
    expect(html).toContain("Starter example · fresh ID each run");
    expect(html).toContain("voyagr-reservation-run-");
    expect(html).toContain(
      "This is the exact document Dragback will validate.",
    );
    expect(html).not.toContain("Example workflow");
  });

  it("keeps the before evidence, assigned agent, and immutable old plan visible after replanning", () => {
    const workspace = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      status: "complete",
      current_plan: {
        id: "PLAN-002",
        ticket_id: "PAY-104",
        objective: "Require finance approval",
        actions: [
          {
            id: "ACTION-CORRECTED",
            description: "Wait for finance approval",
            scopes: ["refund.execution"],
            attributes: { human_approval: true },
          },
        ],
      },
    });
    const html = renderToStaticMarkup(
      <WorkspaceBeforeEvidence
        workspace={workspace}
        titleId="before-evidence-test-title"
      />,
    );

    expect(html).toContain("Before the new decision");
    expect(html).toContain("Stored baseline evidence");
    expect(html).toContain("Automatic refunds detail");
    expect(html).toContain("Unchanged ticket");
    expect(html).toContain("PAY-104");
    expect(html).toContain("Registered agent profile");
    expect(html).toContain("Payments Coding Agent");
    expect(html).toContain("creates scoped subagent runs");
    expect(html).toContain("Stored initial plan");
    expect(html).toContain("PLAN-001");
    expect(html).toContain("Issue automatically");
    expect(html).toContain("graph-v17");
    expect(html).not.toContain("Wait for finance approval");
  });

  it("renders backend-owned stale grant, selective tasks, actual decision wording, and token-free evidence", () => {
    const workspace = mapLiveWorkspace(RAW_WORKSPACE);
    const html = renderToStaticMarkup(
      <WorkspaceImpact
        workspace={workspace}
        planContent='{"id":"PLAN-002"}'
        busy={false}
        evidenceOpen
        onPlanContentChange={() => undefined}
        onSaveAndReauthorize={() => undefined}
        onReauthorize={() => undefined}
        onVerifyReplacement={() => undefined}
        onDownloadReport={() => undefined}
        onToggleEvidence={() => undefined}
      />,
    );
    expect(html).toContain("The original authorization is stale.");
    expect(html).toContain("1 task stopped.");
    expect(html).toContain("1 task remains valid.");
    expect(html).toContain("Refunds over $500 require human approval");
    expect(html).toContain("Rejected · STALE_SNAPSHOT");
    expect(html).toContain("Calculate amount");
    expect(html).toContain("Issue automatically");
    expect(html).toContain("Preserved");
    expect(html).toContain("Stopped");
    expect(html).toContain("DEC-002");
    expect(html).toContain("Grant signatures and raw tokens are intentionally not exposed.");
    expect(html).not.toContain("signed_token");
  });

  it("tells the truth about what is enforcing and what is recorded state", () => {
    const simulated = mapLiveWorkspace(RAW_WORKSPACE);
    const simulatedHtml = renderToStaticMarkup(
      <WorkspaceSupervisor workspace={simulated} titleId="t" />,
    );
    expect(simulatedHtml).toContain("No provider process is controlled");
    expect(simulatedHtml).toContain("recorded state, not an agent being");
    expect(simulatedHtml).not.toContain("Hooks fail open");

    const live = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      supervisor: {
        ...RAW_WORKSPACE.supervisor!,
        execution_mode: "live",
        assignments: RAW_WORKSPACE.supervisor!.assignments!.map((assignment) => ({
          ...assignment,
          execution_mode: "live" as const,
        })),
      },
    });
    const liveHtml = renderToStaticMarkup(
      <WorkspaceSupervisor workspace={live} titleId="t" />,
    );
    // The headline claim and its honest limit must ship together.
    expect(liveHtml).toContain("Live enforcement");
    expect(liveHtml).toContain("PreToolUse");
    expect(liveHtml).toContain("Hooks fail open");
    expect(liveHtml).toContain("the tool call proceeds");
    expect(liveHtml).toContain("PR check is the backstop");
    // In live mode the interrupt reaches a session, not the executor.
    expect(liveHtml).toContain("denied once and handed the correction");
    expect(liveHtml).not.toContain("The executor rejected the old grant");
  });

  it("discloses the standing caveats in both live and simulated mode", () => {
    // These are facts about the build, not about one workspace, and each is
    // something a viewer would otherwise assume is live. Losing them from the
    // simulated branch would be the easiest way for them to quietly disappear.
    const live = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      supervisor: {
        ...RAW_WORKSPACE.supervisor!,
        execution_mode: "live",
        assignments: RAW_WORKSPACE.supervisor!.assignments!.map((assignment) => ({
          ...assignment,
          execution_mode: "live" as const,
        })),
      },
    });

    for (const workspace of [mapLiveWorkspace(RAW_WORKSPACE), live]) {
      const html = renderToStaticMarkup(
        <WorkspaceSupervisor workspace={workspace} titleId="t" />,
      );
      expect(html).toContain("required status check on protected main");
      // Extraction reaches review, but no vendor delivery or approval was exercised.
      expect(html).toContain(
        "Slack extraction is live to a pending proposal in direct",
      );
      expect(html).toContain("No real Composio webhook");
      expect(html).toContain("human_reviewed=false");
      expect(html).toContain("--scope/--was/--now");
      // The CrustData payload is replayed, and is not even a real capture.
      expect(html).toContain("never fires live here");
      expect(html).toContain("documentation-reconstructed, not captured");
      // The seeder's bypass is named, scoped, and shown to be gated.
      expect(html).toContain("approves without channel authentication");
      expect(html).toContain("DRAGBACK_DEMO_UNAUTHENTICATED_APPROVAL=1");
      expect(html).toContain("Every authority check still runs");
      // The known durability limits.
      expect(html).toContain("process-local");
    }
  });

  it("shows the supervisor selectively interrupting and redirecting subagents", () => {
    const interrupted = mapLiveWorkspace(RAW_WORKSPACE);
    const interruptedHtml = renderToStaticMarkup(
      <WorkspaceSupervisor
        workspace={interrupted}
        titleId="supervisor-test-title"
      />,
    );

    expect(interruptedHtml).toContain("Dragback Supervisor");
    expect(interruptedHtml).toContain("Deterministic authority");
    expect(interruptedHtml).toContain("Simulated adapter");
    expect(interruptedHtml).toContain("Refund Calculation Subagent");
    expect(interruptedHtml).toContain("No interrupt requested");
    expect(interruptedHtml).toContain("Refund Execution Subagent");
    expect(interruptedHtml).toContain("Interrupt request issued");
    expect(interruptedHtml).toContain("Interrupt reason");
    expect(interruptedHtml).toContain(
      "The approved refund execution requirement changed at graph-v18.",
    );
    expect(interruptedHtml).toContain("The executor rejected the old grant");
    expect(interruptedHtml).not.toContain("Cancel signal accepted");
    expect(interruptedHtml).toContain(
      "dragback agent run refund-operations --task TASK-003 --provider claude-code",
    );

    const redirected = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      supervisor: {
        ...RAW_WORKSPACE.supervisor!,
        state: "redirecting",
        assignments: RAW_WORKSPACE.supervisor!.assignments!.map(
          (assignment) =>
            assignment.task_id === "TASK-003"
              ? {
                  ...assignment,
                  state: "redirected" as const,
                  run_id: "LIVE-REFUND-OPERATIONS-TASK-003-RUN-2",
                  plan_id: "PLAN-002",
                  decision_snapshot: "graph-v18",
                  redirected_from_run_id: assignment.run_id,
                  redirect_instruction:
                    "Wait for finance approval before issuing the refund.",
                }
              : assignment,
        ),
      },
    });
    const redirectedHtml = renderToStaticMarkup(
      <WorkspaceSupervisor
        workspace={redirected}
        titleId="redirected-supervisor-test-title"
      />,
    );

    expect(redirectedHtml).toContain("Replacement assignment issued");
    expect(redirectedHtml).toContain("Redirected run");
    expect(redirectedHtml).toContain(
      "LIVE-REFUND-OPERATIONS-TASK-003-RUN-1",
    );
    expect(redirectedHtml).toContain(
      "LIVE-REFUND-OPERATIONS-TASK-003-RUN-2",
    );
    expect(redirectedHtml).toContain(
      "Wait for finance approval before issuing the refund.",
    );

    const resumed = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      supervisor: {
        ...RAW_WORKSPACE.supervisor!,
        state: "resumed",
        assignments: RAW_WORKSPACE.supervisor!.assignments!.map(
          (assignment) =>
            assignment.task_id === "TASK-003"
              ? {
                  ...assignment,
                  state: "resumed" as const,
                  decision_snapshot: "graph-v18",
                }
              : assignment,
        ),
      },
    });
    const resumedHtml = renderToStaticMarkup(
      <WorkspaceSupervisor
        workspace={resumed}
        titleId="resumed-supervisor-test-title"
      />,
    );
    expect(resumedHtml).toContain("Replacement run requested");
    expect(resumedHtml).not.toContain("Replacement run started");
  });

  it("does not invent a Codex command for a generic runtime provider", () => {
    const generic = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      supervisor: {
        ...RAW_WORKSPACE.supervisor!,
        assignments: [
          {
            ...RAW_WORKSPACE.supervisor!.assignments![0]!,
            runtime_provider: "generic",
          },
        ],
      },
    });

    const html = renderToStaticMarkup(
      <WorkspaceSupervisor
        workspace={generic}
        titleId="generic-supervisor-test-title"
      />,
    );

    expect(html).toContain("Generic coding agent");
    expect(html).not.toContain("Developer entry point");
    expect(html).not.toContain("--provider codex");
  });

  it("shows the corrected plan in plain language while keeping JSON optional", () => {
    const workspace = mapLiveWorkspace(RAW_WORKSPACE);
    const html = renderToStaticMarkup(
      <WorkspaceImpact
        workspace={workspace}
        planContent={JSON.stringify({
          id: "PLAN-002",
          objective:
            "Continue customer synchronization with read-only CRM access",
          actions: [
            {
              id: "ACTION-READ",
              description: "Read customer records",
            },
            {
              id: "ACTION-CORRECTED",
              description:
                "Remove CRM create, update, and delete operations",
            },
          ],
        })}
        busy={false}
        evidenceOpen={false}
        onPlanContentChange={() => undefined}
        onSaveAndReauthorize={() => undefined}
        onReauthorize={() => undefined}
        onVerifyReplacement={() => undefined}
        onDownloadReport={() => undefined}
        onToggleEvidence={() => undefined}
      />,
    );

    expect(html).toContain("Proposed corrected plan");
    expect(html).toContain(
      "Continue customer synchronization with read-only CRM access",
    );
    expect(html).toContain("Read customer records");
    expect(html).toContain(
      "Remove CRM create, update, and delete operations",
    );
    expect(html).toContain("Edit technical plan JSON");
    expect(html).toContain('class="lw-plan-technical-editor"');
    expect(html).not.toContain(
      'class="lw-plan-technical-editor" open=""',
    );
  });

  it("makes the Callwright safety boundary and final call action explicit", () => {
    const callwrightPlan = {
      id: "PLAN-VOYAGR-018",
      ticket_id: "EVENT-208",
      objective: "Request the newly approved reservation time",
      actions: [
        {
          id: "ACTION-CALL-001",
          description: "Call the venue for 8:30 PM",
          scopes: ["reservation.time"],
          attributes: {
            provider: "voyagr-callwright",
            phone_number_ref: "demo-venue",
          },
        },
      ],
    };
    const staleWorkspace = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      initial_plan: callwrightPlan,
      current_plan: callwrightPlan,
    });
    const staleHtml = renderToStaticMarkup(
      <WorkspaceImpact
        workspace={staleWorkspace}
        planContent="{}"
        busy={false}
        evidenceOpen={false}
        onPlanContentChange={() => undefined}
        onSaveAndReauthorize={() => undefined}
        onReauthorize={() => undefined}
        onVerifyReplacement={() => undefined}
        onDownloadReport={() => undefined}
        onToggleEvidence={() => undefined}
      />,
    );
    expect(staleHtml).toContain("VOYAGR Callwright");
    expect(staleHtml).toContain("Callwright not invoked");
    expect(staleHtml).toContain(
      "rejected the stale authorization before any call request",
    );
    expect(staleHtml).not.toContain("Place authorized call");

    const reauthorizedWorkspace = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      status: "reauthorized",
      initial_plan: callwrightPlan,
      current_plan: callwrightPlan,
      initial_verification: RAW_WORKSPACE.initial_verification,
      replacement_authorization: RAW_WORKSPACE.initial_authorization,
      replacement_verification: null,
    });
    const readyHtml = renderToStaticMarkup(
      <WorkspaceImpact
        workspace={reauthorizedWorkspace}
        planContent="{}"
        busy={false}
        evidenceOpen={false}
        onPlanContentChange={() => undefined}
        onSaveAndReauthorize={() => undefined}
        onReauthorize={() => undefined}
        onVerifyReplacement={() => undefined}
        onDownloadReport={() => undefined}
        onToggleEvidence={() => undefined}
      />,
    );
    const busyHtml = renderToStaticMarkup(
      <WorkspaceImpact
        workspace={reauthorizedWorkspace}
        planContent="{}"
        busy
        evidenceOpen={false}
        onPlanContentChange={() => undefined}
        onSaveAndReauthorize={() => undefined}
        onReauthorize={() => undefined}
        onVerifyReplacement={() => undefined}
        onDownloadReport={() => undefined}
        onToggleEvidence={() => undefined}
      />,
    );
    expect(readyHtml).toContain("Place authorized call");
    expect(busyHtml).toContain("Placing authorized call…");

    const failedWorkspace = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      status: "reauthorized",
      initial_plan: callwrightPlan,
      current_plan: callwrightPlan,
      initial_verification: RAW_WORKSPACE.initial_verification,
      replacement_authorization: RAW_WORKSPACE.initial_authorization,
      replacement_verification: {
        applied: false,
        reason:
          "Grant verified, but Callwright timed out with an unknown submission outcome.",
        verification_code: "VALID",
        execution_mode: "live",
      },
    });
    const failedHtml = renderToStaticMarkup(
      <WorkspaceImpact
        workspace={failedWorkspace}
        planContent="{}"
        busy={false}
        evidenceOpen={false}
        onPlanContentChange={() => undefined}
        onSaveAndReauthorize={() => undefined}
        onReauthorize={() => undefined}
        onVerifyReplacement={() => undefined}
        onDownloadReport={() => undefined}
        onToggleEvidence={() => undefined}
      />,
    );
    expect(failedHtml).toContain("Protected execution needs review");
    expect(failedHtml).toContain("Recheck this same authorization");
    expect(failedHtml).toContain("Recheck protected execution");
    expect(failedHtml).not.toContain("Issue fresh authorization");
  });

  it("renders only redacted Callwright receipt metadata after completion", () => {
    const callwrightPlan = {
      id: "PLAN-VOYAGR-018",
      ticket_id: "EVENT-208",
      objective: "Request the newly approved reservation time",
      actions: [
        {
          id: "ACTION-CALL-001",
          description: "Call the venue for 8:30 PM",
          scopes: ["reservation.time"],
          attributes: {
            provider: "voyagr-callwright",
            phone_number_ref: "demo-venue",
          },
        },
      ],
    };
    const workspace = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      status: "complete",
      initial_plan: callwrightPlan,
      current_plan: callwrightPlan,
      replacement_verification: {
        applied: true,
        reason: "Grant verified; Callwright call submitted.",
        verification_code: "VALID",
        execution_mode: "live",
        call_receipt: {
          provider: "voyagr-callwright",
          call_id: "CALL-LIVE-018",
          status: "submitted",
          evidence_ref: "callwright://calls/CALL-LIVE-018",
        },
      },
    });
    const html = renderToStaticMarkup(
      <WorkspaceImpact
        workspace={workspace}
        planContent="{}"
        busy={false}
        evidenceOpen={false}
        onPlanContentChange={() => undefined}
        onSaveAndReauthorize={() => undefined}
        onReauthorize={() => undefined}
        onVerifyReplacement={() => undefined}
        onDownloadReport={() => undefined}
        onToggleEvidence={() => undefined}
      />,
    );

    expect(html).toContain("Authorized call submitted");
    expect(html).toContain(">Live<");
    expect(html).toContain("CALL-LIVE-018");
    expect(html).toContain("submitted");
    expect(html).toContain("callwright://calls/CALL-LIVE-018");
    expect(html).toContain("execution metadata only");
    expect(html).not.toContain("demo-venue");
    expect(html).not.toContain("+12025550100");
    expect(html).not.toContain("secret-api-key");
    expect(html).not.toContain("signed-grant-token");
  });

  it("offers a low-emphasis recovery action while a decision proposal is pending", () => {
    const workspace = mapLiveWorkspace({
      ...RAW_WORKSPACE,
      status: "change-proposed",
      pending_mutation: RAW_WORKSPACE.latest_approved_mutation,
      latest_approved_mutation: null,
      conflict_authorization: null,
      initial_verification: null,
      invalidation_report: null,
    });
    const html = renderToStaticMarkup(
      <WorkspaceChange
        workspace={workspace}
        content="{}"
        approvalToken="hex-user-key"
        busy={false}
        onContentChange={() => undefined}
        onApprovalTokenChange={() => undefined}
        onPropose={() => undefined}
        onCancel={() => undefined}
        onApprove={() => undefined}
        onVerify={() => undefined}
      />,
    );
    expect(html).toContain("Cancel proposal");
    expect(html).toContain("sl-button--quiet");
  });

  it("explains one current step and one next outcome without repeating all stage descriptions", () => {
    const html = renderToStaticMarkup(
      <WorkspaceGuide
        guide={workspaceGuide("initial-grant-rejected")}
        busy={false}
      />,
    );
    expect(html).toContain("Step 5 of 5");
    expect(html).toContain("Do this now");
    expect(html).toContain("Update the affected plan");
    expect(html).toContain("What happens next");
    expect(html).toContain("Old authorization rejected");
    expect(html).not.toContain("Bring in your decisions");
  });

  it("uses explicit wait language while a step is running", () => {
    const guide = workspaceGuide("change-applied");
    const guideHtml = renderToStaticMarkup(
      <WorkspaceGuide guide={guide} busy />,
    );
    const activityHtml = renderToStaticMarkup(
      <WorkspaceActivity
        events={[]}
        busy
        busyMessage={guide.busyMessage}
      />,
    );
    expect(guideHtml).toContain("Dragback is working");
    expect(guideHtml).toContain("Please wait");
    expect(guideHtml).toContain("Keep this page open");
    expect(activityHtml).toContain("Working on this step");
    expect(activityHtml).toContain("independent executor is checking");
  });

  it("maps known activity types to readable updates without inferring tone from free text", () => {
    const html = renderToStaticMarkup(
      <WorkspaceActivity
        events={[
          {
            sequence: 1,
            eventType: "initial-grant.verified",
            detail: "Executor verification returned STALE_SNAPSHOT.",
            createdAt: "2026-07-23T18:00:00Z",
            data: { applied: false },
          },
        ]}
        busy={false}
        busyMessage="Checking"
      />,
    );
    expect(html).toContain("Original authorization checked");
    expect(html).toContain("Activity history (1)");
    expect(html).toContain("lw-activity__current--negative");
    expect(html).not.toContain("initial-grant verified");
  });
});
