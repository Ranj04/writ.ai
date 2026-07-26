import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { AppShell } from "../scenario-lab/components/AppShell";
import { createLiveWorkspaceClient } from "./api";
import { LiveWorkspace } from "./LiveWorkspace";
import type {
  LiveWorkspaceClient,
  LiveWorkspaceView,
} from "./model";

type RouteState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | {
      status: "ready";
      client: LiveWorkspaceClient;
      workspace: LiveWorkspaceView | null;
      servicesOnline: number;
      servicesTotal: number;
    };

function errorMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught);
}

export function LiveWorkspaceRoute() {
  const client = useMemo(() => createLiveWorkspaceClient(), []);
  const [reloadKey, setReloadKey] = useState(0);
  const [routeState, setRouteState] = useState<RouteState>({
    status: "loading",
  });
  const requestedWorkspace = new URLSearchParams(window.location.search).get(
    "workspace",
  );

  useEffect(() => {
    const controller = new AbortController();
    setRouteState({ status: "loading" });
    async function load() {
      try {
        const [workspace, health] = await Promise.all([
          requestedWorkspace
            ? client.load(requestedWorkspace, {
                signal: controller.signal,
              })
            : Promise.resolve(null),
          api.health(controller.signal),
        ]);
        controller.signal.throwIfAborted();
        setRouteState({
          status: "ready",
          client,
          workspace,
          servicesOnline: Object.values(health).filter(Boolean).length,
          servicesTotal: Object.keys(health).length,
        });
      } catch (caught) {
        if (!controller.signal.aborted) {
          setRouteState({ status: "error", message: errorMessage(caught) });
        }
      }
    }
    void load();
    return () => controller.abort();
  }, [client, reloadKey, requestedWorkspace]);

  useEffect(() => {
    const handlePopState = () => window.location.reload();
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const navigateToWorkspace = useCallback((workspaceId: string) => {
    const url = new URL(window.location.href);
    url.searchParams.set("workspace", workspaceId);
    window.history.pushState(null, "", url);
  }, []);

  if (routeState.status === "loading") {
    return (
      <AppShell
        activeView="workspace"
        surface="live-workspace"
        onNavigate={(view) => {
          window.location.href =
            view === "report" ? "/scenario-lab?view=report" : "/scenario-lab";
        }}
      >
        <div className="sl-route-state" role="status">
          <span className="sl-route-state__indicator" aria-hidden="true" />
          <h1>Loading Live Workspace</h1>
          <p>Checking services and restoring the requested workspace.</p>
        </div>
      </AppShell>
    );
  }

  if (routeState.status === "error") {
    return (
      <AppShell
        activeView="workspace"
        surface="live-workspace"
        onNavigate={(view) => {
          window.location.href =
            view === "report" ? "/scenario-lab?view=report" : "/scenario-lab";
        }}
      >
        <div className="sl-route-state sl-route-state--error" role="alert">
          <h1>Live Workspace could not load.</h1>
          <p>{routeState.message}</p>
          <button
            className="sl-button sl-button--primary"
            type="button"
            onClick={() => setReloadKey((current) => current + 1)}
          >
            Retry
          </button>
        </div>
      </AppShell>
    );
  }

  return (
    <LiveWorkspace
      client={routeState.client}
      initialWorkspace={routeState.workspace}
      servicesOnline={routeState.servicesOnline}
      servicesTotal={routeState.servicesTotal}
      onWorkspaceLoaded={navigateToWorkspace}
    />
  );
}
