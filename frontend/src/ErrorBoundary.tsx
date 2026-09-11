import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * The last line between a render-time throw and a blank tab.
 *
 * `App.tsx` reasons carefully about a hang (worse than a white screen, it
 * takes the whole tab), but nothing reasoned about the white screen itself:
 * without a boundary, any component that throws during render unmounts the
 * entire tree and leaves an empty `#root`. On an approvals product that is
 * the wrong failure — a person who just clicked "approve" cannot tell whether
 * the click landed. So the fallback says exactly that: the interface failed,
 * no approval was submitted, and reloading is how to recover.
 *
 * A class, because React exposes `getDerivedStateFromError` and
 * `componentDidCatch` only on class components; there is no hook equivalent.
 *
 * This catches render, lifecycle and constructor errors in the subtree. It
 * does not catch errors inside event handlers or async code — those already
 * surface as console errors without unmounting anything.
 */
type ErrorBoundaryProps = { children: ReactNode };
type ErrorBoundaryState = { error: Error | null };

export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // One prefixed line for the operator's console, alongside React's own
    // report. Nothing is sent anywhere.
    console.error(
      `[writai/ui] interface failed to render (${error.message})`,
      info.componentStack ?? "",
    );
  }

  render(): ReactNode {
    const { error } = this.state;
    if (error === null) return this.props.children;
    return <RenderFailure error={error} />;
  }
}

function RenderFailure({ error }: { error: Error }) {
  return (
    <main className="app-failure" role="alert">
      <h1 className="app-failure__title">The writ.ai interface failed</h1>
      <p className="app-failure__lead">
        <strong>No approval was submitted.</strong> Nothing you were doing on
        this screen was sent to the server after the failure; a decision is
        only recorded when the server acknowledges it, and this page never got
        that far.
      </p>
      <p>
        To recover, reload the page. Any change still awaiting approval will be
        listed again under Approvals, and a change that was already recorded
        will show its receipt there.
      </p>
      <p className="app-failure__actions">
        <button
          type="button"
          className="app-failure__reload"
          onClick={() => window.location.reload()}
        >
          Reload the page
        </button>{" "}
        <a href="/approvals">Open Approvals</a>
      </p>
      <p className="app-failure__detail">
        <code>{error.message || error.name}</code>
      </p>
    </main>
  );
}
