import type { PendingChange } from "./model";

/**
 * The canonical CSV proof, distributed across five people.
 *
 * IDs follow the frozen repository fixtures (`docs/GRAPH_SCHEMA.md`):
 * `DEC-018` supersedes `DEC-004` through `SPEC-009` and `TICKET-100` to
 * `TASK-102`, while sibling `TASK-101` survives on `export.generation`.
 *
 * Assignment ids are fixture-local. Nothing reads them except `api.ts`, which
 * resolves ids returned by a live approval back onto these people, so the real
 * ids can differ without any component changing.
 */
export const FIXTURE_PENDING_CHANGE: PendingChange = {
  id: "CHANGE-DEC-018",
  source: {
    channel: "compliance",
    author: "Dana Kaur",
    authorInitials: "DK",
    timestamp: "2:41 PM",
    text: "Approved — exports must be admin-only, effective immediately.",
  },
  decision: {
    id: "DEC-018",
    supersedes: "DEC-004",
    scope: "export.authorization",
    was: "all users",
    now: "admins only",
  },
  provenancePath: [
    {
      id: "EVIDENCE-DEC-018",
      title: "Slack message",
      detail: "Dana Kaur, 2:41 PM",
      affected: true,
    },
    {
      id: "DEC-018",
      title: "DEC-018 · supersedes DEC-004",
      detail: "approved · compliance",
      affected: true,
    },
    {
      id: "SPEC-009",
      title: "Export specification",
      detail: "SPEC-009",
      affected: true,
    },
    {
      id: "TICKET-100",
      title: "Implementation ticket",
      detail: "TICKET-100",
      affected: true,
    },
    {
      id: "TASK-102",
      title: "TASK-102 · expose export to all users",
      detail: "3 sessions affected",
      affected: true,
    },
    {
      id: "TASK-101",
      title: "TASK-101 · generate CSV files",
      detail: "out of scope · unaffected",
      affected: false,
    },
  ],
  blastRadius: {
    interrupted: [
      {
        assignmentId: "ASSIGNMENT-TASK-102-PRIYA",
        name: "Priya Raman",
        initials: "PR",
        taskId: "TASK-102",
      },
      {
        assignmentId: "ASSIGNMENT-TASK-102-MARCUS",
        name: "Marcus Obi",
        initials: "MO",
        taskId: "TASK-102",
      },
      {
        assignmentId: "ASSIGNMENT-TASK-102-DAN",
        name: "Dan Levy",
        initials: "DL",
        taskId: "TASK-102",
      },
    ],
    preserved: [
      {
        assignmentId: "ASSIGNMENT-TASK-101-ANA",
        name: "Ana Silva",
        initials: "AS",
        taskId: "TASK-101",
      },
      {
        assignmentId: "ASSIGNMENT-TASK-101-JONAS",
        name: "Jonas Tan",
        initials: "JT",
        taskId: "TASK-101",
      },
    ],
  },
  approverPermission: "approve_compliance",
};

/**
 * One change. The pending queue only renders when more than one is waiting, so
 * the demo screen shows the change and nothing else.
 */
export const FIXTURE_PENDING_CHANGES: PendingChange[] = [FIXTURE_PENDING_CHANGE];
