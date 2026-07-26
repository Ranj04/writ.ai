import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { ScenarioLab } from "./ScenarioLab";
import { createScenarioLabClient, loadScenarioCatalog } from "./api";
import {
  AppShell,
  type ScenarioLabView,
} from "./components/AppShell";
import type { ScenarioDetailLayer } from "./components/ScenarioLayerNav";
import type {
  ScenarioLabClient,
  ScenarioLabData,
  ScenarioRunState,
} from "./model";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | {
      status: "ready";
      data: ScenarioLabData;
      client: ScenarioLabClient;
      initialScenarioId?: string;
      initialRun?: ScenarioRunState;
      initialView: ScenarioLabView;
      initialDetailLayer: ScenarioDetailLayer;
    };

function errorMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught);
}

function waitForDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timeout);
      reject(signal.reason);
    };
    const timeout = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

async function waitForHealthyServices(
  initial: Awaited<ReturnType<typeof api.health>>,
  signal: AbortSignal,
) {
  let health = initial;
  for (let attempt = 0; attempt < 8; attempt += 1) {
    if (Object.values(health).every(Boolean)) return health;
    await waitForDelay(250, signal);
    health = await api.health(signal);
  }
  return health;
}

export function ScenarioLabRoute() {
  const [loadState, setLoadState] = useState<LoadState>({ status: "loading" });
  const [reloadKey, setReloadKey] = useState(0);
  const parameters = new URLSearchParams(window.location.search);
  const demoMode = parameters.get("demo") === "1";
  const requestedScenario = parameters.get("scenario");
  const requestedRun = parameters.get("run");
  const requestedView = parameters.get("view");
  const requestedLayer = parameters.get("layer");

  useEffect(() => {
    const controller = new AbortController();
    setLoadState({ status: "loading" });
    async function load() {
      try {
        const [catalog, initialHealth] = await Promise.all([
          loadScenarioCatalog(controller.signal),
          api.health(controller.signal),
        ]);
        const health = demoMode
          ? await waitForHealthyServices(initialHealth, controller.signal)
          : initialHealth;
        const client = createScenarioLabClient(
          catalog.scenarios,
          catalog.runs,
        );
        const initialScenarioId = demoMode
          ? "csv-exports-admin-only"
          : requestedScenario &&
              catalog.scenarios.some(
                (scenario) => scenario.id === requestedScenario,
              )
            ? requestedScenario
            : undefined;
        let initialRun: ScenarioRunState | undefined;
        if (demoMode) {
          const demoScenarioId = "csv-exports-admin-only";
          await client.resetScenario(demoScenarioId, {
            signal: controller.signal,
          });
          initialRun = await client.startScenario(demoScenarioId, {
            signal: controller.signal,
          });
        } else if (
          initialScenarioId &&
          requestedRun &&
          client.loadRunState
        ) {
          initialRun =
            (await client.loadRunState(requestedRun, initialScenarioId, {
              signal: controller.signal,
            })) ?? undefined;
        }
        controller.signal.throwIfAborted();
        const servicesOnline = Object.values(health).filter(Boolean).length;
        setLoadState({
          status: "ready",
          data: {
            scenarios: catalog.scenarios,
            runs: catalog.runs,
            graphSnapshot:
              catalog.scenarios[0]?.originalDecision.graphSnapshot,
            servicesOnline,
            servicesTotal: Object.keys(health).length,
          },
          client,
          initialScenarioId,
          initialRun,
          initialView: initialScenarioId
            ? "run"
            : requestedView === "report"
              ? "report"
              : "catalog",
          initialDetailLayer:
            requestedLayer === "evidence"
              ? "evidence"
              : requestedLayer === "graph"
                ? "graph"
                : "story",
        });
      } catch (caught) {
        if (controller.signal.aborted) return;
        setLoadState({ status: "error", message: errorMessage(caught) });
      }
    }
    void load();
    return () => controller.abort();
  }, [
    demoMode,
    reloadKey,
    requestedLayer,
    requestedRun,
    requestedScenario,
    requestedView,
  ]);

  const retry = useCallback(() => {
    setReloadKey((current) => current + 1);
  }, []);

  if (loadState.status === "loading") {
    return (
      <AppShell
        activeView="catalog"
        onNavigate={() => {
          window.location.href = "/scenario-lab";
        }}
      >
        <div className="sl-route-state" role="status">
          <span className="sl-route-state__indicator" aria-hidden="true" />
          <h1>Loading Examples</h1>
          <p>Validating the seeded examples and checking all three services.</p>
        </div>
      </AppShell>
    );
  }

  if (loadState.status === "error") {
    return (
      <AppShell
        activeView="catalog"
        onNavigate={() => {
          window.location.href = "/scenario-lab";
        }}
      >
        <div className="sl-route-state sl-route-state--error" role="alert">
          <h1>Examples could not load.</h1>
          <p>{loadState.message}</p>
          <button
            type="button"
            className="sl-button sl-button--primary"
            onClick={retry}
          >
            Retry
          </button>
        </div>
      </AppShell>
    );
  }

  return (
    <ScenarioLab
      data={loadState.data}
      client={loadState.client}
      initialScenarioId={loadState.initialScenarioId}
      initialView={loadState.initialView}
      initialRun={loadState.initialRun}
      initialDetailLayer={loadState.initialDetailLayer}
    />
  );
}
