---
description: Validate a JSON payload against the frozen AIOC contract schemas and report violations.
argument-hint: <path-to-json>
allowed-tools: Bash(uv run:*), Read
---
Validate the JSON file at `$1` against the frozen Pydantic contract in `aioc.contracts`.

1. Read `$1` to see its shape.
2. Pick the right root model:
   - has `selected_agents` / `agent_responses` -> `CoordinatorResponse`
   - has `agent` + `findings` -> `AnyAgentResponse` (dispatches on the `agent` field)
   - a tool result payload -> `ToolSuccess` or `ToolFailure`
3. Validate, for example:
   `uv run python -c "import json,sys; from aioc.contracts import CoordinatorResponse; CoordinatorResponse.model_validate(json.load(open(sys.argv[1]))); print('VALID')" $1`
4. If it fails, report each Pydantic error with the offending field path and, where you can, the
   CONTRACTS.md rule it maps to (section number). Do not modify the file unless the user asks.
