import { Suspense } from "react";
import { HexclaveProvider, HexclaveTheme } from "@hexclave/react";
import { ApprovalsRoute } from "./approvals/ApprovalsRoute";
import { hexclaveClient } from "./hexclave/client";
import { LiveWorkspaceRoute } from "./live-workspace/LiveWorkspaceRoute";
import { ScenarioLabRoute } from "./scenario-lab/ScenarioLabRoute";

export type WritaiRoute = "workspace" | "examples" | "approvals";

export function routeForPath(pathname: string): WritaiRoute {
  if (pathname.startsWith("/approvals")) return "approvals";
  return pathname.startsWith("/scenario-lab") ? "examples" : "workspace";
}

export default function App() {
  const route = routeForPath(window.location.pathname);
  // The Hexclave provider wraps ONLY `/approvals`. That is the surface that
  // needs an identity, and `/` and `/scenario-lab` are the fallback demo
  // routes: they must keep rendering with no provider, no sign-in and no
  // network, because they are what we fall back TO when something breaks.
  if (route === "approvals") {
    // No Hexclave project configured? Render the screen WITHOUT the provider.
    // It then has no identity, which the screen already handles by labelling
    // every approval a rehearsal. Mounting a provider that cannot initialise
    // would white-screen the route instead, which is strictly worse than an
    // honest "nothing was applied".
    const app = hexclaveClient();
    if (app === null) return <ApprovalsRoute />;
    return (
      <Suspense fallback={<div className="ap-page">Loading…</div>}>
        <HexclaveProvider app={app}>
          <HexclaveTheme>
            <ApprovalsRoute />
          </HexclaveTheme>
        </HexclaveProvider>
      </Suspense>
    );
  }
  return route === "examples" ? <ScenarioLabRoute /> : <LiveWorkspaceRoute />;
}
