---
paths:
  - "src/aioc/tools/**"
---
# Platform Layer - MCP tools

- Every tool description follows the four-part template (CONTRACTS.md sec 6.5) **in order**:
  (1) what it does + input formats, (2) at least three example queries, (3) edge cases and limits,
  (4) when to use this vs. the named alternative. Part 4 is mandatory even when there is no
  alternative ("no alternative; this is the only source for X").
- Errors use the four-class taxonomy: `transient` / `validation` / `business` / `permission`.
  `isError` and `ok` always agree. Only `transient` is retryable, and it must set `retry_after_ms`.
- An empty result is a success (`ok: true`, `meta.returned: 0`), not an error. A `business` error
  means the request cannot be computed, not that it computed to nothing.
- `meta` is required on every success response - it is the baseline the token-reduction work measures.
- Config values are NEVER returned - keys only, at every layer.
- `analyze_logs` / `analyze_events` (`incident/analyze_server.py`) overlap on purpose: they are the
  Domain 2 routing case study's baseline, and the `1.1.0` split that fixed them is
  `incident/search_server.py` (`search_container_logs` / `search_recorded_events`). Do not sharpen
  the v1 module - it must stay re-runnable as the "before" - and keep the split's parts 1-3 verbatim
  from v1 so the case study keeps isolating the name and part 4.
