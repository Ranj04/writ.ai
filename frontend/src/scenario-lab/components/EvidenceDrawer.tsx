import { useEffect, useId, useRef } from "react";
import type { EvidenceEntry, GrantView } from "../model";

function EvidenceGrant({
  grant,
  label,
}: {
  grant: GrantView;
  label: string;
}) {
  return (
    <section className="sl-evidence-group">
      <h3>{label}</h3>
      <dl className="sl-evidence-list">
        <div>
          <dt>Authorization ID</dt>
          <dd><code>{grant.id}</code></dd>
        </div>
        <div>
          <dt>Agent</dt>
          <dd>Dragback agent service</dd>
        </div>
        {grant.runId ? (
          <div>
            <dt>Run binding</dt>
            <dd><code>{grant.runId}</code></dd>
          </div>
        ) : null}
        {grant.taskId ? (
          <div>
            <dt>Task binding</dt>
            <dd><code>{grant.taskId}</code></dd>
          </div>
        ) : null}
        <div>
          <dt>Snapshot</dt>
          <dd><code>{grant.graphSnapshot}</code></dd>
        </div>
        <div>
          <dt>Plan</dt>
          <dd><code>{grant.planId}</code></dd>
        </div>
        <div>
          <dt>Scope</dt>
          <dd>{grant.scope.join(", ") || "None"}</dd>
        </div>
        <div>
          <dt>Allowed tasks</dt>
          <dd>{grant.allowedTaskIds?.join(", ") || "No task references"}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>{grant.status}</dd>
        </div>
        {grant.verificationCode ? (
          <div>
            <dt>Executor verification</dt>
            <dd><code>{grant.verificationCode}</code></dd>
          </div>
        ) : null}
        {grant.issuedAt ? (
          <div>
            <dt>Issued</dt>
            <dd>{grant.issuedAt}</dd>
          </div>
        ) : null}
        {grant.planHash ? (
          <div>
            <dt>Plan hash</dt>
            <dd><code>{grant.planHash}</code></dd>
          </div>
        ) : null}
        {grant.expiresAt ? (
          <div>
            <dt>Expires</dt>
            <dd>{grant.expiresAt}</dd>
          </div>
        ) : null}
      </dl>
    </section>
  );
}

export interface EvidenceDrawerProps {
  open: boolean;
  title?: string;
  entries: readonly EvidenceEntry[];
  originalGrant?: GrantView;
  replacementGrant?: GrantView;
  onClose: () => void;
}

export function EvidenceDrawer({
  open,
  title = "Technical evidence",
  entries,
  originalGrant,
  replacementGrant,
  onClose,
}: EvidenceDrawerProps) {
  const titleId = useId();
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    const layer = drawerRef.current?.closest(".sl-drawer-layer");
    const root = layer?.closest(".sl-root");
    const backgroundElements = [
      ...(root?.querySelectorAll<HTMLElement>(":scope > .sl-header") ?? []),
      ...Array.from(layer?.parentElement?.children ?? []).filter(
        (element): element is HTMLElement =>
          element instanceof HTMLElement && element !== layer,
      ),
    ];
    const backgroundState = backgroundElements.map((element) => ({
      element,
      inert: element.hasAttribute("inert"),
      ariaHidden: element.getAttribute("aria-hidden"),
    }));
    document.body.style.overflow = "hidden";
    backgroundElements.forEach((element) => {
      element.setAttribute("inert", "");
      element.setAttribute("aria-hidden", "true");
    });
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab" || !drawerRef.current) return;
      const focusable = Array.from(
        drawerRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hasAttribute("hidden"));
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    closeButtonRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = previousOverflow;
      backgroundState.forEach(({ element, inert, ariaHidden }) => {
        if (!inert) element.removeAttribute("inert");
        if (ariaHidden === null) element.removeAttribute("aria-hidden");
        else element.setAttribute("aria-hidden", ariaHidden);
      });
      previouslyFocused?.focus();
    };
  }, [onClose, open]);

  if (!open) return null;

  return (
    <div
      className="sl-drawer-layer"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <aside
        ref={drawerRef}
        className="sl-evidence-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <header>
          <div>
            <h2 id={titleId}>{title}</h2>
            <p>Raw authority output and snapshot-bound grant metadata.</p>
          </div>
          <button
            ref={closeButtonRef}
            className="sl-icon-button"
            type="button"
            onClick={onClose}
            aria-label="Close technical evidence"
          >
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <path d="m5 5 10 10m0-10L5 15" />
            </svg>
          </button>
        </header>

        <div className="sl-evidence-drawer__body">
          {entries.length > 0 ? (
            <section className="sl-evidence-group">
              <h3>Run evidence</h3>
              <dl className="sl-evidence-list">
                {entries.map((entry, index) => (
                  <div key={`${entry.label}-${index}`}>
                    <dt>{entry.label}</dt>
                    <dd>
                      {entry.kind === "code" ? (
                        <code>{entry.value}</code>
                      ) : (
                        entry.value
                      )}
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
          ) : (
            <div className="sl-empty-state sl-empty-state--compact">
              <p>No technical evidence has been returned yet.</p>
            </div>
          )}
          {originalGrant ? (
            <EvidenceGrant grant={originalGrant} label="Original grant" />
          ) : null}
          {replacementGrant ? (
            <EvidenceGrant grant={replacementGrant} label="Replacement grant" />
          ) : null}
        </div>
      </aside>
    </div>
  );
}
