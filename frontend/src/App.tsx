import { ApprovalsRoute } from "./approvals/ApprovalsRoute";
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
  // NO HexclaveProvider here, deliberately, and this was learned the hard way.
  //
  // Mounting `<HexclaveProvider>` with a configured project id HANGS the
  // renderer: the tab stopped responding to screenshots and then to navigation
  // itself. `/approvals` is a screen we fall back TO when the live demo breaks,
  // so it must never become the thing that breaks — a hang is worse than a
  // white screen, because it takes the whole tab with it.
  //
  // We do not need the provider. It exists to supply React context to Hexclave
  // UI components, and we render none: the only thing this surface wants is a
  // token, which `hexclaveApprovalToken()` fetches directly and defensively.
  if (route === "approvals") return <ApprovalsRoute />;
  return route === "examples" ? <ScenarioLabRoute /> : <LiveWorkspaceRoute />;
}
