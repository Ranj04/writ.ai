from __future__ import annotations

from dragback.config import Settings, settings
from dragback.graph.base import GraphStore
from dragback.graph.memory import MemoryGraphStore


def create_graph_store(config: Settings = settings) -> GraphStore:
    if config.graph_backend.lower() == "neo4j":
        from dragback.graph.neo4j_store import Neo4jGraphStore

        return Neo4jGraphStore(
            uri=config.neo4j_uri,
            username=config.neo4j_username,
            password=config.neo4j_password,
            database=config.neo4j_database,
        )
    return MemoryGraphStore()


__all__ = ["GraphStore", "MemoryGraphStore", "create_graph_store"]
