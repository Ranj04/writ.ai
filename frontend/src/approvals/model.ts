/**
 * Approval-screen data model.
 *
 * Every field below is produced by the server. `blastRadius` in particular is
 * computed server-side from `SupervisorInterruptPort.preview()`. The browser
 * never works out who is affected by intersecting scopes itself: that is a
 * permission decision, and permission decisions do not happen in a browser.
 *
 * The helpers at the bottom of this file format server-supplied values. None of
 * them decides anything.
 */

export type Person = {
  assignmentId: string;
  name: string;
  initials: string;
  taskId: string;
};

export type ChangeSource = {
  channel: string;
  author: string;
  authorInitials: string;
  timestamp: string;
  text: string;
};

export type DecisionDelta = {
  id: string;
  supersedes: string;
  scope: string;
  was: string;
  now: string;
};

export type ProvenanceNode = {
  id: string;
  title: string;
  detail: string;
  affected: boolean;
};

export type BlastRadius = {
  interrupted: Person[];
  preserved: Person[];
};

export type PendingChange = {
  id: string;
  source: ChangeSource;
  decision: DecisionDelta;
  provenancePath: ProvenanceNode[];
  blastRadius: BlastRadius;
  approverPermission: string;
};

/** What the server reports actually happened once the change was applied. */
export type ApprovalReceipt = {
  changeId: string;
  interrupted: Person[];
  preserved: Person[];
};

/**
 * Where the rendered payload came from. A replayed fixture is never labelled
 * live; the badge stays on screen after approval for exactly that reason.
 */
export type DataSource = "live" | "fixture";

const APPROVAL_PREFIX = /^\s*approved\s*[—–\-:,]?\s*/i;
const EFFECTIVE_CLAUSE = /,\s*effective\b[^.]*/i;
const SENTENCE_END = /[.!?]/;
const HEADLINE_MAX = 90;

const ARTIFACT_REFERENCE = /\b(?:DEC|SPEC|TICKET|TASK|PLAN|RUN|PR)-\d+\b/;
const SLACK_MENTION = /<[@!#][^>]+>|(?:^|[\s(])@[\w.-]+/;

/**
 * The headline shown above the card, derived from the approved message text.
 *
 * This is formatting, not extraction: the message itself is rendered verbatim
 * lower down, and nothing here changes what is approved or who it touches. When
 * the message does not read like a decision sentence, fall back to the delta the
 * server already computed.
 */
export function headlineFor(change: PendingChange): string {
  const withoutPrefix = change.source.text.trim().replace(APPROVAL_PREFIX, "");
  const terminator = withoutPrefix.search(SENTENCE_END);
  const firstSentence =
    terminator === -1 ? withoutPrefix : withoutPrefix.slice(0, terminator);
  const headline = firstSentence
    .replace(EFFECTIVE_CLAUSE, "")
    .replace(/[\s.,;:]+$/, "")
    .trim();

  if (!headline) {
    return `${change.decision.scope} → ${change.decision.now}`;
  }
  return truncate(headline.charAt(0).toUpperCase() + headline.slice(1));
}

function truncate(text: string): string {
  if (text.length <= HEADLINE_MAX) {
    return text;
  }
  const clipped = text.slice(0, HEADLINE_MAX);
  const boundary = clipped.lastIndexOf(" ");
  return `${(boundary > 40 ? clipped.slice(0, boundary) : clipped).trimEnd()}…`;
}

/**
 * The line under the Slack message: the channel, plus the two claims the demo
 * rests on — that the message names no ticket and tags nobody.
 *
 * Both claims are read off the message text rather than asserted, so a message
 * that *does* name a ticket simply drops the claim instead of lying about it.
 */
export function sourceCaption(source: ChangeSource): string {
  const parts = [`#${source.channel.replace(/^#/, "")}`];
  if (!ARTIFACT_REFERENCE.test(source.text)) {
    parts.push("no ticket referenced");
  }
  if (!SLACK_MENTION.test(source.text)) {
    parts.push("no one tagged");
  }
  return parts.join(" · ");
}

const FACE_COLOURS = [
  "#7a4fd4",
  "#0f7d8c",
  "#b04a72",
  "#207a3a",
  "#2c5fb8",
  "#8a5a12",
  "#3f6fa8",
];

/** Stable avatar colour per assignment. Decoration; carries no meaning. */
export function faceColour(person: Person): string {
  let hash = 0;
  for (let index = 0; index < person.assignmentId.length; index += 1) {
    hash = (hash * 31 + person.assignmentId.charCodeAt(index)) >>> 0;
  }
  return FACE_COLOURS[hash % FACE_COLOURS.length];
}

const NUMBER_WORDS = [
  "no",
  "one",
  "two",
  "three",
  "four",
  "five",
  "six",
  "seven",
  "eight",
  "nine",
  "ten",
];

/** Small counts read better as words in prose. Larger ones read better as digits. */
export function inWords(value: number): string {
  return value >= 0 && value < NUMBER_WORDS.length
    ? NUMBER_WORDS[value]
    : String(value);
}

export function totalSessions(radius: BlastRadius): number {
  return radius.interrupted.length + radius.preserved.length;
}

export function affectedNodes(change: PendingChange): ProvenanceNode[] {
  return change.provenancePath.filter((node) => node.affected);
}

export function unaffectedNodes(change: PendingChange): ProvenanceNode[] {
  return change.provenancePath.filter((node) => !node.affected);
}

export type WorkItem = {
  taskId: string;
  title: string;
  detail: string;
  people: Person[];
};

/**
 * The provenance nodes that are actually *work*, grouped by the two lists the
 * server sent. A task appears under invalidated or preserved because an
 * assignment on it is in `blastRadius.interrupted` or `blastRadius.preserved` —
 * never because the browser compared its scopes with anything.
 */
export function workOn(
  change: PendingChange,
  people: Person[],
): WorkItem[] {
  const byTask = new Map<string, Person[]>();
  for (const person of people) {
    const existing = byTask.get(person.taskId);
    if (existing) {
      existing.push(person);
    } else {
      byTask.set(person.taskId, [person]);
    }
  }
  return [...byTask.entries()].map(([taskId, assigned]) => {
    const node = change.provenancePath.find((entry) => entry.id === taskId);
    return {
      taskId,
      title: node?.title ?? taskId,
      detail: node?.detail ?? "",
      people: assigned,
    };
  });
}
