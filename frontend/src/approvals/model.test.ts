import { describe, expect, it } from "vitest";
import { FIXTURE_PENDING_CHANGE } from "./fixtures";
import type { PendingChange } from "./model";
import {
  faceColour,
  headlineFor,
  sourceCaption,
  totalSessions,
  workOn,
} from "./model";

function withText(text: string): PendingChange {
  return {
    ...FIXTURE_PENDING_CHANGE,
    source: { ...FIXTURE_PENDING_CHANGE.source, text },
  };
}

describe("headlineFor", () => {
  it("reads the decision out of the approved message", () => {
    expect(headlineFor(FIXTURE_PENDING_CHANGE)).toBe(
      "Exports must be admin-only",
    );
  });

  it("handles the other ways people write an approval", () => {
    expect(headlineFor(withText("Approved: refunds need two signatures."))).toBe(
      "Refunds need two signatures",
    );
    expect(
      headlineFor(withText("approved - uploads are PDF only, effective Monday")),
    ).toBe("Uploads are PDF only");
  });

  it("keeps only the first sentence", () => {
    expect(
      headlineFor(withText("Exports must be admin-only. I will update the doc.")),
    ).toBe("Exports must be admin-only");
  });

  it("falls back to the delta the server computed when there is no sentence", () => {
    expect(headlineFor(withText("Approved."))).toBe(
      "export.authorization → admins only",
    );
  });

  it("truncates a message that would swamp the headline", () => {
    const long = `Approved — ${"policy words ".repeat(20)}`;
    const headline = headlineFor(withText(long));
    expect(headline.length).toBeLessThanOrEqual(91);
    expect(headline.endsWith("…")).toBe(true);
  });
});

describe("sourceCaption", () => {
  it("states the two claims the demo rests on", () => {
    expect(sourceCaption(FIXTURE_PENDING_CHANGE.source)).toBe(
      "#compliance · no ticket referenced · no one tagged",
    );
  });

  it("drops a claim rather than asserting it falsely", () => {
    expect(
      sourceCaption({
        ...FIXTURE_PENDING_CHANGE.source,
        text: "Approved — see TICKET-100.",
      }),
    ).toBe("#compliance · no one tagged");
    expect(
      sourceCaption({
        ...FIXTURE_PENDING_CHANGE.source,
        text: "Approved — @priya please pick this up.",
      }),
    ).toBe("#compliance · no ticket referenced");
  });

  it("does not double the channel hash", () => {
    expect(
      sourceCaption({ ...FIXTURE_PENDING_CHANGE.source, channel: "#compliance" }),
    ).toMatch(/^#compliance ·/);
  });
});

describe("faceColour", () => {
  it("is stable for the same assignment", () => {
    const person = FIXTURE_PENDING_CHANGE.blastRadius.interrupted[0];
    expect(faceColour(person)).toBe(faceColour({ ...person }));
  });
});

describe("blast radius helpers", () => {
  it("counts every session the server sent", () => {
    expect(totalSessions(FIXTURE_PENDING_CHANGE.blastRadius)).toBe(5);
  });

  it("groups work by the task the server bound each person to", () => {
    const invalidated = workOn(
      FIXTURE_PENDING_CHANGE,
      FIXTURE_PENDING_CHANGE.blastRadius.interrupted,
    );
    expect(invalidated).toHaveLength(1);
    expect(invalidated[0].taskId).toBe("TASK-102");
    expect(invalidated[0].title).toBe("TASK-102 · expose export to all users");
    expect(invalidated[0].people.map((person) => person.name)).toEqual([
      "Priya Raman",
      "Marcus Obi",
      "Dan Levy",
    ]);

    const preserved = workOn(
      FIXTURE_PENDING_CHANGE,
      FIXTURE_PENDING_CHANGE.blastRadius.preserved,
    );
    expect(preserved).toHaveLength(1);
    expect(preserved[0].taskId).toBe("TASK-101");
    expect(preserved[0].people).toHaveLength(2);
  });

  it("falls back to the task id when the path has no node for it", () => {
    const orphan = workOn(FIXTURE_PENDING_CHANGE, [
      {
        assignmentId: "ASSIGNMENT-TASK-999",
        name: "Nobody",
        initials: "NB",
        taskId: "TASK-999",
      },
    ]);
    expect(orphan[0].title).toBe("TASK-999");
  });
});
