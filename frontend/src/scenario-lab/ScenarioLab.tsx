import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ScenarioFiltersValue,
  ScenarioLabClient,
  ScenarioLabData,
  ScenarioNarrativeStepId,
  ScenarioRunState,
  ScenarioRunSummary,
} from "./model";
import {
  DEFAULT_SCENARIO_FILTERS,
  narrativeStepForRun,
} from "./utils";
import {
  AppShell,
  type AppShellAction,
  type ScenarioLabView,
} from "./components/AppShell";
import { EvidenceDrawer } from "./components/EvidenceDrawer";
import type { EvidenceSection } from "./components/EvidenceWorkspace";
import { RunReport } from "./components/RunReport";
import { ScenarioCatalog } from "./components/ScenarioCatalog";
import type { ScenarioDetailLayer } from "./components/ScenarioLayerNav";
import { ScenarioRunView } from "./components/ScenarioRunView";
import "./scenario-lab.css";

export interface ScenarioLabProps {
  data: ScenarioLabData;
  client: ScenarioLabClient;
  initialScenarioId?: string;
  initialView?: ScenarioLabView;
  initialRun?: ScenarioRunState | null;
  initialDetailLayer?: ScenarioDetailLayer;
}

function messageFromError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function scenarioLabUrl({
  view,
  scenarioId,
  runId,
  detailLayer = "story",
}: {
  view: ScenarioLabView;
  scenarioId?: string;
  runId?: string;
  detailLayer?: ScenarioDetailLayer;
}): URL {
  const url = new URL(window.location.href);
  if (view === "run" && scenarioId) {
    url.searchParams.set("scenario", scenarioId);
    url.searchParams.delete("view");
    if (runId) url.searchParams.set("run", runId);
    else url.searchParams.delete("run");
    if (detailLayer === "evidence" || detailLayer === "graph") {
      url.searchParams.set("layer", detailLayer);
    } else url.searchParams.delete("layer");
    return url;
  }
  url.searchParams.delete("scenario");
  url.searchParams.delete("layer");
  url.searchParams.delete("run");
  if (view === "report") url.searchParams.set("view", "report");
  else url.searchParams.delete("view");
  return url;
}

export function ScenarioLab({
  data,
  client,
  initialScenarioId,
  initialView = initialScenarioId ? "run" : "catalog",
  initialRun = null,
  initialDetailLayer = "story",
}: ScenarioLabProps) {
  const [view, setView] = useState<ScenarioLabView>(initialView);
  const [selectedScenarioId, setSelectedScenarioId] = useState(
    initialScenarioId ?? data.scenarios[0]?.id ?? "",
  );
  const [filters, setFilters] = useState<ScenarioFiltersValue>(
    DEFAULT_SCENARIO_FILTERS,
  );
  const [catalogSelection, setCatalogSelection] = useState(
    initialScenarioId ?? data.scenarios[0]?.id ?? "",
  );
  const [run, setRun] = useState<ScenarioRunState | null>(initialRun);
  const [runReturnView, setRunReturnView] = useState<"catalog" | "report">(
    "catalog",
  );
  const [runs, setRuns] = useState<readonly ScenarioRunSummary[]>(
    data.runs ?? [],
  );
  const [unavailableRunIds, setUnavailableRunIds] = useState<
    ReadonlySet<string>
  >(() => new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [detailLayer, setDetailLayer] =
    useState<ScenarioDetailLayer>(initialDetailLayer);
  const [evidenceReturnLayer, setEvidenceReturnLayer] = useState<
    "story" | "graph"
  >("story");
  const [evidenceSection, setEvidenceSection] =
    useState<EvidenceSection>(null);
  const [impactRevealed, setImpactRevealed] = useState(
    initialRun?.activeStage === "decision-changed",
  );
  const [runAllProgress, setRunAllProgress] = useState<{
    completed: number;
    total: number;
  } | null>(null);
  const requestRef = useRef<AbortController | null>(null);
  const pendingFocusIdRef = useRef<string | null>(null);

  useEffect(() => {
    setRuns(data.runs ?? []);
  }, [data.runs]);

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "auto" });
  }, [selectedScenarioId, view]);

  useEffect(() => {
    const focusId = pendingFocusIdRef.current;
    if (!focusId) return;
    pendingFocusIdRef.current = null;
    const frame = window.requestAnimationFrame(() => {
      document.getElementById(focusId)?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [selectedScenarioId, view]);

  useEffect(() => {
    const url = scenarioLabUrl({
      view,
      scenarioId: selectedScenarioId,
      runId: run?.runId,
      detailLayer,
    });
    window.history.replaceState(null, "", url);
  }, [detailLayer, run?.runId, selectedScenarioId, view]);

  useEffect(() => {
    const handlePopState = () => window.location.reload();
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    return () => requestRef.current?.abort();
  }, []);

  const scenarios = useMemo(() => {
    const latestByScenario = new Map(
      runs.map((result) => [result.scenarioId, result]),
    );
    return data.scenarios.map((scenario) => {
      const latest = latestByScenario.get(scenario.id);
      return latest
        ? {
            ...scenario,
            lastResult: latest.status,
            lastRunAt: latest.completedAt,
          }
        : scenario;
    });
  }, [data.scenarios, runs]);

  const selectedScenario = scenarios.find(
    (scenario) => scenario.id === selectedScenarioId,
  );

  const beginRequest = useCallback(() => {
    if (requestRef.current) return null;
    const controller = new AbortController();
    requestRef.current = controller;
    setBusy(true);
    setError("");
    return controller;
  }, []);

  const finishRequest = useCallback((controller: AbortController) => {
    if (requestRef.current === controller) {
      requestRef.current = null;
      setBusy(false);
    }
  }, []);

  const pushRoute = useCallback(
    (
      nextView: ScenarioLabView,
      scenarioId?: string,
      runId?: string,
      nextDetailLayer: ScenarioDetailLayer = "story",
    ) => {
      const url = scenarioLabUrl({
        view: nextView,
        scenarioId,
        runId,
        detailLayer: nextDetailLayer,
      });
      window.history.pushState(null, "", url);
    },
    [],
  );

  const closeEvidence = useCallback(() => {
    setEvidenceOpen(false);
  }, []);

  const openScenario = useCallback(
    (scenarioId: string) => {
      if (requestRef.current) return;
      pendingFocusIdRef.current = "scenario-run-title";
      pushRoute("run", scenarioId);
      setSelectedScenarioId(scenarioId);
      setRun(null);
      setRunReturnView("catalog");
      setEvidenceOpen(false);
      setDetailLayer("story");
      setEvidenceSection(null);
      setImpactRevealed(false);
      setView("run");
    },
    [pushRoute],
  );

  const startScenario = useCallback(async () => {
    if (!selectedScenario) return;
    const controller = beginRequest();
    if (!controller) return;
    setImpactRevealed(false);
    setEvidenceSection(null);
    try {
      const next = await client.startScenario(selectedScenario.id, {
        signal: controller.signal,
      });
      if (!controller.signal.aborted) setRun(next);
    } catch (caught) {
      if (!controller.signal.aborted) {
        setError(messageFromError(caught));
      }
    } finally {
      finishRequest(controller);
    }
  }, [beginRequest, client, finishRequest, selectedScenario]);

  const advanceScenario = useCallback(async () => {
    if (!run) return;
    const controller = beginRequest();
    if (!controller) return;
    try {
      const next = await client.advanceScenario(
        run.runId,
        run.activeStage,
        {
          signal: controller.signal,
        },
      );
      if (!controller.signal.aborted) {
        setRun(next);
        setImpactRevealed(false);
        setUnavailableRunIds((current) => {
          const updated = new Set(current);
          updated.delete(next.runId);
          return updated;
        });
      }
      if (
        !controller.signal.aborted &&
        next.status !== "running" &&
        client.loadRunSummaries
      ) {
        const summaries = await client.loadRunSummaries({
          signal: controller.signal,
        });
        if (!controller.signal.aborted) setRuns(summaries);
      }
    } catch (caught) {
      if (!controller.signal.aborted) {
        setError(messageFromError(caught));
      }
    } finally {
      finishRequest(controller);
    }
  }, [beginRequest, client, finishRequest, run]);

  const resetScenario = useCallback(async () => {
    if (!selectedScenario) return;
    const controller = beginRequest();
    if (!controller) return;
    setImpactRevealed(false);
    try {
      const next = await client.resetScenario(selectedScenario.id, {
        signal: controller.signal,
      });
      if (!controller.signal.aborted) {
        setRun(next);
        setEvidenceOpen(false);
        setEvidenceSection(null);
      }
    } catch (caught) {
      if (!controller.signal.aborted) setError(messageFromError(caught));
    } finally {
      finishRequest(controller);
    }
  }, [beginRequest, client, finishRequest, selectedScenario]);

  const runAll = useCallback(async () => {
    const controller = beginRequest();
    if (!controller) return;
    setImpactRevealed(false);
    setRunAllProgress(null);
    setRuns([]);
    setUnavailableRunIds(new Set());
    if (view !== "report") pendingFocusIdRef.current = "run-report-title";
    if (view !== "report") pushRoute("report");
    setView("report");
    try {
      const nextRuns = await client.runAllScenarios(
        scenarios.map((scenario) => scenario.id),
        {
          signal: controller.signal,
        },
      );
      if (!controller.signal.aborted) {
        setRuns(nextRuns);
        setRunAllProgress({
          completed: nextRuns.length,
          total: scenarios.length,
        });
      }
    } catch (caught) {
      if (!controller.signal.aborted) {
        setRunAllProgress(null);
        setError(messageFromError(caught));
      }
    } finally {
      finishRequest(controller);
    }
  }, [beginRequest, client, finishRequest, pushRoute, scenarios, view]);

  const inspectRun = useCallback(
    async (runId: string, scenarioId: string) => {
      if (!client.loadRunState) {
        setUnavailableRunIds((current) => new Set(current).add(runId));
        setError("Detailed state is unavailable for this run.");
        return;
      }
      const controller = beginRequest();
      if (!controller) return;
      try {
        const loaded = await client.loadRunState(runId, scenarioId, {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        if (!loaded) {
          setUnavailableRunIds((current) => new Set(current).add(runId));
          setError("Detailed state is unavailable for this run.");
          return;
        }
        setSelectedScenarioId(scenarioId);
        setRun(loaded);
        setRunReturnView("report");
        setEvidenceOpen(false);
        setDetailLayer("story");
        setEvidenceSection(null);
        setImpactRevealed(loaded.activeStage === "decision-changed");
        pendingFocusIdRef.current = "scenario-run-title";
        pushRoute("run", scenarioId, runId);
        setView("run");
      } catch (caught) {
        if (!controller.signal.aborted) setError(messageFromError(caught));
      } finally {
        finishRequest(controller);
      }
    },
    [beginRequest, client, finishRequest, pushRoute],
  );

  const narrativeStep = useMemo<ScenarioNarrativeStepId>(
    () => narrativeStepForRun(run, impactRevealed),
    [impactRevealed, run],
  );

  const primaryAction = useMemo<AppShellAction | undefined>(() => {
    if (view !== "run" || !selectedScenario) return undefined;
    if (!run) {
      return {
        label: "Start with authorized work",
        onClick: startScenario,
        disabled: busy,
        busy,
      };
    }
    if (run.status === "running") {
      if (run.activeStage === "decision-changed" && !impactRevealed) {
        return {
          label: "Show affected work",
          onClick: () => setImpactRevealed(true),
          disabled: busy,
          busy: false,
        };
      }
      return {
        label:
          run.activeStage === "authorized"
            ? "Approve the change"
            : run.activeStage === "decision-changed"
              ? "Check old authorization"
              : "Correct the plan",
        onClick: advanceScenario,
        disabled: busy,
        busy,
      };
    }
    return {
      label: "Start over",
      onClick: resetScenario,
      disabled: busy,
      busy,
    };
  }, [
    advanceScenario,
    busy,
    impactRevealed,
    resetScenario,
    run,
    selectedScenario,
    startScenario,
    view,
  ]);

  const navigate = useCallback(
    (next: "catalog" | "report") => {
      if (requestRef.current) return;
      if (view !== next) {
        pendingFocusIdRef.current =
          next === "report" ? "run-report-title" : "scenario-catalog-title";
        pushRoute(next);
      }
      setError("");
      setEvidenceOpen(false);
      setDetailLayer("story");
      setEvidenceSection(null);
      setImpactRevealed(false);
      setView(next);
    },
    [pushRoute, view],
  );

  return (
    <AppShell
      activeView={view}
      onNavigate={navigate}
      navigationDisabled={busy}
      graphSnapshot={run?.graphSnapshot ?? data.graphSnapshot}
      servicesOnline={data.servicesOnline}
      servicesTotal={data.servicesTotal}
    >
      {error ? (
        <div className="sl-error-banner" role="alert">
          <strong>Scenario action failed.</strong>
          <span>{error}</span>
          <button type="button" onClick={() => setError("")}>
            Dismiss
          </button>
        </div>
      ) : null}

      {view === "catalog" ? (
        <ScenarioCatalog
          scenarios={scenarios}
          filters={filters}
          onFiltersChange={setFilters}
          selectedScenarioId={catalogSelection}
          onSelectedChange={(scenarioId) =>
            setCatalogSelection((current) =>
              current === scenarioId ? "" : scenarioId,
            )
          }
          onOpen={openScenario}
          onRunAll={runAll}
          runAllBusy={busy}
        />
      ) : null}

      {view === "run" && selectedScenario ? (
        <ScenarioRunView
          scenario={selectedScenario}
          run={run}
          narrativeStep={narrativeStep}
          busy={busy}
          onBack={() => navigate(runReturnView)}
          backLabel={
            runReturnView === "report" ? "Run report" : "All scenarios"
          }
          onReset={resetScenario}
          onOpenEvidence={() => setEvidenceOpen(true)}
          detailLayer={detailLayer}
          evidenceReturnLayer={evidenceReturnLayer}
          evidenceSection={evidenceSection}
          onDetailLayerChange={(layer) => {
            if (layer === "evidence" && detailLayer !== "evidence") {
              setEvidenceReturnLayer(
                detailLayer === "graph" ? "graph" : "story",
              );
            }
            if (layer !== detailLayer) {
              pushRoute("run", selectedScenario.id, run?.runId, layer);
            }
            setDetailLayer(layer);
            if (layer === "story") setEvidenceSection(null);
          }}
          primaryAction={primaryAction}
        />
      ) : null}

      {view === "run" && !selectedScenario ? (
        <div className="sl-page sl-empty-state">
          <h1>Scenario unavailable.</h1>
          <p>Select another scenario from the catalog.</p>
          <button
            className="sl-button sl-button--primary"
            type="button"
            onClick={() => navigate("catalog")}
          >
            View scenarios
          </button>
        </div>
      ) : null}

      {view === "report" ? (
        <RunReport
          runs={runs}
          onInspect={inspectRun}
          onRunAll={runAll}
          runAllBusy={busy}
          unavailableRunIds={unavailableRunIds}
          progress={runAllProgress}
        />
      ) : null}

      <EvidenceDrawer
        open={evidenceOpen}
        entries={run?.evidence ?? []}
        originalGrant={run?.originalGrant}
        replacementGrant={run?.replacementGrant}
        onClose={closeEvidence}
      />
    </AppShell>
  );
}
