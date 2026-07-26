from __future__ import annotations

import pytest
from dragback.graph.neo4j_store import Neo4jGraphStore


def test_version_record_is_required() -> None:
    with pytest.raises(RuntimeError, match=r"metadata is missing.*reset\(\)"):
        Neo4jGraphStore._version_from_record(None)


@pytest.mark.parametrize("value", [None, True, -1, "17"])
def test_version_record_must_contain_a_non_negative_integer(value: object) -> None:
    with pytest.raises(RuntimeError, match="invalid version"):
        Neo4jGraphStore._version_from_record({"version": value})


def test_version_record_returns_the_persisted_version() -> None:
    assert Neo4jGraphStore._version_from_record({"version": 17}) == 17
