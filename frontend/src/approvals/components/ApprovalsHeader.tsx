import {
  hexclaveSignInEnabled,
  redirectToHexclaveSignIn,
} from "../../hexclave/client";

/**
 * The app chrome, reproduced locally for this route.
 *
 * `/` and `/scenario-lab` both render `AppShell` from `scenario-lab/components/`.
 * This route keeps its own header on purpose: the lane brief requires it to be
 * self-contained, and `AppShell` takes an `onNavigate` callback for the
 * scenario-lab views that this route does not own.
 *
 * The reachability half of that trade-off IS now closed. `AppShellView` gained
 * an `"approvals"` member and `AppShell` gained an `/approvals` nav link at
 * integration, so the two fallback demo routes link here; this header links
 * back. Both are plain anchors, so neither shell renders the other's page.
 *
 * What is reproduced here is the visual contract only: same sticky full-bleed
 * bar, same 84px min-height and horizontal padding, same wordmark scale, same
 * nav-link treatment and 2px active underline. Because the header is full-bleed
 * in both places, the chrome lines up across all three routes even though the
 * content columns below it differ.
 */
export function ApprovalsHeader({ view }: { view: "approvals" | "why" }) {
  return (
    <header className="ap-header">
      <a className="ap-wordmark" href="/">
        writ.ai
      </a>

      <nav className="ap-header__nav" aria-label="Primary navigation">
        <a className="ap-nav-link" href="/">
          Workspace
        </a>
        <a className="ap-nav-link" href="/scenario-lab">
          Examples
        </a>
        <a
          className="ap-nav-link"
          href="/approvals"
          aria-current={view === "approvals" ? "page" : undefined}
        >
          Approvals
        </a>
        <a
          className="ap-nav-link"
          href="/approvals/why"
          aria-current={view === "why" ? "page" : undefined}
        >
          Why
        </a>
      </nav>

      <div className="ap-header__utilities">
        {hexclaveSignInEnabled() ? (
          <button
            type="button"
            className="ap-header__signin"
            onClick={() => {
              void redirectToHexclaveSignIn();
            }}
          >
            Sign in with Hexclave
          </button>
        ) : null}
      </div>
    </header>
  );
}
