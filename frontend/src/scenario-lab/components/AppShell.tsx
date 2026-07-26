import type { ReactNode } from "react";

export type ScenarioLabView = "catalog" | "run" | "report";
//: `"approvals"` exists so the shell can mark that tab current. `/approvals`
//: renders its own header (see `approvals/components/ApprovalsHeader.tsx`), so
//: nothing passes this value today — but a photogenic screen with no way in
//: reads as a mockup, and the nav link below is the way in.
export type AppShellView = ScenarioLabView | "workspace" | "approvals";

export interface AppShellAction {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  busy?: boolean;
}

export interface AppShellProps {
  children: ReactNode;
  activeView: AppShellView;
  onNavigate: (view: Exclude<ScenarioLabView, "run">) => void;
  navigationDisabled?: boolean;
  graphSnapshot?: string;
  servicesOnline?: number;
  servicesTotal?: number;
  primaryAction?: AppShellAction;
  surface?: "scenario-lab" | "live-workspace";
}

export function AppShell({
  children,
  activeView,
  onNavigate,
  navigationDisabled = false,
  graphSnapshot,
  servicesOnline,
  servicesTotal,
  primaryAction,
  surface = "scenario-lab",
}: AppShellProps) {
  const servicesKnown =
    typeof servicesOnline === "number" && typeof servicesTotal === "number";
  const servicesHealthy = servicesKnown && servicesOnline === servicesTotal;

  return (
    <div className={`sl-root sl-root--${surface}`}>
      <header className="sl-header">
        <a
          className="sl-wordmark"
          href="/"
          aria-label="writ.ai Workspace"
          aria-disabled={navigationDisabled || undefined}
          onClick={(event) => {
            if (navigationDisabled) event.preventDefault();
          }}
        >
          writ.ai
        </a>

        <nav className="sl-header__nav" aria-label="Primary navigation">
          <a
            className="sl-nav-link"
            href="/"
            aria-current={activeView === "workspace" ? "page" : undefined}
            aria-disabled={navigationDisabled || undefined}
            onClick={(event) => {
              if (navigationDisabled) event.preventDefault();
            }}
          >
            Workspace
          </a>
          <button
            type="button"
            className="sl-nav-link"
            aria-current={
              activeView === "catalog" ||
              activeView === "run" ||
              activeView === "report"
                ? "page"
                : undefined
            }
            onClick={() => onNavigate("catalog")}
            disabled={navigationDisabled}
          >
            Examples
          </button>
          {/*
            A plain link, not an `onNavigate` view: `/approvals` is its own
            route with its own header, and routing it through the scenario-lab
            navigator would make this shell responsible for a page it does not
            render. Adding it changes nothing about how `/` or `/scenario-lab`
            behave — it is one more anchor in the same nav.
          */}
          <a
            className="sl-nav-link"
            href="/approvals"
            aria-current={activeView === "approvals" ? "page" : undefined}
            aria-disabled={navigationDisabled || undefined}
            onClick={(event) => {
              if (navigationDisabled) event.preventDefault();
            }}
          >
            Approvals
          </a>
        </nav>

        <div className="sl-header__utilities">
          {(graphSnapshot || servicesKnown) && (
            <div className="sl-system-state" aria-label="System status">
              <span
                className={`sl-system-state__dot ${
                  servicesHealthy ? "is-online" : "is-degraded"
                }`}
                aria-hidden="true"
              />
              {servicesKnown ? (
                <span>
                  {servicesHealthy
                    ? "Services connected"
                    : `${servicesOnline}/${servicesTotal} services`}
                </span>
              ) : graphSnapshot ? (
                <span>{graphSnapshot}</span>
              ) : null}
            </div>
          )}

          <a
            className="sl-docs-link"
            href="https://github.com/Ranj04/writ.ai#readme"
            target="_blank"
            rel="noreferrer"
          >
            Docs
          </a>

          {primaryAction ? (
            <button
              className="sl-button sl-button--primary sl-header__action"
              type="button"
              onClick={primaryAction.onClick}
              disabled={primaryAction.disabled}
            >
              {primaryAction.busy ? "Working…" : primaryAction.label}
            </button>
          ) : null}
        </div>

        <details className="sl-mobile-menu">
          <summary>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M3 6h18M3 12h18M3 18h18" />
            </svg>
            <span>Menu</span>
          </summary>
          <nav aria-label="Mobile primary navigation">
            <a
              href="/"
              aria-current={activeView === "workspace" ? "page" : undefined}
              aria-disabled={navigationDisabled || undefined}
              onClick={(event) => {
                if (navigationDisabled) event.preventDefault();
              }}
            >
              Workspace
            </a>
            <button
              type="button"
              aria-current={
                activeView === "catalog" ||
                activeView === "run" ||
                activeView === "report"
                  ? "page"
                  : undefined
              }
              disabled={navigationDisabled}
              onClick={(event) => {
                onNavigate("catalog");
                event.currentTarget.closest("details")?.removeAttribute("open");
              }}
            >
              Examples
            </button>
            <a
              href="https://github.com/Ranj04/writ.ai#readme"
              target="_blank"
              rel="noreferrer"
            >
              Docs
            </a>
          </nav>
        </details>
      </header>
      <main className="sl-main">{children}</main>
    </div>
  );
}
