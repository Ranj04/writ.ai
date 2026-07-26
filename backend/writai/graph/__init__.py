from __future__ import annotations

from writai.config import Settings, settings
from writai.graph.base import GraphStore
from writai.graph.memory import MemoryGraphStore


def create_graph_store(config: Settings = settings) -> GraphStore:
    if config.graph_backend.lower() == "neo4j":
        from writai.graph.neo4j_store import Neo4jGraphStore

        return Neo4jGraphStore(
            uri=config.neo4j_uri,
            username=config.neo4j_username,
            password=config.neo4j_password,
            database=config.neo4j_database,
        )
    return MemoryGraphStore()


__all__ = ["GraphStore", "MemoryGraphStore", "create_graph_store"]
