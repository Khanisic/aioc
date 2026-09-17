# src/aioc - package guide (directory scope)

This package holds both layers from `docs/CONTRACTS.md`. They meet at a JSON wire boundary.

- `contracts/` - the executable form of `docs/CONTRACTS.md`: the frozen contract as Pydantic v2
  models. Both layers import it. Do not let it drift from the contract, and do not change a frozen
  shape without the CONTRACTS.md sec 0 process. Note the MCP boundary itself is JSON Schema, not
  Pydantic - a tool server must not depend on these models (contract sec 6).
- `llm/` - the Claude API harness (Day 2): `LLMClient.complete` / `stream_text` / `run_tool_loop`,
  `ToolSpec`/`ToolResult`, and per-call `ToolCallRecord` audit records. The agents and the
  coordinator build on it. Deliberately decoupled from `contracts/` - it mirrors `ToolCallRef`
  in spirit without importing it (the contract describes the MCP boundary, which lands in Phase 2).
- `agents/` - the four subagents (Incident, Docs, GitHub, Deployment). Phase 1. Each returns a
  schema-validated `aioc.contracts.AgentResponse`. Incident is live (Days 3-4): `investigate`
  returns prose, `diagnose` returns a validated `IncidentAgentResponse` by forcing a single
  structured-output tool (`tool_use` + `tool_choice`). The tool's schema is generated from the
  frozen models and then annotated (`_apply_guidance`) with the contract's cross-field rules -
  a generated schema states shape but not invariants, and stating them only in the system prompt
  demonstrably does not hold. Add descriptions there, never shape, and never edit `contracts/`
  to do it (the shared helper is `agents/_annotate.py`). Docs is live (Day 8): retrieval-grounded
  `answer` with in-code grounding checks - an uncited-in-retrieval document id or a paraphrased
  quote raises. GitHub is live (Day 11): tool-driven `analyze` over the `aioc-github` MCP server
  through `aioc.llm.mcp.McpStdioToolset`, facts stamped from tool replies, ungrounded references
  rejected in code. Deployment is live (Day 12): the same shape over `aioc-deployment`, with keys,
  images, and health signals stamped from the replies and a gap required for any tool not run.
  `_toolset.py` is the shared `Toolset` seam and grounding ledger for the tool-driven agents.
  `_status.py` (Day 13) settles `complete` over a null judgement to `partial` for every agent - a
  value the runtime can derive is never asked of the model.
- `retrieval/` - Day 8 ingestion and hybrid search over the incident corpus (pg_trgm + pgvector,
  RRF fusion, Voyage embeddings behind the `Embedder` protocol, lexical-only without a key).
  Consumed by the Docs agent through the `CorpusRetriever` seam.
- `coordinator/` - intent classification, dynamic agent selection, and the refinement loop. Phase 1.
  `handoff.py` (Day 13) is the sequential handoff: a bounded, plain-text digest of a dependency's
  response that the executor appends to the dependent's planner block at the moment the dependency
  returns, and records verbatim in that invocation's `context_passed`. Direct dependencies only.
  The refinement loop (Day 14) lives in `executor.py` and reuses that composition: an open gap with
  a `suggested_agent` becomes a `round: 1+` invocation whose query is the `suggested_query`
  verbatim and whose context is the planner's block, the refinement block, and the raising
  response's digest. `synthesis.py` (Day 14) is the synthesis seam - deterministic by default,
  `ModelSynthesiser` opt-in at the entry point, grounded in code with a deterministic fallback.
- `tools/` - Platform Layer MCP tools, grouped `incident/`, `github/`, `docs/`, `deployment/` (Phase 2).
  `deployment/` (Day 12) is the two contract-named tools: `release.py` diffs release manifests
  structurally with values hashed at parse time (keys and paths only, by construction), `health.py`
  reads Prometheus and applies the deterministic rollout-status rule, `server.py` is the wire.
  `incident/analyze_server.py` (Day 13) is the `aioc-analyze` server for `analyze_logs` (over
  `docker compose logs`, `logs.py`) and `analyze_events` (over the seeded timeline). Their part-4
  lines are the contract's deliberately weak v1.0.0 text, pinned by a test - do not sharpen them;
  the module is the routing case study's baseline. `incident/search_server.py` (Day 14) is the
  `1.1.0` split: the same tools as `search_container_logs` / `search_recorded_events`, the v1
  implementation under new names, and a part 4 that names the alternative.
- `memory/`, `observability/`, `hitl/` - Redis/Postgres/pgvector memory tiers, Langfuse tracing, and
  the human-in-the-loop approval gate (later phases).

Agents and the coordinator import from `aioc.contracts` and build on `aioc.llm`. Tool servers do neither.
