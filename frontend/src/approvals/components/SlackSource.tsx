import type { ChangeSource } from "../model";
import { sourceCaption } from "../model";

/**
 * The message exactly as it was posted. It is rendered verbatim because the
 * whole claim is that nobody had to write a ticket for this to reach the work.
 */
export function SlackSource({
  source,
  label = "Where it came from",
}: {
  source: ChangeSource;
  label?: string;
}) {
  return (
    <div className="ap-sect">
      <p className="ap-label">{label}</p>
      <div className="ap-slack">
        <div className="ap-slack-avatar">{source.authorInitials}</div>
        <div>
          <div className="ap-slack-who">
            <strong>{source.author}</strong>
            <span>{source.timestamp}</span>
          </div>
          <p className="ap-slack-msg">{source.text}</p>
        </div>
      </div>
      <p className="ap-chan">{sourceCaption(source)}</p>
    </div>
  );
}
