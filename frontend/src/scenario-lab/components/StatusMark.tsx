export type StatusTone = "neutral" | "positive" | "warning" | "negative";

export function StatusMark({
  tone,
  label,
}: {
  tone: StatusTone;
  label: string;
}) {
  return (
    <span className={`sl-status sl-status--${tone}`}>
      <span className="sl-status__mark" aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}

export function CheckMark({ passed }: { passed: boolean }) {
  return (
    <span
      className={`sl-check ${passed ? "sl-check--passed" : "sl-check--failed"}`}
      aria-hidden="true"
    >
      {passed ? (
        <svg viewBox="0 0 18 18">
          <path d="m4.2 9.4 3 3 6.6-6.4" />
        </svg>
      ) : (
        <svg viewBox="0 0 18 18">
          <path d="m5.3 5.3 7.4 7.4m0-7.4-7.4 7.4" />
        </svg>
      )}
    </span>
  );
}
