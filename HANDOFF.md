# Handoff - point a new session here

Written at the end of **Day 6** of 30, after the Days 5-6 merge, the switch to a single
maintainer, and the `.env` port fix. `CLAUDE.md` is loaded automatically and covers what the
project *is*; this file covers what a fresh session cannot infer from the code - live state,
environment traps, standing preferences, and what to do next.

Read this, then §6 below, then `docs/EXECUTION_PLAN.md` Day 7.

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
| `origin` | `m-misbahuddin/aioc` | `6792e82` | private, the user's own |
| `khanisic` | `Khanisic/aioc` | `963b2ef` | the shared repo; the user wants both kept in sync |

**The two `main` branches have identical trees and different commit SHAs.** Merging the same
PR separately on each repo produced one merge commit per repo. Content is in sync
(`git diff khanisic/main origin/main` is empty); history forked by one commit per merge.

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
| `agents/incident.py` | `investigate` (prose) + `diagnose` (schema-validated via forced `tool_use`). **The only agent that exists.** |
| `observability/prometheus.py` | Day 5. Live metrics rendered as the agent's context block. |
| `coordinator/planner.py` | Day 6. `plan()` returns a validated `SelectionPlan`. **Planning only - no execution.** Selection measured **5/5** on the done-when queries. |
| `tools/envelope.py` + `tools/incident/timeline_server.py` | Day 6. `get_incident_timeline` as a real stdio MCP server. |
| `docker/postgres/init/` | 18 incidents, 65 timeline events, seeded. |
| Docs/GitHub/Deployment agents | **Empty.** Days 8/11/12. This is Day 7's central design problem - see §6. |

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

Verified: `uv run pytest -q` runs all 141 including the 4 `integration`-marked tests with no
inline override. If those four start skipping again, this is why.

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
uv run pytest -q                                                   # expect 141 passed
uv run ruff check . && uv run ruff format --check . && uv run mypy  # all clean
```

Costs nothing. Last run: **141 passed**, lint and mypy clean, on `main` at `6792e82`.

## 6. Next work: Day 7 - delegation and the error taxonomy

**Done when:** a subagent receives context it never inherited; all four error classes return
distinctly.

### A track: the executor

`Coordinator.plan()` returns a `SelectionPlan` and stops. Day 7 is the executor that consumes
one and assembles a `CoordinatorResponse`. Put it in a new module beside `planner.py`.

**The seam is already built.** `IncidentAgent.diagnose` takes exactly what the coordinator
has:

```python
diagnose(query, *, context: str, request_id: str | None, invocation_id: str | None)
```

Pass `AgentInvocation.context_passed` straight in as `context`. That *is* the explicit
context passing, and it is how the done-when gets proved: assert the agent received exactly
`context_passed` and nothing the coordinator knew but did not pass.

**The central design problem: only the Incident agent exists.** A plan can legitimately
select `docs`, `github`, or `deployment`, and Days 8/11/12 are when those arrive. The
executor must decide what happens when it is handed an invocation it cannot run. The
contract-honest answer is a `Gap` with `resolvable: false`, plus a `status` of `partial` or
weaker. **Do not fabricate an `AgentResponse` for an agent that does not exist** - a
plausible placeholder is precisely the failure mode the null-vs-`[]` rule exists to prevent.
Decide this deliberately and write it down; it is the interesting decision of the day.

**`SelectionPlan` gives you** `intent`, `selected_agents`, `skipped_agents`, `gaps`, plus the
`parallel_group` and `sequential_chain` properties. **`CoordinatorResponse` additionally
needs** `request_id`, `query`, `received_at`, `agent_responses`, `synthesis`, `answer`,
`refinement_rounds`, `unresolved_gaps`, `status`, `cost`, `trace_id`, `completed_at`.

Fill the ones that are not yet earned honestly:

- `refinement_rounds = 0`. The loop is Day 14.
- `trace_id = None`. Langfuse is Day 9.
- `cost` from `Usage` in `aioc.llm.tool_use`, which accumulates `input_tokens` /
  `output_tokens` across every model call in a loop. Do not estimate it.

**The validator that will bite you.** `CoordinatorResponse._check` requires every id in
`answer.evidence` to exist in the union of `agent_responses[].evidence`, and requires
`answer.evidence` to be non-empty when `answer.value` is set and confidence >= 0.5. The
coordinator **cites its subagents; it does not mint its own evidence ids.** A synthesis step
that invents an id fails validation. `intent` is the one Assessment exempt from the evidence
requirement.

**Do not build Day 9 early.** Day 7 is delegation, Day 9 is real parallel execution. Running
the parallel group in a plain loop is correct for today; `mode` and `depends_on` are already
recorded, so Day 9 changes the execution strategy without touching the plan.

### B track: `correlate_events` + the four error classes

`tools/envelope.py` already implements the taxonomy with per-class requirements asserted at
construction, so this is mostly a second stdio server following `timeline_server.py`'s shape.

- The MCP boundary is JSON Schema, not Pydantic. **A tool server must not import
  `aioc.contracts`** - enums and input schemas stay longhand, with a test asserting the copies
  still match the Python enums.
- The four-part description template (CONTRACTS.md §6.5) is mandatory and ordered, including
  part 4 even when there is no alternative.
- All four classes must return *distinctly*: `transient` (the only retryable one, must set
  `retry_after_ms`), `validation`, `business`, `permission`. `isError` and `ok` always agree.
- An empty result is a success (`ok: true`, `meta.returned: 0`), not a `business` error.
- Known deviation to decide on, not inherit blindly: `timeline_server.py` has framework input
  validation deliberately off, so it returns plain text where the contract wants a structured
  `validation` error. Either fix both servers or repeat it consistently and say why.

### Testing

Follow `tests/test_coordinator.py`: a scripted fake client, mostly negative tests, one per
enforced rule. Zero API calls. The live counterpart, if you want one, belongs in `scripts/`
as an opt-in `check_*.py`.

## 7. Carried-over items, none blocking

1. **The two remotes' `main` histories have forked** by one merge commit each (§3). Content
   is identical. Reconciling needs a force push, so it needs asking.
2. **`TIMELINE_STORE_TIMEOUT` deviates from CONTRACTS.md §7.1**, which names
   `PROMETHEUS_TIMEOUT` for this tool. The events are in Postgres, so the contract's code
   would be a false value in a programmatically matched field. Additive, so patch level under
   §0. Needs an entry in `docs/design-notes/contract-changes.md` plus a §9 row. Flagged in the
   module docstring.
3. **Prompt caching not enabled.** The system prompt plus tool schema is byte-identical on
   every call and sits at the front of the prefix. Obvious win, not yet taken.
4. **Haiku is a Day 17 question, not a closed one.** It scored 1/3 on contract-valid
   structured output where Sonnet scored 3/3, and blind retry made it no cheaper than Sonnet.
   A retry loop that re-sends *with the validation error attached* should change that. The
   user wants Haiku; the answer is "once Day 17 exists".

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
like a model ignoring your schema - the surviving fragment is valid JSON with a missing tail.
That cost real time once and there is now a named regression test for it.

**When a new assertion fails on data you believe is right, the assertion is a hypothesis
too.** It has already happened twice here: a timeline-containment check that was wrong (the
data was right), and integration fixtures using 30-day windows against a 7-day contract cap.
Check the spec before changing the data.
