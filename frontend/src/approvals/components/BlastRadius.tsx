import type { BlastRadius as BlastRadiusData, Person } from "../model";
import { faceColour, inWords, totalSessions } from "../model";

function capitalise(word: string): string {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

function PauseGlyph() {
  return (
    <span className="ap-glyph">
      <svg viewBox="0 0 10 10" aria-hidden="true">
        <rect x="1" y="1" width="2.6" height="8" fill="#bc5a09" />
        <rect x="6.4" y="1" width="2.6" height="8" fill="#bc5a09" />
      </svg>
    </span>
  );
}

function Face({ person, paused }: { person: Person; paused: boolean }) {
  return (
    <div className="ap-person">
      <div className="ap-face" style={{ background: faceColour(person) }}>
        {person.initials}
        {paused ? <PauseGlyph /> : null}
      </div>
      <div>
        <b>{person.name}</b>
        <span>{person.taskId}</span>
      </div>
    </div>
  );
}

/**
 * Who this touches.
 *
 * `radius` arrives from the server, which computed it from
 * `SupervisorInterruptPort.preview()`. This component counts the two lists it
 * was given and draws faces. It never works out who is affected itself.
 */
export function BlastRadius({
  radius,
  applied,
  rehearsal = false,
  unresolved = false,
}: {
  radius: BlastRadiusData;
  applied: boolean;
  /** True when nothing was actually applied — see ASSUMPTIONS.md A2. */
  rehearsal?: boolean;
  /** True when the approval was sent but its outcome could not be confirmed. */
  unresolved?: boolean;
}) {
  const stopping = radius.interrupted.length;
  const continuing = radius.preserved.length;
  const total = totalSessions(radius);

  return (
    <div className="ap-sect">
      <p className="ap-label">Who this touches</p>
      <div className="ap-hero">
        <div className="ap-big">
          {!applied
            ? `${stopping} of ${total}`
            : rehearsal
              ? `${stopping} would stop`
              : `${stopping} stopped`}
        </div>
        <p>
          {unresolved
            ? `The approval was sent, but its outcome is not confirmed. Up to ${inWords(stopping)} ${stopping === 1 ? "session may" : "sessions may"} have been interrupted; ${inWords(continuing)} may have continued unchanged.`
            : applied && rehearsal
            ? `Nothing was applied — no session was interrupted. ${capitalise(inWords(continuing))} ${continuing === 1 ? "would keep" : "would keep"} working.`
            : applied
              ? `Each received the new constraint and what it already built that still stands. ${capitalise(inWords(continuing))} ${continuing === 1 ? "was" : "were"} never touched.`
              : `active ${total === 1 ? "session" : "sessions"} will be interrupted and redirected. ${capitalise(inWords(continuing))} ${continuing === 1 ? "keeps" : "keep"} working.`}
        </p>
      </div>
      <div className="ap-people">
        <div className="ap-col ap-col--stop">
          <h4>
            {unresolved ? "Potentially redirected" : "Stopping"}{" "}
            <span className="ap-pill">{stopping}</span>
          </h4>
          {radius.interrupted.map((person) => (
            <Face key={person.assignmentId} person={person} paused />
          ))}
        </div>
        <div className="ap-col ap-col--go">
          <h4>
            {unresolved ? "Expected to continue" : "Continuing"}{" "}
            <span className="ap-pill">{continuing}</span>
          </h4>
          {radius.preserved.map((person) => (
            <Face key={person.assignmentId} person={person} paused={false} />
          ))}
        </div>
      </div>
    </div>
  );
}
