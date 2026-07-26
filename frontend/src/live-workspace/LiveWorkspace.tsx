import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { AppShell } from "../scenario-lab/components/AppShell";
import {
  LiveWorkspaceApiError,
  type LiveWorkspaceClient,
  type LiveWorkspaceStatus,
  type LiveWorkspaceView,
  type WorkspaceDocumentFormat,
  type WorkspaceValidationIssue,
} from "./model";
import {
  importWorkspaceWithConflictRetry,
  parseWorkspaceDocument,
  prepareWorkspaceDocumentRun,
  serializeWorkspaceDocument,
} from "./api";
import {
  CALLWRIGHT_SAMPLE_WORKSPACE,
  correctedPlanDocument,
  createCallwrightWorkspaceRun,
  initialChangeDocument,
} from "./sample";
import {
  editWorkspaceDocument,
  workspaceGuide,
  workspaceReadiness,
  workspaceVerificationReport,
} from "./state";
import {
  confirmExtractedWorkspaceDraft,
  DOCUMENT_TEMPLATE,
  extractWorkspaceDraft,
  isImageDocument,
  isSupportedDocument,
  readDocumentText,
} from "./document-extraction";
import { ValidationSummary } from "./components/ValidationSummary";
import {
  WorkspaceActivity,
  type WorkspaceLiveUpdate,
} from "./components/WorkspaceActivity";
import { WorkspaceAuthorization } from "./components/WorkspaceAuthorization";
import { WorkspaceBaseline } from "./components/WorkspaceBaseline";
import { WorkspaceBeforeEvidence } from "./components/WorkspaceBeforeEvidence";
import { WorkspaceChange } from "./components/WorkspaceChange";
import { WorkspaceGuide } from "./components/WorkspaceGuide";
import {
  WorkspaceImportForm,
  type WorkspaceSourcePreview,
} from "./components/WorkspaceImportForm";
import { WorkspaceImpact } from "./components/WorkspaceImpact";
import { WorkspaceStageRail } from "./components/WorkspaceStageRail";
import { WorkspaceSupervisor } from "./components/WorkspaceSupervisor";
import "./live-workspace.css";

interface ActionError {
  message: string;
  issues: readonly WorkspaceValidationIssue[];
}

const IMPACT_STATUSES = new Set<LiveWorkspaceStatus>([
  "initial-grant-rejected",
  "plan-updated",
  "reauthorized",
  "complete",
]);

export interface LiveWorkspaceProps {
  client: LiveWorkspaceClient;
  initialWorkspace?: LiveWorkspaceView | null;
  servicesOnline?: number;
  servicesTotal?: number;
  onWorkspaceLoaded?: (workspaceId: string) => void;
}

function actionError(caught: unknown): ActionError {
  if (caught instanceof LiveWorkspaceApiError) {
    return { message: caught.message, issues: caught.issues };
  }
  return {
    message: caught instanceof Error ? caught.message : String(caught),
    issues: [],
  };
}

function downloadJson(filename: string, value: unknown): void {
  const blob = new Blob([JSON.stringify(value, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function downloadText(filename: string, value: string, type: string): void {
  const blob = new Blob([value], { type });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function LiveWorkspace({
  client,
  initialWorkspace = null,
  servicesOnline,
  servicesTotal,
  onWorkspaceLoaded,
}: LiveWorkspaceProps) {
  const [workspace, setWorkspace] = useState<LiveWorkspaceView | null>(
    initialWorkspace,
  );
  const [documentContent, setDocumentContent] = useState(() =>
    JSON.stringify(createCallwrightWorkspaceRun(), null, 2),
  );
  const [documentFormat, setDocumentFormat] =
    useState<WorkspaceDocumentFormat>("json");
  const [sourceName, setSourceName] = useState("");
  const [sourcePreview, setSourcePreview] =
    useState<WorkspaceSourcePreview | null>(null);
  const [extractedDraft, setExtractedDraft] = useState(false);
  const [extractionWarnings, setExtractionWarnings] = useState<
    readonly string[]
  >([]);
  const [draftReviewed, setDraftReviewed] = useState(false);
  const [approvalToken, setApprovalToken] = useState("");
  const [changeContent, setChangeContent] = useState(() =>
    initialWorkspace
      ? JSON.stringify(initialChangeDocument(initialWorkspace), null, 2)
      : "",
  );
  const [planContent, setPlanContent] = useState(() =>
    initialWorkspace
      ? JSON.stringify(correctedPlanDocument(initialWorkspace), null, 2)
      : "",
  );
  const [busy, setBusy] = useState(false);
  const [fileReading, setFileReading] = useState(false);
  const [error, setError] = useState<ActionError>({
    message: "",
    issues: [],
  });
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [localUpdate, setLocalUpdate] =
    useState<WorkspaceLiveUpdate | null>(null);
  const requestRef = useRef<AbortController | null>(null);
  const fileReadIdRef = useRef(0);
  const focusAfterActionRef = useRef(false);
  const changeInitializedRef = useRef(initialWorkspace?.id ?? "");
  const planInitializedRef = useRef(
    initialWorkspace?.status === "initial-grant-rejected"
      ? initialWorkspace.id
      : "",
  );

  useEffect(() => {
    return () => requestRef.current?.abort();
  }, []);

  useEffect(() => {
    const previewUrl = sourcePreview?.url;
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    };
  }, [sourcePreview?.url]);

  useEffect(() => {
    if (!focusAfterActionRef.current) return;
    focusAfterActionRef.current = false;
    const frame = window.requestAnimationFrame(() => {
      document.getElementById("workspace-stage-title")?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [workspace?.status]);

  useEffect(() => {
    if (
      workspace?.status === "authorized" &&
      changeInitializedRef.current !== workspace.id
    ) {
      setChangeContent(
        JSON.stringify(initialChangeDocument(workspace), null, 2),
      );
      changeInitializedRef.current = workspace.id;
    }
  }, [workspace]);

  useEffect(() => {
    if (
      workspace?.status === "initial-grant-rejected" &&
      planInitializedRef.current !== workspace.id
    ) {
      setPlanContent(
        JSON.stringify(correctedPlanDocument(workspace), null, 2),
      );
      planInitializedRef.current = workspace.id;
    }
  }, [workspace]);

  const parsedDocument = useMemo(() => {
    try {
      return parseWorkspaceDocument(documentContent, documentFormat);
    } catch {
      return {};
    }
  }, [documentContent, documentFormat]);
  const readiness = useMemo(
    () => workspaceReadiness(parsedDocument),
    [parsedDocument],
  );

  const runAction = useCallback(
    async (
      action: (signal: AbortSignal) => Promise<LiveWorkspaceView>,
      options: { updateUrl?: boolean } = {},
    ) => {
      if (requestRef.current) return;
      const controller = new AbortController();
      requestRef.current = controller;
      setBusy(true);
      setError({ message: "", issues: [] });
      setLocalUpdate(null);
      try {
        const next = await action(controller.signal);
        controller.signal.throwIfAborted();
        focusAfterActionRef.current = true;
        setWorkspace(next);
        if (options.updateUrl) onWorkspaceLoaded?.(next.id);
      } catch (caught) {
        if (!controller.signal.aborted) {
          const nextError = actionError(caught);
          setError(nextError);
          setLocalUpdate({
            title: "This step could not finish",
            detail: nextError.message,
            tone: "negative",
          });
          window.requestAnimationFrame(() => {
            document.querySelector<HTMLElement>(".lw-validation")?.focus();
          });
        }
      } finally {
        if (requestRef.current === controller) {
          requestRef.current = null;
          setBusy(false);
        }
      }
    },
    [onWorkspaceLoaded],
  );

  const importWorkspace = useCallback(() => {
    void runAction(
      async (signal) => {
        const parsed = parseWorkspaceDocument(
          documentContent,
          documentFormat,
        );
        const document =
          extractedDraft && draftReviewed
            ? confirmExtractedWorkspaceDraft(parsed)
            : parsed;
        const result = await importWorkspaceWithConflictRetry(
          client,
          document,
          { signal },
        );
        if (result.retried) {
          setDocumentContent(
            serializeWorkspaceDocument(
              result.document,
              documentFormat,
            ),
          );
          setLocalUpdate({
            title: "A fresh run ID was assigned",
            detail: `The existing record was preserved. This run continues as ${result.document.id}.`,
            tone: "positive",
          });
        }
        setSourcePreview(null);
        return result.workspace;
      },
      { updateUrl: true },
    );
  }, [
    client,
    documentContent,
    documentFormat,
    draftReviewed,
    extractedDraft,
    runAction,
  ]);

  const acceptFile = useCallback((file: File) => {
    if (file.size > 10_000_000) {
      setError({
        message: "Choose a supported file smaller than 10 MB.",
        issues: [],
      });
      setLocalUpdate({
        title: "File not accepted",
        detail:
          "Choose a JSON, YAML, PDF, Word, Markdown, text, PNG, JPEG, or WebP file smaller than 10 MB.",
        tone: "negative",
      });
      return;
    }
    const suffix = file.name.split(".").pop()?.toLowerCase();
    const structured = ["yaml", "yml", "json"].includes(suffix ?? "");
    if (!structured && !isSupportedDocument(file.name)) {
      setError({
        message: "Choose a supported workspace or document file.",
        issues: [],
      });
      setLocalUpdate({
        title: "File not accepted",
        detail:
          "Supported formats: JSON, YAML, PDF, DOCX, Markdown, text, PNG, JPEG, and WebP.",
        tone: "negative",
      });
      return;
    }
    const readId = fileReadIdRef.current + 1;
    fileReadIdRef.current = readId;
    setSourcePreview(
      suffix === "pdf"
        ? { kind: "pdf", url: URL.createObjectURL(file) }
        : isImageDocument(file.name)
          ? { kind: "image", url: URL.createObjectURL(file) }
          : null,
    );
    setFileReading(true);
    setSourceName(file.name);
    setDocumentContent("");
    setDocumentFormat("json");
    setExtractedDraft(false);
    setExtractionWarnings([]);
    setDraftReviewed(false);
    setError({ message: "", issues: [] });
    setLocalUpdate({
      title: `Reading ${file.name}`,
      detail: "The file is being read locally. Nothing has been uploaded yet.",
    });
    let lastOcrUpdate = "";
    void (structured
      ? file
          .text()
          .then((text) => ({ text, extractionConfidence: undefined }))
      : readDocumentText(file, ({ status, progress }) => {
          if (
            fileReadIdRef.current !== readId ||
            !isImageDocument(file.name)
          ) {
            return;
          }
          const percent = Math.round(progress * 100);
          const updateKey = `${status}:${Math.floor(percent / 10)}`;
          if (updateKey === lastOcrUpdate) return;
          lastOcrUpdate = updateKey;
          setLocalUpdate({
            title: `Reading text from ${file.name}`,
            detail: `${status.replaceAll("_", " ")}${percent > 0 ? ` · ${percent}%` : ""}. The image stays in your browser.`,
          });
        }))
      .then(({ text, extractionConfidence }) => {
        if (fileReadIdRef.current !== readId) return;
        try {
          if (structured) {
            const format = suffix === "json" ? "json" : "yaml";
            const prepared = prepareWorkspaceDocumentRun(text, format);
            setDocumentFormat(format);
            setDocumentContent(prepared.content);
            setExtractedDraft(false);
            setExtractionWarnings([]);
            setLocalUpdate({
              title: `${file.name} is ready`,
              detail: `Fresh run ID: ${prepared.document.id}. Review the file if needed, then choose Validate and continue.`,
              tone: "positive",
            });
          } else {
            const extracted = extractWorkspaceDraft(
              text,
              file.name,
              undefined,
              extractionConfidence,
            );
            setDocumentFormat("json");
            setDocumentContent(
              serializeWorkspaceDocument(extracted.document, "json"),
            );
            setExtractedDraft(true);
            setExtractionWarnings(extracted.warnings);
            setLocalUpdate({
              title: `Draft extracted from ${file.name}`,
              detail:
                "Nothing is approved yet. Review the extracted decision, ticket, tasks, scopes, and plan before continuing.",
              tone: "positive",
            });
          }
        } catch (caught) {
          const nextError = actionError(caught);
          setError(nextError);
          setLocalUpdate({
            title: `${file.name} could not be prepared`,
            detail: nextError.message,
            tone: "negative",
          });
        }
      })
      .catch((caught) => {
        if (fileReadIdRef.current !== readId) return;
        const nextError = actionError(caught);
        setError({
          message: nextError.message,
          issues: nextError.issues,
        });
        setLocalUpdate({
          title: "The file could not be read",
          detail: `${nextError.message} Try choosing ${file.name} again or use the starter JSON.`,
          tone: "negative",
        });
      })
      .finally(() => {
        if (fileReadIdRef.current === readId) {
          setFileReading(false);
        }
      });
  }, []);

  const guide = workspaceGuide(workspace?.status);

  const downloadTemplate = useCallback(() => {
    downloadJson(
      "writai-voyagr-callwright-workspace.json",
      CALLWRIGHT_SAMPLE_WORKSPACE,
    );
    setLocalUpdate({
      title: "VOYAGR demo JSON downloaded",
      detail:
        "The file includes the old 7:00 PM call plan, the upstream 8:30 PM change, a preserved sibling task, and the Callwright action.",
      tone: "positive",
    });
  }, []);

  const downloadDocumentTemplate = useCallback(() => {
    downloadText(
      "writai-project-template.md",
      DOCUMENT_TEMPLATE,
      "text/markdown",
    );
    setLocalUpdate({
      title: "Project document template downloaded",
      detail:
        "Replace the example sections with your decision, ticket, tasks, plan, and approver role, then upload the Markdown file.",
      tone: "positive",
    });
  }, []);

  const downloadReport = useCallback(() => {
    if (!workspace) return;
    downloadJson(
      `${workspace.id}-verification-report.json`,
      workspaceVerificationReport(workspace),
    );
    setLocalUpdate({
      title: "Verification report downloaded",
      detail:
        "The report contains the outcome, provenance path, and activity history without secret tokens.",
      tone: "positive",
    });
  }, [workspace]);

  return (
    <AppShell
      activeView="workspace"
      surface="live-workspace"
      onNavigate={(view) => {
        window.location.href =
          view === "report" ? "/scenario-lab?view=report" : "/scenario-lab";
      }}
      navigationDisabled={busy || fileReading}
      graphSnapshot={workspace?.graphVersion}
      servicesOnline={servicesOnline}
      servicesTotal={servicesTotal}
    >
      <article className="sl-page lw-page" aria-labelledby="live-workspace-title">
        <header className="lw-heading">
          <div>
            <h1 id="live-workspace-title">Live Workspace</h1>
            <p>
              {workspace
                ? workspace.name
                : "Start with the VOYAGR Callwright proof, or upload your own decisions, tasks, and agent plan."}
            </p>
          </div>
          {workspace ? (
            <div className="lw-heading__status" aria-label="Workspace status">
              <code>{workspace.graphVersion}</code>
              <span
                className={
                  workspace.status === "complete" ? "is-positive" : ""
                }
              >
                {workspace.status === "complete"
                  ? "Verified"
                  : guide.stateLabel}
              </span>
            </div>
          ) : null}
        </header>

        <WorkspaceStageRail status={workspace?.status} />
        <WorkspaceGuide guide={guide} busy={busy || fileReading} />

        <div className="lw-workspace-layout">
          <div className="lw-workspace-action">
            {!workspace ? (
              <WorkspaceImportForm
                content={documentContent}
                sourceName={sourceName}
                format={documentFormat}
                readiness={readiness}
                sourcePreview={sourcePreview}
                extractedDraft={extractedDraft}
                extractionWarnings={extractionWarnings}
                draftReviewed={draftReviewed}
                busy={busy}
                fileReading={fileReading}
                errorMessage={error.message}
                validationIssues={error.issues}
                onContentChange={(content) => {
                  const next = editWorkspaceDocument(
                    {
                      content: documentContent,
                      format: documentFormat,
                    },
                    content,
                  );
                  setDocumentContent(next.content);
                  setDocumentFormat(next.format);
                  if (extractedDraft) setDraftReviewed(false);
                }}
                onDraftReviewedChange={setDraftReviewed}
                onFile={acceptFile}
                onSubmit={importWorkspace}
                onDownloadTemplate={downloadTemplate}
                onDownloadDocumentTemplate={downloadDocumentTemplate}
                onDismissError={() =>
                  setError({ message: "", issues: [] })
                }
              />
            ) : (
              <>
                <ValidationSummary
                  message={error.message}
                  issues={error.issues}
                  onDismiss={() =>
                    setError({ message: "", issues: [] })
                  }
                />
                <WorkspaceBeforeEvidence
                  workspace={workspace}
                  titleId="workspace-before-evidence-title"
                />
                <WorkspaceSupervisor
                  workspace={workspace}
                  titleId="workspace-supervisor-title"
                />
                {workspace.status === "imported" ? (
                  <WorkspaceBaseline
                    workspace={workspace}
                    approvalToken={approvalToken}
                    busy={busy}
                    onApprovalTokenChange={setApprovalToken}
                    onApprove={() =>
                      void runAction(async (signal) => {
                        const proposalFingerprint =
                          workspace.baselineProposalFingerprint;
                        const proposalInstanceId =
                          workspace.baselineProposalInstanceId;
                        if (!proposalFingerprint || !proposalInstanceId) {
                          throw new LiveWorkspaceApiError(
                            "The baseline approval binding is unavailable.",
                          );
                        }
                        const next = await client.approveBaseline(
                          workspace.id,
                          {
                            approvalToken,
                            proposalFingerprint,
                            proposalInstanceId,
                          },
                          { signal },
                        );
                        setApprovalToken("");
                        return next;
                      })
                    }
                  />
                ) : null}
                {workspace.status === "baseline-approved" ? (
                  <WorkspaceAuthorization
                    workspace={workspace}
                    busy={busy}
                    onAuthorize={() =>
                      void runAction((signal) =>
                        client.authorizePlan(workspace.id, { signal }),
                      )
                    }
                  />
                ) : null}
                {["authorized", "change-proposed", "change-applied"].includes(
                  workspace.status,
                ) ? (
                  <WorkspaceChange
                    workspace={workspace}
                    content={changeContent}
                    approvalToken={approvalToken}
                    busy={busy}
                    onContentChange={setChangeContent}
                    onApprovalTokenChange={setApprovalToken}
                    onPropose={() =>
                      void runAction(async (signal) => {
                        const mutation = parseWorkspaceDocument(
                          changeContent,
                          "json",
                        ) as unknown as Record<string, unknown>;
                        return client.proposeChange(workspace.id, mutation, {
                          signal,
                        });
                      })
                    }
                    onCancel={() =>
                      void runAction((signal) =>
                        client.cancelPendingChange(workspace.id, { signal }),
                      )
                    }
                    onApprove={() =>
                      void runAction(async (signal) => {
                        const decisionId =
                          workspace.pendingMutation?.decision.id;
                        if (!decisionId) {
                          throw new LiveWorkspaceApiError(
                            "The workspace has no pending decision proposal.",
                          );
                        }
                        const proposalFingerprint =
                          workspace.pendingProposalFingerprint;
                        const proposalInstanceId =
                          workspace.pendingProposalInstanceId;
                        if (!proposalFingerprint || !proposalInstanceId) {
                          throw new LiveWorkspaceApiError(
                            "The decision approval binding is unavailable.",
                          );
                        }
                        const next = await client.approveChange(
                          workspace.id,
                          decisionId,
                          {
                            approvalToken,
                            proposalFingerprint,
                            proposalInstanceId,
                          },
                          { signal },
                        );
                        setApprovalToken("");
                        return next;
                      })
                    }
                    onVerify={() =>
                      void runAction((signal) =>
                        client.verifyInitialGrant(workspace.id, { signal }),
                      )
                    }
                  />
                ) : null}
                {IMPACT_STATUSES.has(workspace.status) ? (
                  <WorkspaceImpact
                    workspace={workspace}
                    planContent={planContent}
                    busy={busy}
                    evidenceOpen={evidenceOpen}
                    onPlanContentChange={setPlanContent}
                    onToggleEvidence={() => {
                      const next = !evidenceOpen;
                      setEvidenceOpen(next);
                      window.requestAnimationFrame(() => {
                        document
                          .getElementById(
                            next
                              ? "workspace-technical-evidence"
                              : "workspace-evidence-toggle",
                          )
                          ?.focus();
                      });
                    }}
                    onDownloadReport={downloadReport}
                    onSaveAndReauthorize={() =>
                      void runAction(async (signal) => {
                        const plan = parseWorkspaceDocument(
                          planContent,
                          "json",
                        ) as unknown as Record<string, unknown>;
                        const updated = await client.updatePlan(
                          workspace.id,
                          plan,
                          { signal },
                        );
                        if (!signal.aborted) setWorkspace(updated);
                        return client.reauthorize(workspace.id, { signal });
                      })
                    }
                    onReauthorize={() =>
                      void runAction((signal) =>
                        client.reauthorize(workspace.id, { signal }),
                      )
                    }
                    onVerifyReplacement={() =>
                      void runAction((signal) =>
                        client.verifyReplacementGrant(workspace.id, {
                          signal,
                        }),
                      )
                    }
                  />
                ) : null}
              </>
            )}
          </div>

          <WorkspaceActivity
            events={workspace?.history ?? []}
            busy={busy}
            busyMessage={guide.busyMessage}
            localUpdate={localUpdate}
          />
        </div>
      </article>
    </AppShell>
  );
}
