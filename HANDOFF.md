# Handoff - point a new session here

Written at the end of **Day 6** of 30, after the Days 5-6 merge and the switch to a single
maintainer. `CLAUDE.md` is loaded automatically and covers what the project *is*; this file
covers what a fresh session cannot infer from the code - live state, environment traps,
standing preferences, and what to do next.

Read this, then `docs/EXECUTION_PLAN.md` for Day 7.

**This file is now the only handover that exists.** It used to be a convenience on top of a
second engineer who also knew all of this. That engineer left after Day 6, so anything true
but unwritten is one forgotten detail away from being lost. Update it at the end of every
working day.

---

## 1. Standing preferences (these are not negotiable defaults, they are the user's)

- **Keep costs low.** The 141-test suite makes **zero API calls** and must stay that way.
  Live checks live in `scripts/check_*.py`, are opt-in, and cost one call each. Before
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
| **A / B labels** across all 30 days | **Kept**, redefined as *layer* names rather than people. They now mark which side of the contract a day's work sits on. |
| **The frozen contract** | **Unchanged and still hard.** `schema_version` is still `1.0.0`. The boundary is the whole point; one head owning both sides makes it easier to erode, not less necessary. |
| **The §0 change process** | **Rewritten.** "Both engineers agree in writing" became a dated rationale in `docs/design-notes/contract-changes.md`, written *before* the code changes, plus the superseded text struck through rather than deleted. The record replaces the counterparty. |
| **Daily sync ritual** | Replaced by reading this file plus the previous day's done-when before writing code. |
| **Risk register** | The "uneven contribution" row is struck through and replaced by three real ones: layer boundary erodes, contract changes unrecorded, single point of failure. |
| **Historical attributions** in `docs/interview-prep/` | **Left as written.** They record what actually happened and are the source for the war stories. |
| **Timeline** | Roughly doubled. ~10-12 weeks full-time, not ~6. |

## 3. Where the code is

Days 5-6 are **merged into `main` on both remotes**. Nothing is in flight except the
single-maintainer doc pass on `docs/single-maintainer-handover`.

| Remote | Repo | `main` | Role |
|---|---|---|---|
| `origin` | `m-misbahuddin/aioc` | `72a57b7` | private, the user's own |
| `khanisic` | `Khanisic/aioc` | `432ffbb` | the shared repo; the user wants both kept in sync |

**The two `main` branches have identical trees and different commit SHAs.** Merging the
same PR separately on each repo produced one merge commit per repo. Content is in sync
(`git diff khanisic/main origin/main` is empty); history has forked by one commit.

To stop it drifting further, merge in one place and mirror the result:

```bash
# merge the PR on origin only, then fast-forward the other remote
git push khanisic origin/main:main
```

Reconciling the *existing* fork needs a force push over published history, which is why it
was left alone. Ask before doing it.

### What is live

| Area | State |
|---|---|
| `contracts/` | Frozen at `1.0.0`, executable as Pydantic v2. Do not change a frozen shape. |
| `llm/` | Harness: `complete`, `stream_text`, `run_tool_loop`. Defaults `claude-sonnet-5`, 8192 tokens - both from measurement. |
| `agents/incident.py` | `investigate` (prose) + `diagnose` (schema-validated via forced `tool_use`), with a schema-annotation layer. |
| `observability/prometheus.py` | Day 5. Live metrics rendered as the agent's context block. |
| `coordinator/planner.py` | Day 6. `plan()` returns a validated `SelectionPlan`. **Planning only - no execution.** Selection measured **5/5** on the done-when queries. |
| `tools/envelope.py` + `tools/incident/timeline_server.py` | Day 6. `get_incident_timeline` as a real stdio MCP server. |
| `docker/postgres/init/` | 18 incidents, 65 timeline events, seeded. |
| Docs/GitHub/Deployment agents | Empty. Days 8/11/12. |

## 4. Environment traps - read before debugging anything

**A native Windows PostgreSQL service is listening on 5432 alongside Docker's proxy.** It
wins the race and rejects the `aioc` role, which presents as `password authentication
failed` with every credential provably identical. Confirm with `netstat -ano | grep :5432`.

Postgres is published on **55432** to work around it. **`.env` still says 5432**, so the
four `integration`-marked tests skip unless you override. The fix the user is applying by
hand, in `.env`:

```
POSTGRES_PORT=55432
DATABASE_URL=postgresql://aioc:aioc_dev_only@localhost:55432/aioc
```

Until that lands, bring the stack up and run the suite like this:

```bash
POSTGRES_PORT=55432 docker compose up -d --wait
DATABASE_URL="postgresql://aioc:aioc_dev_only@localhost:55432/aioc" uv run pytest -q
```

`.env` cannot be read by an agent (denied by `.claude/settings.json`, correctly). To compare
a secret, hash it - that is how the port collision was diagnosed without ever reading the
password.

Other traps:

- **GNU Make is not installed.** Every `Makefile` recipe is a single pasteable command.
- **`PYTHONIOENCODING=utf-8` is required** on any command that prints model output. The
  console is cp1252 and a Unicode arrow in a summary crashes the print.
- `make db-reset`, `docker compose down -v`, and `git push` prompt for permission.
  **`down -v` destroys the seeded corpus.**

## 5. Verify the state in one go

```bash
uv sync --all-groups
POSTGRES_PORT=55432 docker compose up -d --wait
DATABASE_URL="postgresql://aioc:aioc_dev_only@localhost:55432/aioc" uv run pytest -q   # expect 141 passed
uv run ruff check . && uv run ruff format --check . && uv run mypy                     # all clean
```

Costs nothing. Last run: **141 passed**, lint and mypy clean, at the Days 5-6 merge.

## 6. Next work: Day 7

- **A:** Task delegation with explicit context passing in each subagent prompt. The
  coordinator currently *plans* but does not *execute* - Day 7 is the executor that consumes
  a `SelectionPlan`, runs the parallel group, then the sequential chain, and assembles a
  `CoordinatorResponse`. It should be a new module beside `planner.py`;
  `SelectionPlan.parallel_group` and `.sequential_chain` already exist for exactly this.
- **B:** `correlate_events` tool + structured `isError` responses across all four error
  classes. `tools/envelope.py` already implements the taxonomy with per-class requirements
  asserted at construction, so this is mostly a second server following
  `timeline_server.py`'s shape.
- **Done when:** a subagent receives context it never inherited; all four error classes
  return distinctly.

## 7. Carried-over items, none blocking

1. **`.env` port fix** (section 4). Until then integration tests need the inline override.
2. **The two remotes' `main` histories have forked** by one merge commit each (section 3).
   Content is identical. Reconciling needs a force push, so it needs asking.
3. **`TIMELINE_STORE_TIMEOUT` deviates from CONTRACTS.md §7.1**, which names
   `PROMETHEUS_TIMEOUT` for this tool. The events are in Postgres, so the contract's code
   would be a false value in a programmatically matched field. Additive, so patch level
   under §0. Now needs an entry in `docs/design-notes/contract-changes.md` plus a §9 row.
   Flagged in the module docstring.
4. **Prompt caching not enabled.** The system prompt plus tool schema is byte-identical on
   every call and sits at the front of the prefix. Obvious win, not yet taken.
5. **Haiku is a Day 17 question, not a closed one.** It scored 1/3 on contract-valid
   structured output where Sonnet scored 3/3, and blind retry made it no cheaper than
   Sonnet. A retry loop that re-sends *with the validation error attached* should change
   that. The user wants Haiku; the answer is "once Day 17 exists".

## 8. Where things are written down

| Need | File |
|---|---|
| What the project is, conventions, layout | `CLAUDE.md` (auto-loaded) |
| The frozen contract | `docs/CONTRACTS.md` - normative, read before touching a schema |
| Rationale for any frozen change, written before the code | `docs/design-notes/contract-changes.md` |
| Day-by-day plan and done-whens | `docs/EXECUTION_PLAN.md` |
| Running and reading tests | `docs/guides/running-tests.md` |
| The corpus schema and how to extend it | `docs/guides/incidents-table.md` |
| Why things are shaped this way | `docs/interview-prep/decisions.md` |
| Six debugging narratives worth not repeating | `docs/interview-prep/war-stories.md` |
| Every measured number, with provenance | `docs/interview-prep/numbers.md` |
| The user's own resume notes | `PROGRESS.local.md` (gitignored) |

Per-area conventions load automatically from `.claude/rules/` when you open a file they
cover - `contracts.md`, `coordinator.md`, `agents.md`, `tools.md`, `tests.md`,
`platform.md`. Read the relevant one before writing in that area.

## 9. Two habits this project rewards

**Check `stop_reason` before blaming the model.** A truncated structured-output call looks
like a model ignoring your schema - the surviving fragment is valid JSON with a missing
tail. That cost real time once and there is now a named regression test for it.

**When a new assertion fails on data you believe is right, the assertion is a hypothesis
too.** It has already happened twice here: a timeline-containment check that was wrong (the
data was right), and integration fixtures using 30-day windows against a 7-day contract cap.
Check the spec before changing the data.
