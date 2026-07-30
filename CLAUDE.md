# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

AIOC (Enterprise AI Operations Center) is a multi-agent AIOps system built as portfolio evidence for the Claude Certified Architect - Foundations (CCA-F) credential.
A coordinator dynamically routes operational queries to four deep subagents (Incident, Docs, GitHub, Deployment), each producing schema-validated, confidence-scored output through custom MCP tools.

Every major decision maps to a CCA-F domain, so changes should be legible as evidence for a specific domain, not just functional.
The domain-to-decision mapping and phased build order live in `docs/BUILD_PLAN.md`; the day-by-day execution schedule and definition of done live in `docs/EXECUTION_PLAN.md`.

## Current status: Day 4 - first agent returns validated output, chaos is injectable

The repository is at the end of Day 4.
Day 1 delivered Engineer B's scaffold (Docker stack, `Makefile`, package skeleton) plus the frozen contract implemented as Pydantic v2 models under `src/aioc/contracts/`, with a contract-conformance test suite.
Day 2 added Engineer A's Claude API harness under `src/aioc/llm/` (messages, streaming, and a manual `tool_use` loop, unit-tested against a scripted fake client) and completed Engineer B's Domain 3 configuration layer under `.claude/`.
Day 3 added Engineer A's Incident agent skeleton under `src/aioc/agents/` (expert-SRE prompt, single-turn prose) and Engineer B's demo app under `demo-app/` (three containerized services scraped by Prometheus).
Day 4 turned that skeleton's free-text tail into schema-validated output - `IncidentAgent.diagnose` returns a contract `IncidentAgentResponse` via `tool_use` + `tool_choice` - and added Engineer B's chaos injector (`demo-app/chaos/inject.py`), which drives the four `FailureMode` scenarios over the demo app's `/_chaos` knobs.
Still downstream - the coordinator (`src/aioc/coordinator/`), the Docs/GitHub/Deployment agents, and the MCP tools (`src/aioc/tools/`) - are empty packages awaiting their days.
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
Changing a frozen thing requires the formal process in CONTRACTS.md §0: agreement in writing, a `schema_version` bump (patch = additive-optional, minor = additive-required or new enum member, major = removal or type change), and a changelog row in §9.
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

- `src/aioc/contracts/` - jointly owned; the executable form of `docs/CONTRACTS.md` as Pydantic v2 models (`primitives`, `envelope`, `coordinator`, the four findings modules, `tools`, `enums`). All models subclass `StrictModel` (`extra="forbid"`). This is the one package both engineers import. Note the MCP boundary itself is JSON Schema, not Pydantic (contract §6) - a tool server must not depend on these models.
- `src/aioc/llm/` - the Claude API harness (Day 2, Reasoning Layer): `LLMClient` with `complete` (messages), `stream_text` (streaming), and `run_tool_loop` (a manual `tool_use` loop with per-call `ToolCallRecord` audit records); `ToolSpec`/`ToolResult`; `LLMSettings` via pydantic-settings.
  Defaults are `claude-sonnet-5` and 8192 output tokens, both set from measurement rather than taste: Sonnet is the cheapest model that returns a contract-valid response on every attempt, and a full incident report does not fit in 4096 tokens.
  Model selection is explicitly not frozen - `scripts/check_structured_output.py` is how you re-measure it. Deliberately decoupled from `aioc.contracts` - the contract's `ToolCallRef` and error taxonomy describe the MCP boundary, which lands in Phase 2. The agents and the coordinator build on this package.
- `src/aioc/agents/` - Reasoning Layer subagents. `incident.py` is live (Days 3-4): `IncidentAgent.investigate` returns prose; `IncidentAgent.diagnose` returns a schema-validated `IncidentAgentResponse`, forcing the model through a single structured-output tool whose JSON Schema is generated from the frozen models and then annotated with the contract's cross-field rules (`_apply_guidance`).
  The annotation layer exists because a generated schema states shape but not invariants: with the `other`/`detail` pairing rule living only in the system prompt, every model tested filled `*_detail` on non-`other` enums and the response failed validation.
  Descriptions are added, never shape, and `aioc.contracts` stays untouched; the guard raises at import if a field it annotates no longer exists.
  Docs/GitHub/Deployment land on Days 8/11/12.
- `src/aioc/coordinator/` - the orchestrator: intent classification, dynamic selection, refinement loop (Phase 1 stub).
- `src/aioc/tools/` - Platform Layer MCP tools, grouped `incident/`, `docs/`, `deployment/` (Phase 2 stub, Engineer B).
- `src/aioc/{memory,observability,hitl}/` - Redis/Postgres/pgvector memory tiers, Langfuse tracing, and the human-in-the-loop gate (later phases).
- `tests/test_contract.py` - validates the models against the CONTRACTS.md §8 worked example plus one negative test per invariant.
- `tests/test_llm_harness.py` - drives the harness `tool_use` loop with a scripted fake client; no network and no API key. `examples/llm_round_trip.py` is the live counterpart (needs a real key).
- `tests/test_incident_agent.py` - drives both Incident agent paths with a scripted fake client (prose accounting, context passing, and the Day 4 structured `diagnose` including a contract-violating payload that must be rejected). `examples/incident_structured_demo.py` is the live counterpart.
- `tests/test_chaos_inject.py` - checks the chaos injector's failure-mode -> knob mapping offline (complete, consistent, unambiguous 1:1 with `FailureMode`).
- `tests/test_seed_corpus.py` - guards the Day 5 incident corpus offline. Parses `docker/postgres/init/02-incidents.sql` and `03-seed-incidents.sql` and asserts the SQL `CHECK` lists match the contract enums, every `FailureMode` has at least two seed rows (a mode with none cannot be scored by the Day 19 eval), ids carry their contract prefixes, and the seed stays idempotent and deterministic. Postgres enforces the constraints; this catches drift.
- `scripts/` - dev tooling, not shipped code. `runlog.py` records any run as structured JSON under `test-results/`; `check_structured_output.py` is the live per-model check that `diagnose` returns a contract-valid response.
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
