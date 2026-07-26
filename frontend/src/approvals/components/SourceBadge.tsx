import type { DataSource } from "../model";

/**
 * Real versus simulated, stated on the screen rather than in a console line.
 * It stays visible after approval so a replayed payload is never mistaken for a
 * live one.
 */
export function SourceBadge({ source }: { source: DataSource }) {
  return (
    <span className={`ap-eyebrow ap-source-badge ap-source-badge--${source}`}>
      {source === "live" ? "live data" : "fixture data"}
    </span>
  );
}
