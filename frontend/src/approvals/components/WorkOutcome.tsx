import type { PendingChange } from "../model";
import { workOn } from "../model";

function Column({
  kind,
  heading,
  items,
}: {
  kind: "invalidated" | "preserved";
  heading: string;
  items: ReturnType<typeof workOn>;
}) {
  return (
    <div className={`ap-work-col ap-work-col--${kind}`}>
      <h4>
        {heading} <span className="ap-pill">{items.length}</span>
      </h4>
      {items.map((item) => (
        <div key={item.taskId} className="ap-work-item">
          <b>{item.title}</b>
          <span>
            {item.people.map((person) => person.name).join(" · ")}
            {item.detail ? ` — ${item.detail}` : ""}
          </span>
        </div>
      ))}
    </div>
  );
}

/**
 * Work invalidated and work preserved, side by side and weighted the same.
 *
 * The preserved column is the difference between "my agent got hijacked" and
 * "my agent got corrected", so it is not a footnote to the other one.
 */
export function WorkOutcome({ change }: { change: PendingChange }) {
  const invalidated = workOn(change, change.blastRadius.interrupted);
  const preserved = workOn(change, change.blastRadius.preserved);
  return (
    <div className="ap-sect">
      <p className="ap-label">What this did to the work</p>
      <div className="ap-work">
        <Column
          kind="invalidated"
          heading="Needs redoing"
          items={invalidated}
        />
        <Column kind="preserved" heading="Still stands" items={preserved} />
      </div>
    </div>
  );
}
