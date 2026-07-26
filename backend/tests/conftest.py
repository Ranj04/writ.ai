from __future__ import annotations

import os

# Service modules create their runtime at import time. Keep the deterministic test
# suite on memory even when a developer's ignored .env selects Neo4j for the live app.
# Opt-in Neo4j parity tests construct their own explicit store.
os.environ["DRAGBACK_GRAPH_BACKEND"] = "memory"
