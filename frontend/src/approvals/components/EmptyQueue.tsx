import type { DataSource } from "../model";
import { SourceBadge } from "./SourceBadge";

/**
 * Nothing is awaiting approval.
 *
 * The wording is deliberately narrow. An empty queue means Dragback is holding
 * no change for approval — it does NOT mean every decision was applied, which
 * would be a claim about state this screen never read.
 */
export function EmptyQueue({ dataSource }: { dataSource: DataSource }) {
  return (
    <div className="ap-empty">
      <div className="ap-eyebrows">
        <span className="ap-eyebrow">
          <i className="ap-dot" />
          Nothing waiting
        </span>
        <SourceBadge source={dataSource} />
      </div>
      <h1>No pending changes</h1>
      <p>Dragback is not holding any change for approval right now.</p>
    </div>
  );
}
