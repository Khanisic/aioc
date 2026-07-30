# Handoff - point a new session here

Written at the end of **Day 6** of 30. `CLAUDE.md` is loaded automatically and covers what the
project *is*; this file covers what a fresh session cannot infer from the code - live state,
environment traps, standing preferences, and what to do next.

Read this, then `docs/EXECUTION_PLAN.md` for Day 7.

---

## 1. Standing preferences (these are not negotiable defaults, they are the user's)

- **Keep costs low.** The 141-test suite makes **zero API calls** and must stay that way. Live
  checks live in `scripts/check_*.py`, are opt-in, and cost one call each. Before running
  anything live, say how many calls it will cost. Do not run a model matrix unasked.
- **No em dashes** in any output or file. Plain dashes only.
- **Never add an agent name as commit co-author.**
- Reproduce bugs end-to-end before fixing them. Fix lint and test failures you notice even when
  they are not yours.
- The user is **Engineer A** (Reasoning Layer: coordinator, agents, schemas, evals) but has been
  doing both engineers' work.

## 2. Where the code is

Branch **`d5-d6-live-metrics-coordinator-and-first-tool`**, 5 commits ahead of `main`, pushed to
both remotes. `main` is at `e45d329` and is a clean mirror of `khanisic/main` (Day 4).

Two remotes, and the distinction matters:

| Remote | Repo | Role |
|---|---|---|
| `origin` | `m-misbahuddin/aioc` | private, the user's own |
| `khanisic` | `Khanisic/aioc` | shared with the other engineer; PRs merge here |

Days 1-4 merged into `khanisic/main` via PRs #1-#5. **Days 5-6 have no PR yet** - that is a
decision for the user, not something to do unprompted.

### What is live

| Area | State |
|---|---|
| `contracts/` | Frozen at `1.0.0`, executable as Pydantic v2. Do not change a frozen shape. |
| `llm/` | Harness: `complete`, `stream_text`, `run_tool_loop`. Defaults `claude-sonnet-5`, 8192 tokens - both from measurement. |
| `agents/incident.py` | `investigate` (prose) + `diagnose` (schema-validated via forced `tool_use`), with a schema-annotation layer. |
| `observability/prometheus.py` | Day 5. Live metrics rendered as the agent's context block. |
| `coordinator/planner.py` | Day 6. `plan()` returns a validated `SelectionPlan`. **Planning only - no execution.** |
| `tools/envelope.py` + `tools/incident/timeline_server.py` | Day 6. `get_incident_timeline` as a real stdio MCP server. |
| `docker/postgres/init/` | 18 incidents, 65 timeline events, seeded. |
| Docs/GitHub/Deployment agents | Empty. Days 8/11/12. |

## 3. Environment traps - read before debugging anything

**A native Windows PostgreSQL service is listening on 5432 alongside Docker's proxy.** It wins
the race and rejects the `aioc` role, which presents as `password authentication failed` with
every credential provably identical. Confirm with `netstat -ano | grep :5432` - two LISTENING
pids means this.

Postgres was republished on **55432** to work around it. **Unless the user has since edited
`.env`, the stack is on 55432 while `.env` says 5432**, so the four `integration`-marked tests
will skip. Ask before touching `.env` - it holds the API key and cannot be read (denied by
`.claude/settings.json`, correctly).

To run the DB-backed tests today:

```bash
DATABASE_URL="postgresql://aioc:aioc_dev_only@localhost:55432/aioc" uv run pytest -q
```

Other traps:

- **GNU Make is not installed.** Every `Makefile` recipe is a single pasteable command.
- **`PYTHONIOENCODING=utf-8` is required** on any command that prints model output. The console
  is cp1252 and a Unicode arrow in a summary crashes the print.
- `.env`, `secrets/**`, `*.pem`, `*.key` are unreadable. To compare a secret, hash it - that is
  how the port collision was diagnosed without ever reading the password.
- `make db-reset`, `docker compose down -v`, and `git push` prompt for permission. **`down -v`
  now destroys the seeded corpus**, which was not true before Day 5.

## 4. Verify the state in one go

```bash
uv sync --all-groups
DATABASE_URL="postgresql://aioc:aioc_dev_only@localhost:55432/aioc" uv run pytest -q   # expect 141 passed
uv run ruff check . && uv run ruff format --check . && uv run mypy                     # all clean
docker compose up -d --wait                                                            # if the stack is down
```

Costs nothing. If tests pass and lint is clean, nothing has rotted.

## 5. Next work: Day 7

From `docs/EXECUTION_PLAN.md`:

- **A:** Task delegation with explicit context passing in each subagent prompt. The coordinator
  currently *plans* but does not *execute* - Day 7 is the executor that consumes a
  `SelectionPlan`, runs the parallel group, then the sequential chain, and assembles a
  `CoordinatorResponse`.
- **B:** `correlate_events` tool + structured `isError` responses across all four error classes.
- **Done when:** a subagent receives context it never inherited; all four error classes return
  distinctly.

`tools/envelope.py` already implements the taxonomy with per-class requirements asserted at
construction, so B is mostly a second server following `timeline_server.py`'s shape.

For A, the executor should be a new module beside `planner.py`. `SelectionPlan.parallel_group`
and `.sequential_chain` already exist for exactly this.

## 6. Carried-over items, none blocking

1. **3 of 5 coordinator cases unverified live.** Day 6's done-when names five sample queries;
   two are verified and pass. `uv run python scripts/check_agent_selection.py --all` runs the
   rest - **3 API calls**, so ask first.
2. **`.env` port fix** (section 3). Until then integration tests skip.
3. **`TIMELINE_STORE_TIMEOUT` deviates from CONTRACTS.md §7.1**, which names
   `PROMETHEUS_TIMEOUT` for this tool. The events are in Postgres, so the contract's code would
   be a false value in a programmatically-matched field. Additive, so patch-level under §0 -
   needs a §9 changelog row and the other engineer's agreement. Flagged in the module docstring.
4. **Prompt caching not enabled.** The system prompt plus tool schema is byte-identical on every
   call and sits at the front of the prefix. Obvious win, not yet taken.
5. **Haiku is a Day 17 question, not a closed one.** It scored 1/3 on contract-valid structured
   output where Sonnet scored 3/3, and blind retry made it no cheaper than Sonnet. A retry loop
   that re-sends *with the validation error attached* should change that. The user wants Haiku;
   the answer is "once Day 17 exists".

## 7. Where things are written down

| Need | File |
|---|---|
| What the project is, conventions, layout | `CLAUDE.md` (auto-loaded) |
| The frozen contract | `docs/CONTRACTS.md` - normative, read before touching a schema |
| Day-by-day plan and done-whens | `docs/EXECUTION_PLAN.md` |
| Running and reading tests | `docs/guides/running-tests.md` |
| The corpus schema and how to extend it | `docs/guides/incidents-table.md` |
| Why things are shaped this way | `docs/interview-prep/decisions.md` |
| Six debugging narratives worth not repeating | `docs/interview-prep/war-stories.md` |
| Every measured number, with provenance | `docs/interview-prep/numbers.md` |
| The user's own resume notes | `PROGRESS.local.md` (gitignored) |

Per-area conventions load automatically from `.claude/rules/` when you open a file they cover -
`contracts.md`, `coordinator.md`, `agents.md`, `tools.md`, `tests.md`, `platform.md`. Read the
relevant one before writing in that area.

## 8. Two habits this project rewards

**Check `stop_reason` before blaming the model.** A truncated structured-output call looks like a
model ignoring your schema - the surviving fragment is valid JSON with a missing tail. That cost
real time once and there is now a named regression test for it.

**When a new assertion fails on data you believe is right, the assertion is a hypothesis too.**
It has already happened twice here: a timeline-containment check that was wrong (the data was
right), and integration fixtures using 30-day windows against a 7-day contract cap. Check the
spec before changing the data.
