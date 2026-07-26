from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dragback.domain import AgentRun, Artifact, DecisionMutation, Edge

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "fixtures"


def _read(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def load_graph_fixture() -> tuple[int, list[Artifact], list[Edge], AgentRun]:
    raw = _read("graph_v17.json")
    artifacts = [Artifact.model_validate(item) for item in raw["artifacts"]]
    edges = [Edge.model_validate(item) for item in raw["edges"]]
    run = AgentRun.model_validate(raw["run"])
    return int(raw["version"]), artifacts, edges, run


def load_decision_v18() -> DecisionMutation:
    return DecisionMutation.model_validate(_read("decision_v18.json"))


def load_ignored_proposal() -> DecisionMutation:
    return DecisionMutation.model_validate(_read("proposal_ignored.json"))
