import type { WorkspaceImportDocument } from "./model";
import { createWorkspaceRun } from "./sample";
import pdfWorkerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

const DOCUMENT_SUFFIXES = new Set([
  "md",
  "markdown",
  "txt",
  "pdf",
  "docx",
  "png",
  "jpg",
  "jpeg",
  "webp",
]);

const IMAGE_SUFFIXES = new Set(["png", "jpg", "jpeg", "webp"]);

const SECTION_ALIASES: Record<string, string> = {
  project: "project",
  "project name": "project",
  title: "project",
  decision: "decision",
  "approved decision": "decision",
  "current decision": "decision",
  "current policy": "decision",
  policy: "decision",
  ticket: "ticket",
  "engineering ticket": "ticket",
  specification: "specification",
  spec: "specification",
  tasks: "tasks",
  "engineering tasks": "tasks",
  "agent plan": "plan",
  plan: "plan",
  "implementation plan": "plan",
  approver: "approver",
  "approver role": "approver",
  owner: "approver",
  agent: "agent",
  assignee: "agent",
  "assigned agent": "agent",
  "assigned coding agent": "agent",
  "coding agent": "agent",
};

interface DocumentSections {
  project: string[];
  decision: string[];
  ticket: string[];
  specification: string[];
  tasks: string[];
  plan: string[];
  approver: string[];
  agent: string[];
}

export interface ExtractedWorkspaceDraft {
  document: WorkspaceImportDocument;
  sourceText: string;
  warnings: readonly string[];
}

export interface DocumentReadProgress {
  status: string;
  progress: number;
}

export interface DocumentReadResult {
  text: string;
  extractionConfidence?: number;
}

const MAX_IMAGE_PIXELS = 25_000_000;
const MAX_IMAGE_DIMENSION = 12_000;
const MAX_WORKSPACE_NAME_LENGTH = 160;
const JIRA_TICKET_KEY_PATTERN =
  /(?:^|[^A-Z0-9])([A-Z][A-Z0-9]{1,9}-\d{1,8})(?=$|[^A-Z0-9])/;

interface JiraTaskProfile {
  title: string;
  pattern: RegExp;
  scope: string;
  requirements: Record<string, unknown>;
}

const JIRA_TASK_PROFILES: readonly JiraTaskProfile[] = [
  {
    title: "Authenticate with the CRM",
    pattern: /\bauthenticate\s+with\s+the\s+crm\b/i,
    scope: "integration.authentication",
    requirements: { authenticated: true },
  },
  {
    title: "Read customer records",
    pattern: /\bread\s+customer\s+records\b/i,
    scope: "integration.read",
    requirements: { read_access: true },
  },
  {
    title: "Create customer records",
    pattern: /\bcreate\s+customer\s+records\b/i,
    scope: "integration.write",
    requirements: { write_access: true },
  },
  {
    title: "Update customer records",
    pattern: /\bupdate\s+customer\s+records\b/i,
    scope: "integration.write",
    requirements: { write_access: true },
  },
  {
    title: "Delete customer records",
    pattern: /\bdelete\s+customer\s+records\b/i,
    scope: "integration.write",
    requirements: { write_access: true },
  },
];

export const DOCUMENT_TEMPLATE = `# Project name

## Approved decision
Describe the current company rule that governs this work.

## Ticket
Describe the engineering outcome being requested.

## Specification
Describe the requirements the implementation must satisfy.

## Tasks
- First engineering task
- Second engineering task

## Agent plan
- First action the coding agent will take
- Second action the coding agent will take

## Assigned coding agent
Coding Agent

## Approver role
engineering-admin
`;

function blankSections(): DocumentSections {
  return {
    project: [],
    decision: [],
    ticket: [],
    specification: [],
    tasks: [],
    plan: [],
    approver: [],
    agent: [],
  };
}

function cleanHeading(line: string): string {
  return line
    .replace(/^#{1,6}\s+/, "")
    .replace(/^\*{1,2}|\*{1,2}$/g, "")
    .replace(/:$/, "")
    .trim()
    .toLowerCase();
}

function sectionForLine(line: string): keyof DocumentSections | null {
  const markdownHeading = /^#{1,6}\s+/.test(line);
  const labelOnly = /^[A-Za-z][A-Za-z /_-]{1,40}:$/.test(line);
  const normalized = cleanHeading(line);
  const knownPlainHeading = normalized in SECTION_ALIASES;
  if (!markdownHeading && !labelOnly && !knownPlainHeading) return null;
  return (SECTION_ALIASES[normalized] as
    | keyof DocumentSections
    | undefined) ?? null;
}

function inlineSection(
  line: string,
): { section: keyof DocumentSections; value: string } | null {
  const match = /^([A-Za-z][A-Za-z /_-]{1,40}):\s+(.+)$/.exec(line);
  if (!match) return null;
  const section = SECTION_ALIASES[match[1].trim().toLowerCase()] as
    | keyof DocumentSections
    | undefined;
  return section ? { section, value: match[2].trim() } : null;
}

function parseSections(text: string): DocumentSections {
  const sections = blankSections();
  let current: keyof DocumentSections | null = null;
  for (const rawLine of text.replace(/\r\n?/g, "\n").split("\n")) {
    const line = rawLine.trim();
    if (!line) continue;
    const heading = sectionForLine(line);
    if (heading) {
      current = heading;
      continue;
    }
    const inline = inlineSection(line);
    if (inline) {
      current = inline.section;
      sections[current].push(inline.value);
      continue;
    }
    if (/^#\s+/.test(line) && sections.project.length === 0) {
      sections.project.push(line.replace(/^#\s+/, "").trim());
      continue;
    }
    if (current) sections[current].push(line);
  }
  return sections;
}

function stripBullet(value: string): string {
  return value
    .replace(/^[-*+]\s+/, "")
    .replace(/^\d+[.)]\s+/, "")
    .trim();
}

function meaningfulLines(values: readonly string[]): string[] {
  return values.map(stripBullet).filter((value) => value.length > 0);
}

function firstSentence(values: readonly string[], fallback: string): string {
  const text = meaningfulLines(values).join(" ").trim();
  if (!text) return fallback;
  const sentence = text.match(/^.*?[.!?](?:\s|$)/)?.[0]?.trim();
  return sentence || text;
}

function boundedText(
  value: string,
  maxLength: number,
  fallback: string,
): string {
  const normalized = value.replace(/\s+/g, " ").trim() || fallback;
  if (normalized.length <= maxLength) return normalized;
  const prefix = normalized.slice(0, maxLength - 1);
  const wordBoundary = prefix.lastIndexOf(" ");
  const safelyTruncated =
    wordBoundary >= Math.floor(maxLength * 0.6)
      ? prefix.slice(0, wordBoundary)
      : prefix;
  return `${safelyTruncated.trimEnd()}…`;
}

function slug(value: string, fallback: string): string {
  const normalized = value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 42)
    .replace(/-+$/g, "");
  return normalized || fallback;
}

function scopeForTask(task: string, index: number): string {
  const words = slug(task, `task-${index + 1}`)
    .split("-")
    .filter(Boolean)
    .slice(0, 4);
  return `work.${words.join("_") || `task_${index + 1}`}`;
}

function sourceRef(filename: string, path: string): string {
  return `document://${encodeURIComponent(filename)}/${path}`;
}

function normalizedOcrText(text: string): string {
  return text
    .replace(/\r\n?/g, "\n")
    .replace(/\bread-\s+only\b/gi, "read-only")
    .replace(/[ \t]+/g, " ")
    .trim();
}

function jiraTicketTitle(
  text: string,
  ticketKey: string,
): string | null {
  const lines = normalizedOcrText(text)
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const projectIndex = lines.findIndex(
    (line) => cleanHeading(line) === "project",
  );
  if (projectIndex < 0) return null;
  for (const candidate of lines.slice(projectIndex + 1, projectIndex + 5)) {
    const title = candidate
      .replace(/^(?:Backlog|Active sprint|Releases|Reports)\s+/i, "")
      .replace(new RegExp(`^${ticketKey}\\s*[-:·]?\\s*`, "i"), "")
      .replace(/\s+(?:Description|Details|Assignee|Reporter|Priority|Status).*$/i, "")
      .trim();
    if (
      title.length >= 3 &&
      !/^(?:Description|Details|Assignee|Reporter|Priority|Status)$/i.test(
        title,
      )
    ) {
      return title;
    }
  }
  return null;
}

function jiraNarrativeSentences(text: string): string[] {
  const withoutInterfaceLabels = normalizedOcrText(text)
    .replace(/\n+/g, " ")
    .replace(
      /\b(?:Backlog|Active sprint|Releases|Reports|Details|Assignee|Coding Agent|Reporter|Platform Lead|Priority|High|Status|In progress)\b/gi,
      " ",
    )
    .replace(/\b(?:Original authorization|writ\.ai test change)\b/gi, " ")
    .replace(/\s+/g, " ");
  return (
    withoutInterfaceLabels
      .match(/[^.!?]+[.!?]/g)
      ?.map((sentence) => sentence.trim())
      .filter(Boolean) ?? []
  );
}

function jiraTaskProfiles(text: string): JiraTaskProfile[] {
  const normalized = normalizedOcrText(text).replace(/\n+/g, " ");
  return JIRA_TASK_PROFILES.filter((profile) =>
    profile.pattern.test(normalized),
  );
}

function tryExtractJiraWorkspaceDraft(
  text: string,
  filename: string,
  runToken: string | undefined,
  sourceConfidence: number,
  imageSource: boolean,
): ExtractedWorkspaceDraft | null {
  const normalized = normalizedOcrText(text);
  const ticketKey =
    `${filename}\n${normalized}`.match(JIRA_TICKET_KEY_PATTERN)?.[1] ?? null;
  const tasks = jiraTaskProfiles(normalized);
  const hasStructure =
    /\boriginal authorization\b/i.test(normalized) &&
    /\btasks\b/i.test(normalized);
  const hasJiraSignal =
    /\bjira\b/i.test(filename) ||
    /\bjira software\b/i.test(normalized) ||
    /\bwrit\.ai test change\b/i.test(normalized);
  if (!ticketKey || !hasStructure || !hasJiraSignal || tasks.length < 2) {
    return null;
  }

  const sentences = jiraNarrativeSentences(normalized);
  const narrative = sentences.join(" ");
  const description =
    narrative.match(/\bImplement\b[^.!?]*\b(?:customer data|CRM)\b[^.!?]*[.!?]/i)
      ?.[0] ??
    "Implement the customer synchronization described in the Jira ticket.";
  const originalAuthorization =
    narrative.match(
      /\b(?:Read|Reading|Write|Writing)\b[^.!?]*\bapproved\b[^.!?]*[.!?]/i,
    )?.[0] ?? "Customer-record access is approved for this work.";
  const suggestedChangeText =
    narrative.match(
      /\bThe\s+CRM\s+integration\b[^.!?]*\bread-only\b[^.!?]*[.!?]/i,
    )?.[0] ?? null;
  const title =
    jiraTicketTitle(normalized, ticketKey) ??
    description.replace(/^Implement\s+/i, "").replace(/[.!?]$/, "");
  const assignedAgent = /\bcoding agent\b/i.test(normalized)
    ? "Coding Agent"
    : "Unassigned coding agent";
  const project = boundedText(
    `${ticketKey} · ${title}`,
    MAX_WORKSPACE_NAME_LENGTH,
    ticketKey,
  );
  const workspaceId = slug(project, "jira-workspace").slice(0, 54);
  const approver = "engineering-admin";
  const allScopes = [...new Set(tasks.map((task) => task.scope))];
  const requirements = Object.fromEntries(
    allScopes.map((scope) => [
      scope,
      {
        ...tasks.find((task) => task.scope === scope)?.requirements,
      },
    ]),
  );
  const taskArtifacts = tasks.map((task, index) => ({
    id: `${ticketKey}-TASK-${String(index + 1).padStart(2, "0")}`,
    kind: "Task",
    title: task.title,
    text: task.title,
    scopes: [task.scope],
    source_ref: sourceRef(filename, `tasks/${index + 1}`),
  }));
  const actions = tasks.map((task, index) => ({
    id: `${ticketKey}-ACTION-${String(index + 1).padStart(2, "0")}`,
    description: task.title,
    scopes: [task.scope],
    attributes: {
      task_id: taskArtifacts[index].id,
      ...task.requirements,
    },
  }));
  const suggestedChange = suggestedChangeText
    ? {
        id: `${ticketKey}-DECISION-READ-ONLY`,
        title: "CRM integration must be read-only",
        text: suggestedChangeText,
        affected_scopes: ["integration.write"],
        requirements: {
          "integration.write": { write_access: false },
        },
        source_ref: sourceRef(filename, "writai-test-change"),
      }
    : undefined;
  const base: WorkspaceImportDocument = {
    id: workspaceId,
    name: project,
    description: `Draft extracted locally from the Jira screenshot ${filename}. Review is required before approval.`,
    graph_version: 17,
    authority_policy: Object.fromEntries(
      allScopes.map((scope) => [scope, [approver]]),
    ),
    baseline_decision: {
      id: `${ticketKey}-DECISION-BASELINE`,
      kind: "Decision",
      title: originalAuthorization,
      text: originalAuthorization,
      scopes: allScopes,
      approval_status: "proposal",
      authority_role: approver,
      confidence: sourceConfidence,
      source_ref: sourceRef(filename, "original-authorization"),
      attributes: {
        requirements,
        ...(suggestedChange ? { suggested_change: suggestedChange } : {}),
        extraction: {
          method: imageSource
            ? "local-ocr-jira-ticket"
            : "deterministic-jira-ticket",
          extraction_confidence: sourceConfidence,
          source_filename: filename,
          human_reviewed: false,
          review_required: true,
        },
      },
    },
    specification: {
      id: `${ticketKey}-SPECIFICATION`,
      kind: "Specification",
      title: `${title} requirements`,
      text: description,
      scopes: allScopes,
      source_ref: sourceRef(filename, "description"),
    },
    ticket: {
      id: ticketKey,
      kind: "Ticket",
      title,
      text: description,
      scopes: allScopes,
      source_ref: sourceRef(filename, "ticket"),
      attributes: {
        source_system: "jira",
        assigned_agent: assignedAgent,
      },
    },
    tasks: taskArtifacts,
    plan: {
      id: `${ticketKey}-PLAN`,
      ticket_id: ticketKey,
      objective: `Implement ${title.replace(/^Build\s+/i, "")} under the approved customer-record access`,
      actions,
    },
  };
  const warnings = [
    ...(imageSource
      ? [
          "Text was read from the Jira image with local OCR; verify every extracted field.",
        ]
      : []),
    "No authoritative approver role was present in the ticket; engineering-admin is a demo review role.",
  ];
  return {
    document: createWorkspaceRun(base, runToken),
    sourceText: text,
    warnings,
  };
}

export function isSupportedDocument(filename: string): boolean {
  const suffix = filename.split(".").pop()?.toLowerCase() ?? "";
  return DOCUMENT_SUFFIXES.has(suffix);
}

export function isImageDocument(filename: string): boolean {
  const suffix = filename.split(".").pop()?.toLowerCase() ?? "";
  return IMAGE_SUFFIXES.has(suffix);
}

export function validateImageDimensions(
  width: number,
  height: number,
): void {
  if (
    width <= 0 ||
    height <= 0 ||
    width > MAX_IMAGE_DIMENSION ||
    height > MAX_IMAGE_DIMENSION ||
    width * height > MAX_IMAGE_PIXELS
  ) {
    throw new Error(
      "The screenshot dimensions are too large. Use an image below 25 megapixels and 12,000 pixels per side.",
    );
  }
}

async function inspectImageDimensions(file: File): Promise<void> {
  if (typeof createImageBitmap === "function") {
    const bitmap = await createImageBitmap(file);
    try {
      validateImageDimensions(bitmap.width, bitmap.height);
    } finally {
      bitmap.close();
    }
    return;
  }
  const url = URL.createObjectURL(file);
  try {
    await new Promise<void>((resolve, reject) => {
      const image = new Image();
      image.onload = () => {
        try {
          validateImageDimensions(image.naturalWidth, image.naturalHeight);
          resolve();
        } catch (caught) {
          reject(caught);
        }
      };
      image.onerror = () =>
        reject(new Error("The browser could not decode this screenshot."));
      image.src = url;
    });
  } finally {
    URL.revokeObjectURL(url);
  }
}

export async function readDocumentText(
  file: File,
  onProgress?: (progress: DocumentReadProgress) => void,
): Promise<DocumentReadResult> {
  const suffix = file.name.split(".").pop()?.toLowerCase();
  if (isImageDocument(file.name)) {
    await inspectImageDimensions(file);
    const { createWorker, OEM } = await import("tesseract.js");
    const assetBase = `${import.meta.env.BASE_URL}tesseract`;
    const worker = await createWorker("eng", OEM.LSTM_ONLY, {
      workerPath: `${assetBase}/worker.min.js`,
      corePath: `${assetBase}/core`,
      langPath: `${assetBase}/lang`,
      gzip: true,
      logger: (message) => {
        onProgress?.({
          status: message.status,
          progress:
            typeof message.progress === "number" ? message.progress : 0,
        });
      },
    });
    try {
      const result = await worker.recognize(file);
      const text = result.data.text.trim();
      if (!text) {
        throw new Error(
          "No readable text was found in the screenshot. Try a sharper image with larger text.",
        );
      }
      return {
        text,
        extractionConfidence: Math.max(
          0,
          Math.min(1, result.data.confidence / 100),
        ),
      };
    } finally {
      await worker.terminate();
    }
  }
  if (suffix === "pdf") {
    const pdfjs = await import("pdfjs-dist");
    pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
    const data = new Uint8Array(await file.arrayBuffer());
    const pdf = await pdfjs.getDocument({ data, useWorkerFetch: false }).promise;
    const pages = await Promise.all(
      Array.from({ length: pdf.numPages }, async (_, index) => {
        const page = await pdf.getPage(index + 1);
        const content = await page.getTextContent();
        const lines = new Map<number, Array<{ x: number; text: string }>>();
        for (const item of content.items) {
          if (!("str" in item) || !item.str.trim()) continue;
          const y = Math.round(item.transform[5]);
          const line = lines.get(y) ?? [];
          line.push({ x: item.transform[4], text: item.str });
          lines.set(y, line);
        }
        return [...lines.entries()]
          .sort(([left], [right]) => right - left)
          .map(([, line]) =>
            line
              .sort((left, right) => left.x - right.x)
              .map((item) => item.text)
              .join(" "),
          )
          .join("\n");
      }),
    );
    return { text: pages.join("\n") };
  }
  if (suffix === "docx") {
    const mammoth = await import("mammoth");
    const result = await mammoth.extractRawText({
      arrayBuffer: await file.arrayBuffer(),
    });
    return { text: result.value };
  }
  return { text: await file.text() };
}

export function extractWorkspaceDraft(
  text: string,
  filename: string,
  runToken?: string,
  extractionConfidence?: number,
): ExtractedWorkspaceDraft {
  const imageSource = isImageDocument(filename);
  const sourceConfidence = imageSource
    ? Math.max(0, Math.min(1, extractionConfidence ?? 0.6))
    : 0.7;
  const jiraDraft = tryExtractJiraWorkspaceDraft(
    text,
    filename,
    runToken,
    sourceConfidence,
    imageSource,
  );
  if (jiraDraft) return jiraDraft;
  const sections = parseSections(text);
  const decision = firstSentence(
    sections.decision,
    "Engineering work must follow the reviewed project requirements.",
  );
  const ticket = firstSentence(
    sections.ticket,
    "Implement the reviewed project requirements.",
  );
  const taskLines = meaningfulLines(
    sections.tasks.length > 0 ? sections.tasks : sections.plan,
  ).slice(0, 12);
  const tasks =
    taskLines.length > 0
      ? taskLines
      : ["Review the imported requirement", "Implement the approved change"];
  const planLines = meaningfulLines(
    sections.plan.length > 0 ? sections.plan : tasks,
  );
  const project = boundedText(
    meaningfulLines(sections.project)[0] ??
      filename.replace(/\.[^.]+$/, "").replace(/[-_]+/g, " "),
    MAX_WORKSPACE_NAME_LENGTH,
    "Imported project",
  );
  const approver =
    slug(firstSentence(sections.approver, "engineering-admin"), "engineering-admin");
  const assignedAgent = boundedText(
    firstSentence(sections.agent, "Coding Agent"),
    120,
    "Coding Agent",
  );
  const workspaceId = slug(project, "document-workspace").slice(0, 54);
  const taskScopes = tasks.map(scopeForTask);
  const allScopes = [...new Set(taskScopes)];
  const requirements = Object.fromEntries(
    allScopes.map((scope) => [scope, { reviewed: true }]),
  );
  const taskArtifacts = tasks.map((task, index) => ({
    id: `TASK-${String(index + 1).padStart(3, "0")}`,
    kind: "Task",
    title: task,
    text: task,
    scopes: [taskScopes[index]],
    source_ref: sourceRef(filename, `tasks/${index + 1}`),
  }));
  const actions = taskArtifacts.map((task, index) => ({
    id: `ACTION-${String(index + 1).padStart(3, "0")}`,
    description: planLines[index] ?? task.title,
    scopes: [...task.scopes],
    attributes: {
      task_id: task.id,
      reviewed: true,
    },
  }));
  const base: WorkspaceImportDocument = {
    id: workspaceId,
    name: project,
    description: `Draft extracted locally from ${filename}. Review is required before approval.`,
    graph_version: 17,
    authority_policy: Object.fromEntries(
      allScopes.map((scope) => [scope, [approver]]),
    ),
    baseline_decision: {
      id: "DEC-001",
      kind: "Decision",
      title: decision,
      text: meaningfulLines(sections.decision).join(" ") || decision,
      scopes: allScopes,
      approval_status: "proposal",
      authority_role: approver,
      confidence: sourceConfidence,
      source_ref: sourceRef(filename, "decision"),
      attributes: {
        requirements,
        extraction: {
          method: imageSource
            ? "local-ocr-document-template"
            : "deterministic-document-template",
          extraction_confidence: sourceConfidence,
          source_filename: filename,
          human_reviewed: false,
          review_required: true,
        },
      },
    },
    specification: {
      id: "SPEC-001",
      kind: "Specification",
      title: `${project} implementation requirements`,
      text:
        meaningfulLines(sections.specification).join(" ") ||
        `Implement the reviewed requirements for ${project}.`,
      scopes: allScopes,
      source_ref: sourceRef(filename, "specification"),
    },
    ticket: {
      id: "TICKET-001",
      kind: "Ticket",
      title: ticket,
      text: meaningfulLines(sections.ticket).join(" ") || ticket,
      scopes: allScopes,
      source_ref: sourceRef(filename, "ticket"),
      attributes: {
        assigned_agent: assignedAgent,
      },
    },
    tasks: taskArtifacts,
    plan: {
      id: "PLAN-001",
      ticket_id: "TICKET-001",
      objective: `Implement ${project} under the reviewed decision`,
      actions,
    },
  };
  const warnings: string[] = [];
  if (imageSource) {
    warnings.push(
      "Text was read from the image with local OCR; verify every extracted field.",
    );
  }
  if (sections.decision.length === 0) {
    warnings.push("No Decision section was found; placeholder decision text was added.");
  }
  if (sections.ticket.length === 0) {
    warnings.push("No Ticket section was found; placeholder ticket text was added.");
  }
  if (sections.tasks.length === 0 && sections.plan.length === 0) {
    warnings.push("No Tasks or Agent plan section was found; two review tasks were added.");
  }
  return {
    document: createWorkspaceRun(base, runToken),
    sourceText: text,
    warnings,
  };
}

export function confirmExtractedWorkspaceDraft(
  document: WorkspaceImportDocument,
): WorkspaceImportDocument {
  const cloned = structuredClone(document);
  const decision = cloned.baseline_decision;
  const attributes: Record<string, unknown> =
    typeof decision.attributes === "object" &&
    decision.attributes !== null &&
    !Array.isArray(decision.attributes)
      ? { ...decision.attributes }
      : {};
  const rawExtraction = attributes["extraction"];
  const extraction: Record<string, unknown> =
    typeof rawExtraction === "object" &&
    rawExtraction !== null &&
    !Array.isArray(rawExtraction)
      ? { ...rawExtraction }
      : {};
  decision.confidence = 0.99;
  decision.attributes = {
    ...attributes,
    extraction: {
      ...extraction,
      human_reviewed: true,
      reviewed_at: new Date().toISOString(),
    },
  };
  return cloned;
}
