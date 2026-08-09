# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

AIOC (Enterprise AI Operations Center) is a multi-agent AIOps system built as portfolio evidence for the Claude Certified Architect - Foundations (CCA-F) credential.
A coordinator dynamically routes operational queries to four deep subagents (Incident, Docs, GitHub, Deployment), each producing schema-validated, confidence-scored output through custom MCP tools.

Every major decision maps to a CCA-F domain, so changes should be legible as evidence for a specific domain, not just functional.
The domain-to-decision mapping and phased build order live in `docs/BUILD_PLAN.md`; the day-by-day execution schedule and definition of done live in `docs/EXECUTION_PLAN.md`.

**Staffing.** The project was planned for two engineers, A (Reasoning Layer) and B (Platform Layer), split across a frozen contract.
The second engineer left after Day 6; one maintainer now owns both layers.
The A/B labels survive throughout the docs as *layer* names, not people, and the contract stays frozen and hard - the boundary is now enforced by tests and by the §0 change process rather than by two people not sharing a head.
Historical attributions in `docs/interview-prep/` are left as written, because they record what actually happened.

## Start a new session by reading HANDOFF.md

`HANDOFF.md` carries what this file cannot: live branch and stack state, environment traps
(notably a port collision that presents as an authentication failure), the standing cost
constraint, and the next day's work. Read it first on a fresh session.

## Current status: Day 7 - coordinator delegates, two tools serve, four error classes distinct

The repository is at the end of Day 7.
Day 1 delivered the Platform Layer scaffold (Docker stack, `Makefile`, package skeleton) plus the frozen contract implemented as Pydantic v2 models under `src/aioc/contracts/`, with a contract-conformance test suite.
Day 2 added the Reasoning Layer's Claude API harness under `src/aioc/llm/` and completed the Domain 3 configuration layer under `.claude/`.
Day 3 added the Incident agent skeleton (expert-SRE prompt, single-turn prose) and the demo app (three containerized services scraped by Prometheus).
Day 4 turned that skeleton's free-text tail into schema-validated output - `IncidentAgent.diagnose` returns a contract `IncidentAgentResponse` via `tool_use` + `tool_choice` - and added the chaos injector (`demo-app/chaos/inject.py`) driving the four `FailureMode` scenarios.
Day 5 replaced the agent's hand-written context with live Prometheus data (`src/aioc/observability/prometheus.py`) and seeded the incident corpus (18 incidents, 65 timeline events, `docker/postgres/init/`). Checkpoint verified live: chaos injected, agent produced contract-valid JSON naming the right failure mode.
Day 6 added the coordinator's planning half (`src/aioc/coordinator/planner.py`) and the first custom MCP tool as a real stdio server (`src/aioc/tools/incident/timeline_server.py`).
Day 7 added the execution half (`src/aioc/coordinator/executor.py`: explicit context passing proven by test, honest `Gap`s for agents that do not exist yet, measured cost) and the second MCP tool (`src/aioc/tools/incident/correlate_server.py`), with all four error classes returning distinctly and a shared chaos ground-truth permission gate (`src/aioc/tools/policy.py`).
Still downstream: the Docs/GitHub/Deployment agents (Days 8/11/12), retrieval (Day 8), tracing and real parallelism (Day 9), the refinement loop and model-written synthesis (Day 14), and the eval harness (Day 19).
`docs/interview-prep/` carries the war stories, decisions, and measured numbers - useful orientation on *why* things are shaped the way they are.
When you add code, follow the structure and sequencing in `docs/BUILD_PLAN.md` (phases) and `docs/EXECUTION_PLAN.md` (days); do not invent a different architecture.

The three source documents, in priority order:

- `docs/CONTRACTS.md` - the frozen integration contract. Normative. Read this before writing any agent, tool, or schema.
- `docs/BUILD_PLAN.md` - what to build, why, and in what phase; the CCA-F domain mapping.
- `docs/EXECUTION_PLAN.md` - who builds what on which day, the accounts/services checklist, and the risk register.

Task-level how-to guides live in `docs/guides/`: `running-tests.md` (the offline suite, the billed live checks, and how to read `test-results/`) and `incidents-table.md` (the Day 5 corpus schema for `docker/postgres/init/`).

## The one hard rule: the contract is frozen

`docs/CONTRACTS.md` (schema version `1.0.0`) is the integration surface between the two layers and is **frozen**.
The frozen sections are: shared primitives, the `AgentResponse` envelope, the four agent findings payloads, the `CoordinatorResponse`, the tool request/response envelope with its four-class error taxonomy, and the six named tool schemas.

Do not change anything frozen as a side effect of implementation work.
Changing a frozen thing requires the formal process in CONTRACTS.md §0, in order: a dated rationale in `docs/design-notes/contract-changes.md` written *before* the code changes, the superseded text struck through rather than deleted, a `schema_version` bump (patch = additive-optional, minor = additive-required or new enum member, major = removal or type change), and a changelog row in §9.
The process originally required a second engineer's written agreement; that engineer left after Day 6, so the written record is now the only check on a frozen change and the first two steps are not optional.
Consumers must read `schema_version` off every payload and fail loudly on a major mismatch rather than best-effort parsing.

What is deliberately **not** frozen and may churn freely: handoff digest formats, eval record formats, prompt text, retrieval parameters, model selection, and every internal module boundary.

**The one pre-authorized exception:** `analyze_logs` (§7.5) and `analyze_events` (§7.6) ship deliberately overlapping and misrouting - they are the independent variable in the Domain 2 routing case study.
Splitting/renaming them is pre-approved as a `1.1.0` bump. Their v1.0.0 definitions must remain in the file verbatim (struck through, not deleted) so the before/after case-study numbers keep their baseline.

## Architecture

Two layers meet at a JSON wire boundary. Keep that boundary clean; it is what lets the two halves be built independently.

**Reasoning Layer** (owns CCA-F Domains 1, 4, half of 5): the coordinator, the four subagents, context passing, output schemas, and evals.
Pydantic v2 is normative here.

**Platform Layer** (owns Domains 2, 3, half of 5): the custom MCP tools, Claude Code configuration, CI, demo environment, and observability.
JSON Schema is normative for anything crossing the MCP boundary.

### The coordinator and four subagents

The coordinator is the spine. Its non-negotiable behaviors (each is graded CCA-F evidence):

- **Dynamic selection** - invoke only the agents a query needs, and record every non-invoked agent in `skipped_agents` with a reason. An empty `skipped_agents` on a typical query means dynamic selection is not working.
- **Explicit context passing** - each subagent receives its context in its prompt, with no automatic inheritance. This is enforced at the schema level: `AgentInvocation.context_passed` must be non-empty. An empty value means context was assumed inherited, which is exactly the failure this project exists to demonstrate the absence of.
- **Parallel vs sequential** - independent agents (Incident + Docs) run in parallel (`mode: parallel`, `depends_on: []`); dependent ones run sequentially (GitHub reads a PR, then Deployment diffs the release: `mode: sequential`, non-empty `depends_on`).
- **Refinement loop** - after synthesis, re-delegate on resolvable gaps. The loop consumes `Gap.suggested_agent` + `Gap.suggested_query` directly and stops on `Gap.resolvable: false`. Trust that flag; agents must set it honestly.

The four agents and their findings payloads (CONTRACTS.md §4): `IncidentFindings`, `DocsFindings`, `GitHubFindings`, `DeploymentFindings`.
All four return the same `AgentResponse` envelope (§3); only `findings` differs.

### The six tools

`get_incident_timeline`, `correlate_events`, `diff_release`, `check_rollout_health`, `analyze_logs`, `analyze_events` (CONTRACTS.md §7).
Every tool description must follow the four-part template in §6.5, in order: (1) what it does + input formats, (2) at least three example queries, (3) edge cases and limits, (4) when to use this vs. the named alternative.
Part 4 is the intervention measured by the routing case study; a tool with no alternative still needs the line.

## Schema conventions that are easy to get wrong

These are validated, not merely conventional. When implementing agents or tools, get these right:

- **`null` vs `[]` are different and both legal.** `null` = not determined / data absent. `[]` = looked and found nothing. A `null` must never be a guess or placeholder; when a field is `null` because data was absent, the response must carry a matching `Gap` in `gaps[]`.
- **The `other` enum pattern.** Every enum has an `other` member, always paired with a detail string. `detail` / `x_detail` must be non-null exactly when the value is `other`, and `null` otherwise. Both directions are validated.
- **`Assessment[T]` wraps analytic fields only** - anything inferred, judged, ranked, or concluded. Factual scalars (timestamps, service names, SHAs, counts) stay plain. `Assessment` carries `value`, `confidence`, `evidence` ids, `reasoning`, `detail`.
- **Confidence bands are normative** (CONTRACTS.md §2.1) and scored by the eval harness. Below `0.25` means set `value` to `null` and emit a `Gap` instead of guessing.
- **`status` must be `partial` or weaker whenever any `Assessment.value` in findings is `null`.** `complete` with a `null` analytic field is a validation error.
- **Config values are never returned** by any tool or agent at any layer - keys only. Keys leak nothing; values leak connection strings and secrets. `diff_release` enforces this at the source.
- **Tool errors use the four-class taxonomy** (§6.4): `transient` (the only retryable class), `validation`, `business`, `permission`. `isError` and `ok` always agree. Retrying a non-transient error will fail identically - change the request or record a `Gap` with `resolvable: false`.

**Formatting primitives** (CONTRACTS.md §1): `snake_case` fields and enum members on the wire and in Python; RFC 3339 timestamps with explicit `Z` (never bare epoch, never local offset); durations as integer `_ms` or `_seconds`; opaque prefixed ids (`inc_`, `evt_`, `ev_`, `doc_`, `claim_`, `act_`, `inv_`, `req_`, `tc_`).

**When the prose in CONTRACTS.md and its worked example (§8) disagree, the example wins.**

## Tech stack and source layout

Python 3.12, Pydantic v2, MCP for tools. Data tiers: Postgres + pgvector (episodic + semantic memory and the RAG corpus), Redis/Upstash (working memory), Prometheus + Grafana (self-hosted, for the demo app's metrics), Langfuse (tracing and cost).
Heavy infrastructure (Kubernetes, Terraform, Kafka, multi-region) is deliberately out of scope - kept only as documented manifests under `infrastructure/`, never a build priority. See `docs/BUILD_PLAN.md` "Out of scope" and `docs/EXECUTION_PLAN.md` appendix for the full rationale.

Source layout (`src/` layout, package `aioc`):

- `src/aioc/contracts/` - the executable form of `docs/CONTRACTS.md` as Pydantic v2 models (`primitives`, `envelope`, `coordinator`, the four findings modules, `tools`, `enums`). All models subclass `StrictModel` (`extra="forbid"`). This is the one package both layers import. Note the MCP boundary itself is JSON Schema, not Pydantic (contract §6) - a tool server must not depend on these models.
- `src/aioc/llm/` - the Claude API harness (Day 2, Reasoning Layer): `LLMClient` with `complete` (messages), `stream_text` (streaming), and `run_tool_loop` (a manual `tool_use` loop with per-call `ToolCallRecord` audit records); `ToolSpec`/`ToolResult`; `LLMSettings` via pydantic-settings.
  Defaults are `claude-sonnet-5` and 8192 output tokens, both set from measurement rather than taste: Sonnet is the cheapest model that returns a contract-valid response on every attempt, and a full incident report does not fit in 4096 tokens.
  Model selection is explicitly not frozen - `scripts/check_structured_output.py` is how you re-measure it. Deliberately decoupled from `aioc.contracts` - the contract's `ToolCallRef` and error taxonomy describe the MCP boundary, which lands in Phase 2. The agents and the coordinator build on this package.
- `src/aioc/agents/` - Reasoning Layer subagents. `incident.py` is live (Days 3-4): `IncidentAgent.investigate` returns prose; `IncidentAgent.diagnose` returns a schema-validated `IncidentAgentResponse`, forcing the model through a single structured-output tool whose JSON Schema is generated from the frozen models and then annotated with the contract's cross-field rules (`_apply_guidance`).
  The annotation layer exists because a generated schema states shape but not invariants: with the `other`/`detail` pairing rule living only in the system prompt, every model tested filled `*_detail` on non-`other` enums and the response failed validation.
  Descriptions are added, never shape, and `aioc.contracts` stays untouched; the guard raises at import if a field it annotates no longer exists.
  Docs/GitHub/Deployment land on Days 8/11/12.
- `src/aioc/coordinator/` - the orchestrator. `planner.py` is live (Day 6): `Coordinator.plan` returns a validated `SelectionPlan` (intent, selected agents with their explicit context, skipped agents with reasons). The graded Domain 1 behaviours are enforced by validators, not prompt text - all four agents must be accounted for, `depends_on` must resolve inside the plan (acyclically), and `context_passed` must be non-empty *and* not merely restate the query. Reads `AIOC_COORDINATOR_MODEL`, which is the seam the Day 23 routing experiment measures. The model is asked for `PlannedInvocation` (`AgentInvocation` minus `round`) and the coordinator stamps `round` itself - plumbing it already knows is a field the model can only get wrong, which it demonstrably did.
  `executor.py` is live (Day 7): `Executor.execute` consumes a plan into a contract `CoordinatorResponse`, handing each runner exactly `context_passed` and nothing else; an invocation it cannot run (only Incident exists until Days 8/11/12) becomes a `Gap` with `resolvable: false`, never a fabricated response. Synthesis is deterministic until Day 14; `cost` is accumulated from `Usage`, never estimated. `respond()` glues plan + execution. New agents register in `default_runners()`. The refinement loop is Day 14.
- `src/aioc/tools/` - Platform Layer MCP tools, grouped `incident/`, `docs/`, `deployment/`. `envelope.py` is the shared contract wire envelope (sec 6); `incident/timeline_server.py` (`get_incident_timeline`, Day 6) and `incident/correlate_server.py` (`correlate_events`, Day 7) are real stdio MCP servers reading the seeded corpus, with connection settings shared in `incident/store.py`. `policy.py` is the chaos ground-truth gate: any `chaos*` service returns a `permission` error (`CHAOS_SCOPE_REQUIRED`) from every server, because those signals are the Day 19 eval's answer key. **None of these import `aioc.contracts`** - the MCP boundary is JSON Schema (contract sec 6), so enums and input schemas are longhand, with a test asserting the copies still match the Python enums. Framework input validation is deliberately off: it returns plain text where the contract requires a structured `validation` error.
- `src/aioc/observability/` - both directions of one concern. `prometheus.py` is live (Day 5): a thin PromQL client plus `build_incident_context`, which renders live demo-app metrics as the Incident agent's explicit context block. **`chaos_knob_value` is excluded by an enforced guard** at both the query and the rendered-output level - it is the Day 19 eval's ground truth, and leaking it would make evals pass silently. Langfuse tracing lands Day 9.
- `src/aioc/{memory,hitl}/` - Redis/Postgres/pgvector memory tiers and the human-in-the-loop gate (later phases).
- `tests/test_contract.py` - validates the models against the CONTRACTS.md §8 worked example plus one negative test per invariant.
- `tests/test_llm_harness.py` - drives the harness `tool_use` loop with a scripted fake client; no network and no API key. `examples/llm_round_trip.py` is the live counterpart (needs a real key).
- `tests/test_incident_agent.py` - drives both Incident agent paths with a scripted fake client (prose accounting, context passing, and the Day 4 structured `diagnose` including a contract-violating payload that must be rejected). `examples/incident_structured_demo.py` is the live counterpart.
- `tests/test_chaos_inject.py` - checks the chaos injector's failure-mode -> knob mapping offline (complete, consistent, unambiguous 1:1 with `FailureMode`).
- `tests/test_seed_corpus.py` - guards the Day 5 incident corpus offline. Parses `docker/postgres/init/02-incidents.sql` and `03-seed-incidents.sql` and asserts the SQL `CHECK` lists match the contract enums, every `FailureMode` has at least two seed rows (a mode with none cannot be scored by the Day 19 eval), ids carry their contract prefixes, and the seed stays idempotent and deterministic. Postgres enforces the constraints; this catches drift.
- `tests/test_prometheus_context.py` - Day 5 metric reads and context rendering, driven by a fake httpx transport. Includes the tests that prove `chaos_knob_value` cannot reach an agent's context.
- `tests/test_coordinator.py` - Day 6 selection planning against a scripted fake client. Mostly negative tests: each enforced orchestration rule has one proving the violation is rejected.
- `tests/test_executor.py` - Day 7 delegation, driven by fake runners through the `AgentRunner` protocol. The two done-when facts have direct tests: the runner receives exactly `context_passed` (nothing inherited, nothing enriched), and an unimplemented agent yields a `resolvable: false` `Gap` rather than a fabricated response. Also dependency ordering, failure isolation, and measured cost.
- `tests/test_timeline_tool.py` - Day 6 MCP tool. Wire envelope, four-class error taxonomy, input validation, and the four-part description template offline; four `integration`-marked tests query the seeded corpus.
- `tests/test_correlate_tool.py` - Day 7 MCP tool. Validation (including `AMBIGUOUS_ANCHOR`), the chaos `permission` gate on both servers, the pure correlation math, the description template, and one named test producing all four error classes distinctly from real code paths; three `integration`-marked tests query the seeded corpus.
- `scripts/` - dev tooling, not shipped code. `runlog.py` records any run as structured JSON under `test-results/`. Four live checks, each costing real tokens and each opt-in: `check_structured_output.py` (per-model contract validity), `check_day5_checkpoint.py` (chaos injected -> agent JSON, scored against ground truth), `check_agent_selection.py` (coordinator routing; defaults to 2 of 5 cases), `check_day7_delegation.py` (plan -> execute end to end, 2 calls; asserts the agent's wire prompt is exactly `context_passed` + query, and that a coordinator-only sentinel does not leak).
- `test-results/` - structured records of every test run: `index.jsonl` plus one directory per run holding `run.json` and `events.jsonl`. Gitignored except its README, which is normative for the record schema. Pytest records itself via hooks in `tests/conftest.py`; `AIOC_RUNLOG=0` opts out.
- `demo-app/`, `docker/`, `docker-compose.yml`, `Makefile` - the local stack (Postgres + pgvector, Redis), the demo app (three services + Prometheus), and `demo-app/chaos/inject.py` (Day 4), owned by the Platform Layer.
- `docker/postgres/init/` - runs once, on first initialisation of an empty Postgres volume, in alphabetical order: `01-extensions.sql` (vector, pg_trgm, pgcrypto), `02-incidents.sql` (the incident corpus schema), `03-seed-incidents.sql` (18 synthetic incidents, 65 timeline events). Changing a file here needs `make db-reset` (destructive) to take. Note `init/` never runs against hosted Postgres, so Day 24 applies these files with `psql` - each is written to be runnable standalone and the seed is idempotent.

CCA-F Domain 3 config (`.claude/`, in place): six path-scoped rules under `.claude/rules/` keyed on `paths:` (contracts, coordinator, agents, tools, tests, platform); a directory-scoped `src/aioc/CLAUDE.md` completing the user → project → directory hierarchy; two slash commands (`/validate-schema`, `/contract`); a `context: fork` project skill `contract-audit` that runs a read-only drift check against the contract; and a committed `.claude/settings.json` carrying the shared permission layer.
Rules are context and `settings.json` is enforcement - put a preference in a rule, put a boundary in `permissions`.
`settings.json` allows the inner dev loop, sends the three destructive commands (`make db-reset`, `docker compose down -v`, `git push`) to `ask`, and denies reads of `.env`, `secrets/**`, `*.pem`, and `*.key`.
Per-developer overrides belong in the gitignored `.claude/settings.local.json`, never in the committed file.
The rationale, the rules-vs-CLAUDE.md split, and the verification procedure are in `docs/design-notes/domain-3-config-layer.md`.

## Commands

The Python project is `uv`-managed and pinned to Python 3.12 (`requires-python = ">=3.12,<3.13"`). `uv` fetches the interpreter itself, so a system 3.12 is not required. `dev` dependencies are a PEP 735 group, installed by default.

- `uv sync --all-groups` - create/refresh the `.venv` with runtime + dev dependencies.
- `uv run pytest -q` (or `make test`) - run the contract-conformance suite. `make test` runs unit tests only, skipping `integration`-marked tests that need the Docker stack.
- `uv run pytest "tests/test_contract.py::test_worked_example_validates_and_round_trips"` - run a single test.
- `make lint` - `ruff` + `mypy` (strict); both are configured in `pyproject.toml`.

The stack and chaos commands (from B's `Makefile`; the demo app itself lands later):

- `docker compose up -d --wait` / `make up` - bring up Postgres (with the `vector` extension) and Redis. `make verify` checks they are actually usable, not merely running.
- `make down`, `make db-reset` (destructive), `make psql` - stack lifecycle helpers.
- `make chaos-<mode>` - injects one of four failure modes (memory leak, bad config deploy, slow downstream dependency, 500-spike tied to a commit). These map 1:1 to the `FailureMode` enum so eval output can be scored against injected ground truth.

## House style

- Never use the em dash. Use a plain dash instead.
- In long Markdown files, put each full sentence on its own line (this file follows that rule); keep normal Markdown structure otherwise.
- Do not hand-edit `CHANGELOG.md` or other auto-generated files. The CONTRACTS.md §9 changelog is the exception - it is edited by hand as part of the formal contract-change process.
