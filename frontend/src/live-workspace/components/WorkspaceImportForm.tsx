import type { FormEvent } from "react";
import type {
  WorkspaceDocumentFormat,
  WorkspaceValidationIssue,
} from "../model";
import type { WorkspaceReadiness } from "../state";
import { CodeDocumentEditor } from "./CodeDocumentEditor";
import { ValidationSummary } from "./ValidationSummary";
import { WorkspaceRequirements } from "./WorkspaceRequirements";

export interface WorkspaceSourcePreview {
  kind: "image" | "pdf";
  url: string;
}

export interface WorkspaceImportFormProps {
  content: string;
  sourceName: string;
  format: WorkspaceDocumentFormat;
  readiness: WorkspaceReadiness;
  sourcePreview?: WorkspaceSourcePreview | null;
  extractedDraft: boolean;
  extractionWarnings: readonly string[];
  draftReviewed: boolean;
  busy: boolean;
  fileReading: boolean;
  errorMessage: string;
  validationIssues: readonly WorkspaceValidationIssue[];
  onContentChange: (content: string) => void;
  onDraftReviewedChange: (reviewed: boolean) => void;
  onFile: (file: File) => void;
  onSubmit: () => void;
  onDownloadTemplate: () => void;
  onDownloadDocumentTemplate: () => void;
  onDismissError: () => void;
}

export function WorkspaceImportForm({
  content,
  sourceName,
  format,
  readiness,
  sourcePreview = null,
  extractedDraft,
  extractionWarnings,
  draftReviewed,
  busy,
  fileReading,
  errorMessage,
  validationIssues,
  onContentChange,
  onDraftReviewedChange,
  onFile,
  onSubmit,
  onDownloadTemplate,
  onDownloadDocumentTemplate,
  onDismissError,
}: WorkspaceImportFormProps) {
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit();
  };
  const acceptFile = (file: File | undefined) => {
    if (file) onFile(file);
  };

  return (
    <>
      <ValidationSummary
        message={errorMessage}
        issues={validationIssues}
        onDismiss={onDismissError}
      />
      <form
        className="lw-import-form"
        aria-labelledby="workspace-import-title"
        onSubmit={submit}
        noValidate
      >
        <div className="lw-section-heading">
          <div>
            <h2 id="workspace-import-title">Choose a workspace file</h2>
            <p>
              Use JSON, YAML, PDF, Word, Markdown, text, or a screenshot up to
              10 MB. The file is read locally first.
            </p>
          </div>
          <span>
            {sourceName
              ? `${sourceName} · fresh run ID assigned`
              : "Starter example · fresh ID each run"}
          </span>
        </div>
        <label
          className="lw-dropzone"
          htmlFor="workspace-file"
          onDragOver={(event) => {
            if (!busy && !fileReading) event.preventDefault();
          }}
          onDrop={(event) => {
            event.preventDefault();
            if (!busy && !fileReading) {
              acceptFile(event.dataTransfer.files[0]);
            }
          }}
        >
          <input
            className="sl-visually-hidden"
            id="workspace-file"
            type="file"
            accept=".yaml,.yml,.json,.pdf,.docx,.md,.markdown,.txt,.png,.jpg,.jpeg,.webp,application/json,text/yaml,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/markdown,text/plain,image/png,image/jpeg,image/webp"
            disabled={busy || fileReading}
            onClick={(event) => {
              event.currentTarget.value = "";
            }}
            onChange={(event) => acceptFile(event.target.files?.[0])}
          />
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 16V4m0 0L8 8m4-4 4 4M5 15v4h14v-4" />
          </svg>
          <strong>
            {sourceName ? sourceName : "Upload a project file"}
          </strong>
          <span>
            {sourceName
              ? "File loaded. Drop another file here to replace it."
              : "Drag and drop here, or click to choose a file."}
          </span>
        </label>

        {sourcePreview ? (
          <section
            className="lw-source-preview"
            aria-labelledby="workspace-source-preview-title"
          >
            <div className="lw-source-preview__heading">
              <div>
                <strong id="workspace-source-preview-title">
                  Original {sourcePreview.kind === "pdf" ? "PDF" : "image"}
                </strong>
                <span>
                  Compare this local preview with the extracted draft below.
                </span>
              </div>
              <small>Not uploaded yet</small>
            </div>
            {sourcePreview.kind === "pdf" ? (
              <object
                className="lw-source-preview__document"
                data={sourcePreview.url}
                type="application/pdf"
                aria-label={`Preview of ${sourceName}`}
              >
                <p>
                  This browser cannot display the PDF preview. You can still
                  review the extracted draft below.
                </p>
              </object>
            ) : (
              <img
                className="lw-source-preview__image"
                src={sourcePreview.url}
                alt={`Preview of ${sourceName}`}
              />
            )}
          </section>
        ) : null}

        {extractedDraft ? (
          <section className="lw-extraction-review" aria-live="polite">
            <strong>Document converted to a reviewable draft</strong>
            <p>
              writ.ai extracted a proposed decision, ticket, tasks, scopes,
              and agent plan. This file is treated as a workspace snapshot;
              its baseline evidence and initial plan appear as “Before the new
              decision” on the next screen. Screenshot text is read with local
              OCR. writ.ai cannot approve this draft automatically.
            </p>
            {extractionWarnings.length > 0 ? (
              <ul>
                {extractionWarnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            ) : null}
            <label className="lw-extraction-review__confirmation">
              <input
                type="checkbox"
                checked={draftReviewed}
                disabled={busy || fileReading}
                onChange={(event) =>
                  onDraftReviewedChange(event.currentTarget.checked)
                }
              />
              <span>
                I reviewed the extracted decision, ticket, tasks, scopes, and
                plan.
              </span>
            </label>
          </section>
        ) : null}

        <div className="lw-import-actions">
          <button
            className="sl-button sl-button--primary lw-import-submit"
            type="submit"
            disabled={
              busy ||
              fileReading ||
              !readiness.ready ||
              (extractedDraft && !draftReviewed)
            }
          >
            {fileReading
              ? "Reading file…"
              : busy
                ? "Validating and importing…"
                : extractedDraft
                  ? "Review draft and continue"
                  : "Validate and continue"}
          </button>
          <button
            className="sl-button sl-button--secondary"
            type="button"
            disabled={busy || fileReading}
            onClick={onDownloadTemplate}
          >
            Download VOYAGR demo JSON
          </button>
          <button
            className="sl-button sl-button--secondary"
            type="button"
            disabled={busy || fileReading}
            onClick={onDownloadDocumentTemplate}
          >
            Download document template
          </button>
        </div>

        <details className="lw-disclosure">
          <summary>Review or edit the workspace document</summary>
          <p>
            {extractedDraft
              ? "This is the draft extracted from your document. Confirm every field before continuing."
              : "This is the exact document writ.ai will validate. Advanced users can edit it before continuing."}
          </p>
          <CodeDocumentEditor
            id="workspace-document"
            label={format === "yaml" ? "Workspace YAML" : "Workspace JSON"}
            value={content}
            onChange={onContentChange}
            disabled={busy || fileReading}
          />
        </details>

        <details className="lw-disclosure">
          <summary>File checklist</summary>
          <WorkspaceRequirements readiness={readiness} />
        </details>
      </form>
    </>
  );
}
