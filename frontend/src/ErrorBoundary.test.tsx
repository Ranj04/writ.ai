// @vitest-environment jsdom
//
// The only test in this suite that renders into a DOM. Everything else uses
// `renderToStaticMarkup`, which cannot exercise an error boundary: React's
// server renderer rethrows instead of falling back. jsdom is scoped to this
// file on purpose so the rest of the suite keeps its DOM-free environment.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "./ErrorBoundary";

function Thrower(): never {
  throw new Error("render exploded");
}

describe("ErrorBoundary", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("replaces a throwing subtree with the no-approval fallback", () => {
    // React reports caught errors on console.error; keep the test output
    // readable without hiding the assertion below.
    vi.spyOn(console, "error").mockImplementation(() => {});

    render(
      <ErrorBoundary>
        <p>sibling content that must vanish</p>
        <Thrower />
      </ErrorBoundary>,
    );

    const fallback = screen.getByRole("alert");
    expect(fallback.textContent).toContain("No approval was submitted");
    expect(fallback.textContent).toContain("reload the page");
    expect(fallback.textContent).toContain("render exploded");
    expect(screen.queryByText("sibling content that must vanish")).toBeNull();
  });

  it("renders its children untouched when nothing throws", () => {
    render(
      <ErrorBoundary>
        <p>healthy content</p>
      </ErrorBoundary>,
    );

    expect(screen.getByText("healthy content")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
