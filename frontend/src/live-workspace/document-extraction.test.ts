import { describe, expect, it } from "vitest";
import {
  confirmExtractedWorkspaceDraft,
  extractWorkspaceDraft,
  isSupportedDocument,
  validateImageDimensions,
} from "./document-extraction";
import { workspaceReadiness } from "./state";

const DOCUMENT = `# Refund safeguards

## Approved decision
Verified refunds may be issued automatically.

## Ticket
Automate customer refunds.

## Tasks
- Calculate the refund amount
- Verify the customer identity
- Issue the refund

## Agent plan
- Calculate the refund amount
- Verify the customer identity
- Issue the refund

## Assigned coding agent
Refund Coding Agent

## Approver role
finance-admin
`;

const JIRA_SCREENSHOT_OCR = `Software Dragback Demo Workspace

PROJECT

Backlog Build customer synchronization

Active sprint Description Details

Releases Implement a coding-agent workflow that synchronizes customer Assignee
data with the CRM. Coding Agent

Reports

Original authorization Reporter
Platform Lead
Reading and writing customer records are approved.

Priority
High
Tasks Status
Authenticate with the CRM In progress
Read customer records Dragback test change

Create customer records

The CRM integration must be
read-only.

Update customer records

Delete customer records

Fictional demo ticket - not connected to a real Jira workspace
`;

describe("document workspace extraction", () => {
  it("supports practical project-document extensions", () => {
    expect(isSupportedDocument("requirements.pdf")).toBe(true);
    expect(isSupportedDocument("brief.docx")).toBe(true);
    expect(isSupportedDocument("ticket.md")).toBe(true);
    expect(isSupportedDocument("notes.txt")).toBe(true);
    expect(isSupportedDocument("ticket-screenshot.png")).toBe(true);
    expect(isSupportedDocument("planning-board.jpeg")).toBe(true);
    expect(isSupportedDocument("requirements.webp")).toBe(true);
    expect(isSupportedDocument("archive.zip")).toBe(false);
  });

  it("converts a headed engineering document into a reviewable valid draft", () => {
    const result = extractWorkspaceDraft(
      DOCUMENT,
      "refund-requirements.md",
      "document-test",
    );

    expect(result.document.id).toBe(
      "refund-safeguards-run-document-test",
    );
    expect(result.document.baseline_decision).toMatchObject({
      approval_status: "proposal",
      authority_role: "finance-admin",
      confidence: 0.7,
    });
    expect(result.document.ticket.title).toBe(
      "Automate customer refunds.",
    );
    expect(result.document.ticket.attributes).toMatchObject({
      assigned_agent: "Refund Coding Agent",
    });
    expect(result.document.tasks).toHaveLength(3);
    expect(result.document.plan.actions).toHaveLength(3);
    expect(result.warnings).toEqual([]);
    expect(workspaceReadiness(result.document).ready).toBe(true);
  });

  it("uses explicit placeholders and warnings instead of inventing authority", () => {
    const result = extractWorkspaceDraft(
      "# Sparse project\nA paragraph without template headings.",
      "sparse.txt",
      "sparse-test",
    );

    expect(result.warnings).toHaveLength(3);
    expect(result.document.baseline_decision.approval_status).toBe(
      "proposal",
    );
    expect(result.document.tasks).toHaveLength(2);
    expect(workspaceReadiness(result.document).ready).toBe(true);
  });

  it("marks screenshot OCR as lower-confidence and review-required", () => {
    const result = extractWorkspaceDraft(
      DOCUMENT,
      "refund-ticket.png",
      "image-test",
      0.87,
    );

    expect(result.document.baseline_decision).toMatchObject({
      approval_status: "proposal",
      confidence: 0.87,
      attributes: {
        extraction: {
          extraction_confidence: 0.87,
          human_reviewed: false,
          method: "local-ocr-document-template",
          review_required: true,
        },
      },
    });
    expect(result.warnings[0]).toContain("local OCR");
  });

  it("turns the realistic Jira screenshot into ticket-specific work and a read-only change proposal", () => {
    const result = extractWorkspaceDraft(
      JIRA_SCREENSHOT_OCR,
      "01_jira_CRM-208_read_only.png",
      "jira-test",
      0.84,
    );
    const taskScopes = result.document.tasks.map((task) => task.scopes);
    const attributes = result.document.baseline_decision.attributes as {
      requirements: Record<string, Record<string, unknown>>;
      suggested_change: {
        title: string;
        text: string;
        affected_scopes: string[];
        requirements: Record<string, Record<string, unknown>>;
      };
      extraction: {
        method: string;
        extraction_confidence: number;
      };
    };

    expect(result.document.name).toBe(
      "CRM-208 · Build customer synchronization",
    );
    expect(result.document.ticket).toMatchObject({
      id: "CRM-208",
      title: "Build customer synchronization",
      text: "Implement a coding-agent workflow that synchronizes customer data with the CRM.",
      attributes: {
        source_system: "jira",
        assigned_agent: "Coding Agent",
      },
    });
    expect(result.document.baseline_decision).toMatchObject({
      title: "Reading and writing customer records are approved.",
      approval_status: "proposal",
      confidence: 0.84,
    });
    expect(result.document.tasks.map((task) => task.title)).toEqual([
      "Authenticate with the CRM",
      "Read customer records",
      "Create customer records",
      "Update customer records",
      "Delete customer records",
    ]);
    expect(taskScopes).toEqual([
      ["integration.authentication"],
      ["integration.read"],
      ["integration.write"],
      ["integration.write"],
      ["integration.write"],
    ]);
    expect(attributes.requirements).toEqual({
      "integration.authentication": { authenticated: true },
      "integration.read": { read_access: true },
      "integration.write": { write_access: true },
    });
    expect(attributes.suggested_change).toMatchObject({
      title: "CRM integration must be read-only",
      text: "The CRM integration must be read-only.",
      affected_scopes: ["integration.write"],
      requirements: {
        "integration.write": { write_access: false },
      },
    });
    expect(attributes.extraction).toEqual(
      expect.objectContaining({
        method: "local-ocr-jira-ticket",
        extraction_confidence: 0.84,
      }),
    );
    expect(result.warnings).toEqual([
      expect.stringContaining("local OCR"),
      expect.stringContaining("No authoritative approver role"),
    ]);
    expect(
      result.warnings.some((warning) => warning.includes("placeholder decision")),
    ).toBe(false);
    expect(workspaceReadiness(result.document).ready).toBe(true);
  });

  it("does not activate Jira extraction from a filename alone", () => {
    const result = extractWorkspaceDraft(
      "# Sparse Jira note\nNo structured ticket fields here.",
      "jira-note.png",
      "jira-fallback-test",
      0.8,
    );

    expect(result.document.ticket.id).toBe("TICKET-001");
    expect(result.warnings).toEqual([
      expect.stringContaining("local OCR"),
      expect.stringContaining("No Decision section"),
      expect.stringContaining("No Ticket section"),
      expect.stringContaining("No Tasks or Agent plan section"),
    ]);
  });

  it("bounds an OCR-derived workspace name to the backend limit", () => {
    const result = extractWorkspaceDraft(
      `Title
${"Customer relationship management integration ".repeat(12)}
unstructured OCR content continues without sentence punctuation`,
      "crm-ticket.png",
      "long-name-test",
      0.82,
    );

    expect(result.document.name.length).toBeLessThanOrEqual(160);
    expect(result.document.name).not.toMatch(/\s$/);
    expect(workspaceReadiness(result.document).ready).toBe(true);
  });

  it("preserves OCR confidence when a human confirms the draft", () => {
    const extracted = extractWorkspaceDraft(
      DOCUMENT,
      "refund-ticket.png",
      "review-test",
      0.61,
    );
    const confirmed = confirmExtractedWorkspaceDraft(extracted.document);

    expect(confirmed.baseline_decision).toMatchObject({
      approval_status: "proposal",
      confidence: 0.99,
      attributes: {
        extraction: {
          extraction_confidence: 0.61,
          human_reviewed: true,
          review_required: true,
        },
      },
    });
    expect(
      (
        confirmed.baseline_decision.attributes as {
          extraction: { reviewed_at: string };
        }
      ).extraction.reviewed_at,
    ).toMatch(/Z$/);
    expect(extracted.document.baseline_decision.confidence).toBe(0.61);
  });

  it("rejects oversized decoded screenshots before OCR starts", () => {
    expect(() => validateImageDimensions(4000, 4000)).not.toThrow();
    expect(() => validateImageDimensions(6000, 5000)).toThrow(
      "below 25 megapixels",
    );
    expect(() => validateImageDimensions(12_001, 100)).toThrow(
      "12,000 pixels per side",
    );
  });

  it("recognizes plain headings produced by PDF and Word text extraction", () => {
    const result = extractWorkspaceDraft(
      `Project name
Access controls
Approved decision
Production access requires review.
Ticket
Add production deployment controls.
Tasks
Add an approval check
Record the approval evidence
Approver role
security-admin`,
      "access-controls.docx",
      "plain-heading-test",
    );

    expect(result.document.name).toBe("Access controls");
    expect(result.document.ticket.title).toBe(
      "Add production deployment controls.",
    );
    expect(result.document.tasks).toHaveLength(2);
    expect(result.document.baseline_decision.authority_role).toBe(
      "security-admin",
    );
    expect(result.warnings).toEqual([]);
  });
});
