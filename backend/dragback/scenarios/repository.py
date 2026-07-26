from __future__ import annotations

from threading import RLock
from typing import Protocol

from dragback.scenarios.run_models import ScenarioRunSummary


class ScenarioRunRepository(Protocol):
    def save(self, summary: ScenarioRunSummary) -> None: ...

    def list_latest(self) -> list[ScenarioRunSummary]: ...


class InMemoryScenarioRunRepository:
    """Replaceable, process-local result storage for the prototype."""

    def __init__(self) -> None:
        self._latest: dict[str, ScenarioRunSummary] = {}
        self._lock = RLock()

    def save(self, summary: ScenarioRunSummary) -> None:
        with self._lock:
            self._latest[summary.scenario_id] = summary.model_copy(deep=True)

    def list_latest(self) -> list[ScenarioRunSummary]:
        with self._lock:
            return [
                summary.model_copy(deep=True)
                for summary in sorted(
                    self._latest.values(),
                    key=lambda item: item.completed_at,
                    reverse=True,
                )
            ]
