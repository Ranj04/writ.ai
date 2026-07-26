from __future__ import annotations

from copy import deepcopy

from dragback.domain import Artifact, Edge, EdgeKind


class MemoryGraphStore:
    def __init__(self) -> None:
        self._version = 0
        self._artifacts: dict[str, Artifact] = {}
        self._edges: list[Edge] = []

    def reset(self, *, version: int, artifacts: list[Artifact], edges: list[Edge]) -> None:
        self._version = version
        self._artifacts = {item.id: deepcopy(item) for item in artifacts}
        self._edges = [deepcopy(edge) for edge in edges]

    @property
    def version(self) -> int:
        return self._version

    @property
    def version_label(self) -> str:
        return f"graph-v{self._version}"

    def increment_version(self) -> str:
        self._version += 1
        return self.version_label

    def add_artifact(self, artifact: Artifact) -> None:
        if artifact.id in self._artifacts:
            raise ValueError(f"Artifact already exists: {artifact.id}")
        self._artifacts[artifact.id] = deepcopy(artifact)

    def update_artifact(self, artifact: Artifact) -> None:
        if artifact.id not in self._artifacts:
            raise KeyError(f"Unknown artifact: {artifact.id}")
        self._artifacts[artifact.id] = deepcopy(artifact)

    def get_artifact(self, artifact_id: str) -> Artifact:
        try:
            return deepcopy(self._artifacts[artifact_id])
        except KeyError as exc:
            raise KeyError(f"Unknown artifact: {artifact_id}") from exc

    def list_artifacts(self) -> list[Artifact]:
        return [deepcopy(item) for item in self._artifacts.values()]

    def add_edge(self, edge: Edge) -> None:
        if edge.source_id not in self._artifacts or edge.target_id not in self._artifacts:
            raise KeyError(f"Both edge endpoints must exist: {edge.source_id} -> {edge.target_id}")
        self._edges.append(deepcopy(edge))

    def list_edges(self) -> list[Edge]:
        return [deepcopy(edge) for edge in self._edges]

    def outgoing_edges(
        self, artifact_id: str, kinds: set[EdgeKind] | None = None
    ) -> list[Edge]:
        return [
            deepcopy(edge)
            for edge in self._edges
            if edge.source_id == artifact_id and (kinds is None or edge.kind in kinds)
        ]

    def snapshot(self) -> dict[str, object]:
        return {
            "graph_version": self.version_label,
            "artifacts": [item.model_dump(mode="json") for item in self.list_artifacts()],
            "edges": [edge.model_dump(mode="json") for edge in self.list_edges()],
        }
