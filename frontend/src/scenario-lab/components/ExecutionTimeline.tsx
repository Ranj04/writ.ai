import type { ScenarioEventView } from "../model";
import { formatCategory } from "../utils";

function formatEventTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(date);
}

export function ExecutionTimeline({
  events,
  activeLoopState,
}: {
  events: readonly ScenarioEventView[];
  activeLoopState?: string;
}) {
  return (
    <section className="sl-execution-timeline" aria-labelledby="timeline-title">
      <div className="sl-execution-timeline__heading">
        <div>
          <h2 id="timeline-title">Execution timeline</h2>
          <p>Backend events appear in committed sequence order.</p>
        </div>
        <span>{activeLoopState ? `Agent · ${activeLoopState}` : "Not started"}</span>
      </div>
      {events.length > 0 ? (
        <ol>
          {events.map((event) => (
            <li key={`${event.sequence}-${event.eventType}`}>
              <div className="sl-timeline-sequence" aria-hidden="true">
                {event.sequence}
              </div>
              <div className="sl-timeline-copy">
                <div>
                  <strong>{event.label}</strong>
                  <span>{formatCategory(event.stage)}</span>
                </div>
                <p>{event.detail}</p>
              </div>
              <time dateTime={event.createdAt}>
                {formatEventTime(event.createdAt)}
              </time>
            </li>
          ))}
        </ol>
      ) : (
        <p className="sl-execution-timeline__empty">
          Start the guided run to record authority, graph, loop, and executor
          events.
        </p>
      )}
    </section>
  );
}
