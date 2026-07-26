import type { WorkspaceReadiness } from "../state";

const REQUIREMENTS: ReadonlyArray<{
  key: keyof Omit<WorkspaceReadiness, "ready">;
  label: string;
  detail: string;
}> = [
  {
    key: "approvedDecision",
    label: "Decision proposal",
    detail: "One or more decision proposals that define the baseline.",
  },
  {
    key: "ticketAndTasks",
    label: "Ticket and tasks",
    detail: "A ticket context and associated tasks or work items.",
  },
  {
    key: "scopedPlan",
    label: "Scoped agent plan",
    detail: "The agent plan that will be authorized and bound to the baseline.",
  },
  {
    key: "authorityRoles",
    label: "Authority roles",
    detail: "Admin roles allowed to authorize and approve changes.",
  },
];

export function WorkspaceRequirements({
  readiness,
}: {
  readiness: WorkspaceReadiness;
}) {
  return (
    <section
      className="lw-requirements"
      aria-labelledby="workspace-requirements-title"
    >
      <h2 id="workspace-requirements-title">What Dragback needs</h2>
      <dl>
        {REQUIREMENTS.map((requirement) => {
          const ready = readiness[requirement.key];
          return (
            <div key={requirement.key}>
              <dt>
                <span aria-hidden="true">{ready ? "✓" : "○"}</span>
                {requirement.label}
              </dt>
              <dd>
                {requirement.detail}
                <span className="sl-visually-hidden">
                  {ready ? " Present." : " Missing."}
                </span>
              </dd>
            </div>
          );
        })}
      </dl>
      <p
        className={
          readiness.ready
            ? "lw-readiness lw-readiness--ready"
            : "lw-readiness"
        }
        role="status"
      >
        <span aria-hidden="true">{readiness.ready ? "✓" : "○"}</span>
        {readiness.ready
          ? "File looks complete. Server validation is next."
          : "Add the missing fields before continuing."}
      </p>
    </section>
  );
}
