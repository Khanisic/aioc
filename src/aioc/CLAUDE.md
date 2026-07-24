# src/aioc - package guide (directory scope)

This package holds both layers from `docs/CONTRACTS.md`. They meet at a JSON wire boundary.

- `contracts/` - jointly owned. The executable form of `docs/CONTRACTS.md`: the frozen contract as
  Pydantic v2 models. Both engineers import it. Do not let it drift from the contract, and do not
  change a frozen shape without the CONTRACTS.md sec 0 process. Note the MCP boundary itself is JSON
  Schema, not Pydantic - a tool server must not depend on these models (contract sec 6).
- `agents/` - the four subagents (Incident, Docs, GitHub, Deployment). Phase 1. Each returns a
  schema-validated `aioc.contracts.AgentResponse`.
- `coordinator/` - intent classification, dynamic agent selection, and the refinement loop. Phase 1.
- `tools/` - Platform Layer MCP tools, grouped `incident/`, `docs/`, `deployment/` (Phase 2, Engineer B).
- `memory/`, `observability/`, `hitl/` - Redis/Postgres/pgvector memory tiers, Langfuse tracing, and
  the human-in-the-loop approval gate (later phases).

Agents and the coordinator import from `aioc.contracts`. Tool servers do not.
