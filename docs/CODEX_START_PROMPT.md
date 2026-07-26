# Prompt to paste into Codex

You are working in the Dragback repository. Read `AGENTS.md`, `dragback.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/GRAPH_SCHEMA.md`, and `TASKS.md` before changing code.

First run:

```bash
make demo
make test
```

Preserve these product invariants:

- the LLM never issues the final verdict;
- newest does not automatically mean authoritative;
- invalidation is scope-sensitive, not blanket;
- the agent cannot approve itself;
- grants bind to graph snapshot and plan hash;
- the executor independently verifies grants;
- the deterministic demo must work without API keys.

Your immediate objective is to make the hackathon demonstration production-like without expanding its scope. Prioritize:

1. selective invalidation correctness and explainable paths;
2. stale-grant rejection at the executor;
3. a visible `ACT -> REPLAN -> ACT` loop transition;
4. a thin UI showing one invalidated sibling and one preserved sibling;
5. Neo4j parity only after the core tests remain green.

Do not add live OAuth, authentication, multitenancy, broad company search, or extra agent personas. Add tests for every behavioral change. Clearly distinguish real behavior from fixture-driven integrations.

At the start of your response, summarize the current architecture and state which invariant your proposed changes strengthen. Then implement the smallest complete change, run relevant tests, and report exact results.
