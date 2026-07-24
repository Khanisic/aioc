---
paths:
  - "src/aioc/agents/**"
---
# Reasoning Layer - agents

- Each subagent returns a schema-validated `AgentResponse` from `aioc.contracts`. Never emit
  free-form JSON; construct or validate the model.
- Explicit context passing: everything a subagent needs is in its prompt - no implicit inheritance.
  The coordinator records the literal block in `AgentInvocation.context_passed`, which must be
  non-empty. This is the single most-tested orchestration fact in the project.
- Nullable fields carry meaning: genuinely absent data is `null` WITH a matching `Gap`, never a
  fabricated plausible value.
- Confidence bands are normative (CONTRACTS.md sec 2.1). Below 0.25 -> set `value` to `null` and
  emit a `Gap`.
- On schema/validation failure, re-request with the specific error attached (the validation-retry
  loop). Track retry-resolvable errors (format) separately from unresolvable ones (info absent).
- Dynamic selection: invoke only the agents a query needs, and record the rest in `skipped_agents`.
