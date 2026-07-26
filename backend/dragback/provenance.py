from __future__ import annotations

from collections.abc import Iterable

from dragback.domain import Edge, EdgeKind, InvalidationPath

AUTHORITY_DOWNSTREAM_EDGE_KINDS = {
    EdgeKind.BASIS_FOR,
    EdgeKind.CREATES,
    EdgeKind.DECOMPOSES_TO,
    EdgeKind.CURRENTLY_DRIVES,
    EdgeKind.IMPLEMENTS,
}


def authority_edge_sort_key(
    edge: Edge,
) -> tuple[str, str, tuple[str, ...], str]:
    """Return one backend-independent traversal order for authority edges."""

    return (
        edge.target_id,
        edge.kind.value,
        tuple(sorted(edge.scopes)),
        edge.evidence_ref or "",
    )


def select_primary_invalidation_path(
    paths: Iterable[InvalidationPath],
    *,
    preferred_artifact_id: str | None = None,
) -> InvalidationPath | None:
    """Choose longest provenance, then the lexicographically smallest equal-depth path."""

    candidates = list(paths)
    if preferred_artifact_id is not None:
        preferred = [
            path for path in candidates if path.artifact_id == preferred_artifact_id
        ]
        if preferred:
            candidates = preferred
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda path: (
            -len(path.node_ids),
            tuple(path.node_ids),
            path.artifact_id,
        ),
    )
