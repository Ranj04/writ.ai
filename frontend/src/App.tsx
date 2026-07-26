import { ApprovalsRoute } from "./approvals/ApprovalsRoute";
import { LiveWorkspaceRoute } from "./live-workspace/LiveWorkspaceRoute";
import { ScenarioLabRoute } from "./scenario-lab/ScenarioLabRoute";

export type DragbackRoute = "workspace" | "examples" | "approvals";

export function routeForPath(pathname: string): DragbackRoute {
  if (pathname.startsWith("/approvals")) return "approvals";
  return pathname.startsWith("/scenario-lab") ? "examples" : "workspace";
}

export default function App() {
  const route = routeForPath(window.location.pathname);
  if (route === "approvals") return <ApprovalsRoute />;
  return route === "examples" ? <ScenarioLabRoute /> : <LiveWorkspaceRoute />;
}
