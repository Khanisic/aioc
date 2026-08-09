# Handoff - point a new session here

Written at the end of **Day 7** of 30, after delegation landed and the error taxonomy went
four-for-four. `CLAUDE.md` is loaded automatically and covers what the project *is*; this
file covers what a fresh session cannot infer from the code - live state, environment traps,
standing preferences, and what to do next.

Read this, then §6 below, then `docs/EXECUTION_PLAN.md` Day 8.

**This file is the only handover that exists.** The second engineer left after Day 6, so
anything true but unwritten is one forgotten detail away from being lost. Update it at the
end of every working day.

---

## 1. Standing preferences (these are not negotiable defaults, they are the user's)

- **Keep costs low.** The 190-test suite makes **zero API calls** and must stay that way.
  Live checks live in `scripts/check_*.py`, are opt-in, and cost 1-2 calls each. Before
  running anything live, say how many calls it will cost. Do not run a model matrix unasked.
- **No em dashes** in any output or file. Plain dashes only.
- **Never add an agent name as commit co-author.**
- Reproduce bugs end-to-end before fixing them. Fix lint and test failures you notice even
  when they are not yours.
- The user was **Engineer A** (Reasoning Layer) and now owns both layers.

## 2. Staffing: one maintainer, both layers

The second engineer left after Day 6. What changed in the docs, and what deliberately did not:

| Thing | Decision |
|---|---|
| **A / B labels** across all 30 days | **Kept**, redefined as *layer* names rather than people. They mark which side of the contract a day's work sits on. |
| **The frozen contract** | **Unchanged and still hard.** `schema_version` is still `1.0.0`. One head owning both sides makes the boundary easier to erode, not less necessary. |
| **The §0 change process** | **Rewritten.** "Both engineers agree in writing" became a dated rationale in `docs/design-notes/contract-changes.md`, written *before* the code changes, plus the superseded text struck through rather than deleted. The record replaces the counterparty. |
| **Daily sync ritual** | Replaced by reading this file plus the previous day's done-when before writing code. |
| **Risk register** | "Uneven contribution" is struck through and replaced by three real ones: layer boundary erodes, contract changes unrecorded, single point of failure. |
| **Historical attributions** in `docs/interview-prep/` | **Left as written.** They record what actually happened and are the source for the war stories. |
| **Timeline** | Roughly doubled. ~10-12 weeks full-time, not ~6. |

## 3. Where the code is

Everything is merged. **Nothing is in flight** - `main` is the only branch that matters.

| Remote | Repo | `main` | Role |
|---|---|---|---|
| `origin` | `m-misbahuddin/aioc` | the Day 7 merge | private, the user's own |
| `khanisic` | `Khanisic/aioc` | `963b2ef` (end of Day 6 content) | the shared repo; the user wants both kept in sync |

**The two `main` histories forked before Day 7** (one merge commit per repo for the same
content) and khanisic now also trails by the Day 7 merge. Reconciling needs a force push
over published history, which is why it was left alone. Ask before doing it; until then,
merge on `origin` only.

### What is live

| Area | State |
|---|---|
| `contracts/` | Frozen at `1.0.0`, executable as Pydantic v2. Do not change a frozen shape. |
| `llm/` | Harness: `complete`, `stream_text`, `run_tool_loop`. Defaults `claude-sonnet-5`, 8192 tokens - both from measurement. |
| `agents/incident.py` | `investigate` (prose) + `diagnose` (schema-validated). **Still the only agent.** `diagnose` now also takes a `usage` accumulator (the Day 7 cost seam). |
| `coordinator/planner.py` | Day 6. `plan()` returns a validated `SelectionPlan`; now rejects cyclic `depends_on`, takes a `usage` accumulator, and stamps `round` itself rather than asking the model (war story #7). Selection measured **5/5**. |
| `coordinator/executor.py` | **Day 7.** `Executor.execute(plan, query)` -> contract `CoordinatorResponse`. Explicit context passing proven by test; unrunnable agents produce `resolvable: false` gaps, never fabricated responses; synthesis deterministic until Day 14; cost measured, not estimated. `respond()` = plan + execute in one call. |
| `tools/envelope.py` + `tools/policy.py` | The contract wire envelope (sec 6) + the chaos ground-truth permission gate shared by all servers. |
| `tools/incident/timeline_server.py` | Day 6. `get_incident_timeline` stdio MCP server. Now also enforces the chaos gate. |
| `tools/incident/correlate_server.py` | **Day 7.** `correlate_events` stdio MCP server over the corpus (impulse Pearson over 60s event bins). All four error classes return distinctly - there is a named test producing each from real code paths. |
| `tools/incident/store.py` | Shared Postgres settings for both servers (the `.env` port override lives through this). |
| `docker/postgres/init/` | 18 incidents, 65 timeline events, seeded. |
| Docs/GitHub/Deployment agents | **Empty.** Days 8/11/12. The executor's `default_runners()` is where each one registers when it lands - nothing forces the registration, so wiring it is part of each agent's day. |

## 4. Environment traps - read before debugging anything

**The Postgres port collision is fixed, and the fix is in `.env`.** Keep this section: the
trap returns the moment `.env` is regenerated from `.env.example`, which still says 5432.

A native Windows PostgreSQL service listens on 5432 alongside Docker's proxy. It wins the
race and rejects the `aioc` role, which presents as `password authentication failed` with
every credential provably identical. Two LISTENING pids in `netstat -ano | grep :5432`
confirm it. Postgres is therefore published on **55432**, and `.env` now carries:

```
POSTGRES_PORT=55432
DATABASE_URL=postgresql://aioc:aioc_dev_only@localhost:55432/aioc
```

Verified: `uv run pytest -q` runs all 190 including the 7 `integration`-marked tests with no
inline override. If those seven start skipping again, this is why. (They also skip when Docker
Desktop itself is not running - the skip message says "connection timeout expired" either way,
so check `docker compose ps` before re-reading this section.)

`.env` cannot be read by an agent (denied by `.claude/settings.json`, correctly). To compare
a secret, hash it - that is how the port collision was diagnosed without ever reading the
password.

Other traps:

- **GNU Make is not installed.** Every `Makefile` recipe is a single pasteable command.
- **`PYTHONIOENCODING=utf-8` is required** on any command that prints model output. The
  console is cp1252 and a Unicode arrow in a summary crashes the print.
- `make db-reset`, `docker compose down -v`, and `git push` prompt for permission.
  **`down -v` destroys the seeded corpus.**
- Bring the stack up with `docker compose up -d --wait`; `.env` now supplies the port.

## 5. Verify the state in one go

```bash
uv sync --all-groups
docker compose up -d --wait
uv run pytest -q                                                   # expect 190 passed
uv run ruff check . && uv run ruff format --check . && uv run mypy  # all clean
```

Costs nothing. Last run: **190 passed**, lint and mypy clean, at the end of Day 7.

The one live check for this day's work, when you want it re-proven (**2 API calls**):

```bash
PYTHONIOENCODING=utf-8 uv run python scripts/check_day7_delegation.py
```

## 6. Next work: Day 8 - Docs agent and retrieval

**Done when:** the Docs agent answers from the seeded corpus with citations.

### A track: the Docs agent

Follow `agents/incident.py`'s structured pattern exactly - forced `tool_use`, schema
generated from the frozen models, `_apply_guidance`-style annotation for the cross-field
rules. Read CONTRACTS.md §4.2 (`DocsFindings`) before writing anything: claims cite `doc_*`
sources, every claim needs a citation, and the agent must be constrained to retrieved
documents only - "the model's own knowledge" is exactly what a Docs agent must not answer
from.

**Registration is part of the day.** Add the new agent to
`coordinator/executor.py::default_runners()` (the `AgentRunner` protocol:
`run(query, *, context, request_id, invocation_id, usage)`). Until that line lands, the
executor honestly reports `docs` invocations as `agent_not_implemented` gaps - nothing will
remind you except the Day 10 demo failing to use it.

### B track: pgvector ingestion

- `04-embeddings.sql` with a separate `incident_embeddings` table keyed by `incident_id`
  and a `model` column - re-embedding must never rewrite the corpus. The dimension is fixed
  at CREATE TABLE time, so choosing the embedding model is *the* Day 8 decision.
- Anthropic has no embeddings endpoint, so this means an external provider (Voyage was the
  working assumption) - a new account/key, which belongs in `.env` and `.env.example`
  (key name only) and in the EXECUTION_PLAN accounts checklist.
- Hybrid search: vector + lexical. The lexical half is already indexed
  (`incidents_summary_trgm_idx`, pg_trgm from `01-extensions.sql`).
- Remember `docker/postgres/init/` only runs on a fresh volume: adding `04-embeddings.sql`
  needs `make db-reset` (destructive, prompts) or applying the file by hand with `psql`.

### Testing

Same split as Days 6-7: offline tests with fakes for the agent plumbing and the ingestion
logic, `integration`-marked tests against the seeded corpus, live checks opt-in under
`scripts/`. `tests/test_executor.py` shows the runner-protocol fake pattern; a Docs agent
fake registered in a test's runner map exercises the full delegation path with zero calls.

## 7. Carried-over items, none blocking

1. **The two remotes' `main` histories have forked** (§3), and khanisic now trails by Day 7.
   Reconciling needs a force push, so it needs asking.
2. **Three additive error codes await the §0 paperwork**: `TIMELINE_STORE_TIMEOUT` and
   `EVENT_STORE_TIMEOUT` (both replacing the contract's `PROMETHEUS_TIMEOUT` where the store
   is actually Postgres) and `CHAOS_SCOPE_REQUIRED` (the Day 7 permission gate). All additive,
   so patch level under §0: one dated entry in `docs/design-notes/contract-changes.md`, a §9
   row, and a bump to `1.0.1` would clear all three at once. Each is flagged in its module
   docstring; none is done silently.
3. **Prompt caching not enabled.** The system prompt plus tool schema is byte-identical on
   every call and sits at the front of the prefix. Obvious win, not yet taken.
4. **Haiku is a Day 17 question, not a closed one.** It scored 1/3 on contract-valid
   structured output where Sonnet scored 3/3, and blind retry made it no cheaper than Sonnet.
   A retry loop that re-sends *with the validation error attached* should change that. The
   user wants Haiku; the answer is "once Day 17 exists".
5. **Delegation is verified live on one query, not a set.** `scripts/check_day7_delegation.py`
   passes (2 calls: one plan, one diagnose) and is worth re-running after any coordinator
   prompt change, but the routing check has five cases and this has one. Adding cases costs
   2 calls each.

## 8. Where things are written down

| Need | File |
|---|---|
| What the project is, conventions, layout | `CLAUDE.md` (auto-loaded) |
| The frozen contract | `docs/CONTRACTS.md` - normative, read before touching a schema |
| Rationale for any frozen change, written before the code | `docs/design-notes/contract-changes.md` |
| Day-by-day plan and done-whens | `docs/EXECUTION_PLAN.md` |
| Running and reading tests | `docs/guides/running-tests.md` |
| The corpus schema and how to extend it | `docs/guides/incidents-table.md` |
| Why things are shaped this way | `docs/interview-prep/decisions.md` (Day 7 added #11 and #12) |
| Six debugging narratives worth not repeating | `docs/interview-prep/war-stories.md` |
| Every measured number, with provenance | `docs/interview-prep/numbers.md` |
| The user's own resume notes | `PROGRESS.local.md` (gitignored) |

Per-area conventions load automatically from `.claude/rules/` when you open a file they
cover - `contracts.md`, `coordinator.md`, `agents.md`, `tools.md`, `tests.md`,
`platform.md`. Read the relevant one before writing in that area.

## 9. Two habits this project rewards

**Check `stop_reason` before blaming the model.** A truncated structured-output call looks
like a model ignoring your schema - the surviving fragment is valid JSON with a missing tail.
That cost real time once and there is now a named regression test for it.

**When a new assertion fails on data you believe is right, the assertion is a hypothesis
too.** It has already happened twice here: a timeline-containment check that was wrong (the
data was right), and integration fixtures using 30-day windows against a 7-day contract cap.
Check the spec before changing the data.
