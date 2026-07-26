import type { WorkspaceValidationIssue } from "../model";

export function ValidationSummary({
  message,
  issues,
  onDismiss,
}: {
  message: string;
  issues: readonly WorkspaceValidationIssue[];
  onDismiss: () => void;
}) {
  if (!message) return null;
  return (
    <section className="lw-validation" role="alert" tabIndex={-1}>
      <div>
        <strong>Workspace validation failed.</strong>
        <p>{message}</p>
      </div>
      {issues.length > 0 ? (
        <ul>
          {issues.map((issue, index) => (
            <li key={`${issue.location}-${issue.type}-${index}`}>
              <code>{issue.location}</code>
              <span>{issue.type}</span>
            </li>
          ))}
        </ul>
      ) : null}
      <button type="button" onClick={onDismiss}>
        Dismiss
      </button>
    </section>
  );
}
