# Handoff - point a new session here

Written at the end of **Day 10** of 30, after the first real end-to-end demo ran and the
checkpoint GIF was captured.
`CLAUDE.md` is loaded automatically and covers what the project *is*; this
file covers what a fresh session cannot infer from the code - live state, environment traps,
standing preferences, and what to do next.

Read this, then §6 below, then `docs/EXECUTION_PLAN.md` Day 11.

**This file is the only handover that exists.** The second engineer left after Day 6, so
anything true but unwritten is one forgotten detail away from being lost. Update it at the
end of every working day.

---

## 1. Standing preferences (these are not negotiable defaults, they are the user's)

- **Keep costs low.** The 253-test suite makes **zero API calls and zero network calls**
  and must stay that way - which is why tracing is opt-in at the entry point rather than
  activated by keys in `.env`. Live checks live in `scripts/check_*.py`, are opt-in, and
  cost 1-3 calls each. Before running anything live, say how many calls it will cost. Do
  not run a model matrix unasked. (Voyage embedding calls count too -
  `scripts/ingest_embeddings.py` is opt-in and its `--dry-run` is free. Langfuse spans are
  not Claude calls, but they are network - same opt-in rule.)
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

| Remote | Repo | Role |
|---|---|---|
| `origin` | `m-misbahuddin/aioc` | private, the user's own. **Merge here.** |
| `khanisic` | `Khanisic/aioc` | the shared repo; kept in sync by mirroring after each merge |

(Exact SHAs move daily - trust `git rev-parse main origin/main khanisic/main`, not this
file. If khanisic trails origin, the mirror push below is the fix.)

**The fork is resolved.** The two histories had diverged by one merge commit per repo for the
same content; on 2026-08-09 khanisic was force-pushed to match origin exactly. Both remotes
and local `main` are now the same commit, with identical trees and identical history.

Nothing was lost: khanisic's three extra commits were pure merge commits, and
`git diff <merge-base> khanisic/main` was empty - it had added no content of its own. Verify
that before any future force push rather than trusting this paragraph.

**To keep them in sync, merge in one place and mirror the result:**

```bash
# merge the PR on origin, pull it locally, then fast-forward the shared repo
git push khanisic main:main
```

That is a normal push now, not a force push, and it stays that way as long as nothing is ever
merged directly on khanisic. If a PR is merged there, the fork returns.

### What is live

| Area | State |
|---|---|
| `contracts/` | Frozen at `1.0.0`, executable as Pydantic v2. Do not change a frozen shape. |
| `llm/` | Harness: `complete`, `stream_text`, `run_tool_loop`. Defaults `claude-sonnet-5`, 8192 tokens - both from measurement. |
| `agents/incident.py` | `investigate` (prose) + `diagnose` (schema-validated), with a `usage` accumulator (the Day 7 cost seam). The schema-annotation helper it pioneered now lives in `agents/_annotate.py`, shared with the Docs agent. |
| `agents/docs.py` | **Day 8.** `DocsAgent.answer`: retrieval first (injectable `CorpusRetriever` seam), documents rendered into the prompt, forced `emit_docs_report` tool, then grounding enforced in code - an unretrieved `document_id` or a paraphrased quote raises `DocsAgentError`. Coverage counters and the retrieval `ToolCallRef` are stamped by the runtime, never asked of the model. Registered in `default_runners()`. |
| `retrieval/` | **Day 8.** `embeddings.py` (Embedder protocol, Voyage client; `default_embedder()` is `None` without `VOYAGE_API_KEY`) + `corpus.py` (sha256-idempotent ingestion into `incident_embeddings`, pg_trgm + pgvector hybrid search, RRF fusion, honest `degraded` field for lexical-only mode). |
| `coordinator/planner.py` | Day 6. `plan()` returns a validated `SelectionPlan`; now rejects cyclic `depends_on`, takes a `usage` accumulator, and stamps `round` itself rather than asking the model (war story #7). Selection measured **5/5**. |
| `coordinator/executor.py` | Day 7, **parallel + traced Day 9.** `Executor.execute(plan, query)` -> contract `CoordinatorResponse`. Explicit context passing proven by test; unrunnable agents produce `resolvable: false` gaps, never fabricated responses; synthesis deterministic until Day 14; cost measured, not estimated. The parallel group runs on a thread pool: every runner gets its own `Usage` accumulator, folded into the total after the join (the shared-`+=` race from the Day 8 handoff is closed by construction); results merge in plan order; a barrier test proves overlap offline. `respond()` = plan + execute in one call, and owns the request trace (`plan` span + agent spans). |
| `observability/tracing.py` | **Day 9.** The `Tracer`/`RequestTrace`/`AgentSpan` seam. `NullTracer` is the default everywhere; `default_tracer()` returns the `LangfuseTracer` adapter only when both keys are set (SDK client built lazily, injectable stub in tests). One trace per request; `agent:<name>` spans open/close in the worker thread that runs them, so a parallel plan shows overlapping spans; `ToolCallRef`s ride as child events carrying their measured timing; `CoordinatorResponse.trace_id` comes from the trace. Tracing activates **only** when an entry point passes a tracer - keys in `.env` alone cannot make tests emit spans. |
| `tools/envelope.py` + `tools/policy.py` | The contract wire envelope (sec 6) + the chaos ground-truth permission gate shared by all servers. |
| `tools/incident/timeline_server.py` | Day 6. `get_incident_timeline` stdio MCP server. Now also enforces the chaos gate. |
| `tools/incident/correlate_server.py` | **Day 7.** `correlate_events` stdio MCP server over the corpus (impulse Pearson over 60s event bins). All four error classes return distinctly - there is a named test producing each from real code paths. |
| `tools/incident/store.py` | Shared Postgres settings for both servers (the `.env` port override lives through this). |
| `docker/postgres/init/` | 18 incidents, 65 timeline events, seeded. `04-embeddings.sql` (Day 8) adds `incident_embeddings` - additive and `IF NOT EXISTS`, already applied to the running database by the ingest script's table guard. **Vectors are ingested** (18/18, `voyage-3.5`, 2026-08-21) and hybrid search is live. |
| `scripts/demo_day10.py` + `render_demo_gif.py` | **Day 10.** The end-to-end demo (inject -> live metrics -> `respond()` traced; ~3 calls, `--skip-inject` and `--query` to vary) and the GIF renderer (free; PEP 723 inline pillow, replays the run's recorded transcript). The checkpoint asset is committed at `docs/assets/day10-demo.gif`. |
| GitHub/Deployment agents | **Empty.** Days 11/12. The executor's `default_runners()` is where each one registers when it lands - nothing forces the registration (there is now a test pinning the current registration set), so wiring it is part of each agent's day. |

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

Verified: `uv run pytest -q` runs all 253 including the 10 `integration`-marked tests with no
inline override. If those ten start skipping again, this is why. (They also skip when Docker
Desktop itself is not running - the skip message says "connection timeout expired" either way,
so check `docker compose ps` before re-reading this section.)

`.env` cannot be read by an agent (denied by `.claude/settings.json`, correctly). To compare
a secret, hash it - that is how the port collision was diagnosed without ever reading the
password.

**Langfuse is the US region, and the host must say so.** The account's keys are valid only
against `https://us.cloud.langfuse.com`; the SDK default is the EU host, and the mismatch
presents as 401 "invalid credentials" with keys that are provably correct (same shape as
the Postgres trap above: the error names the wrong cause). `.env` carries
`LANGFUSE_HOST=https://us.cloud.langfuse.com`; the trap returns if `.env` is regenerated,
and `scripts/check_day9_trace.py` now fails fast with the region hint instead of silently
losing spans on a background thread.

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
uv run pytest -q                                                   # expect 255 passed
uv run ruff check . && uv run ruff format --check . && uv run mypy  # all clean
```

Costs nothing. Last run: **255 passed** (stack up, all 10 integration tests included),
lint and mypy clean, at the end of Day 10.

The whole system has now been proven live end to end - the Day 10 demo (**~3 Claude
calls** per run, needs the stack; `--skip-inject` reuses active chaos, `--query` overrides
the canonical query):

```bash
PYTHONIOENCODING=utf-8 uv run python scripts/demo_day10.py
uv run scripts/render_demo_gif.py --run test-results/runs/<date>/<run-dir>   # free
```

`check_day9_trace.py --fake-agents` remains the zero-cost tracing smoke test, and Day 7's
delegation check (`check_day7_delegation.py`, 2 calls) is still worth re-running after any
coordinator prompt change. Remember `make chaos-reset` (or
`uv run python demo-app/chaos/inject.py --reset`) after a demo - injected chaos persists.

## 6. Next work: Day 11 - the GitHub agent

**From the plan:** A: GitHub agent - read repos, analyze PRs, explain diffs. B: GitHub MCP
server wired in, scoped credentials, repo access verified.

What already exists for it:

- `GitHubFindings` is frozen in the contract (CONTRACTS.md §4.3) and executable in
  `aioc.contracts` - read both before writing the agent. The `GitHubAgentResponse`
  envelope already validates.
- The agent pattern to follow is `agents/docs.py` (Day 8): a forced structured-output
  tool whose schema is generated from the frozen models and annotated via
  `agents/_annotate.py`, runtime-stamped plumbing, honest gaps. The Docs agent is the
  better template than Incident because it also drives a tool loop before emitting.
- Registration is part of the day: add the runner to the executor's `default_runners()`
  and update `test_default_runners_register_incident_and_docs` (the test pins the set,
  so forgetting the wiring fails the suite - that is by design).
- **A `GITHUB_TOKEN` is needed** (accounts checklist): a fine-grained PAT with read-only
  Contents / Pull requests / Metadata on the target repo, plus `GITHUB_REPO=owner/name`.
  `.env.example` documents why read-only matters (the `permission` error class and the
  Day 16 HITL gate are theatre against an over-scoped token).
- The plan's Day 13 sequential path (GitHub reads the PR -> Deployment diffs the release)
  is downstream; today's agent only needs to stand alone.

The coordinator already routes github-shaped queries (selection was measured 5/5 with a
`sequential_dependency` case), and the executor turns a selected-but-unregistered agent
into an honest `agent_not_implemented` gap - so the day is done when that gap disappears
for github queries.

## 7. Carried-over items, none blocking

1. ~~The two remotes' `main` histories have forked.~~ **Resolved 2026-08-09** by force-pushing
   khanisic to match origin (§3). Keep it resolved by never merging directly on khanisic.
2. ~~No VOYAGE_API_KEY yet, so the vectors are not ingested.~~ **Resolved 2026-08-21**: the
   key is in `.env`, all 18 rows are embedded (`voyage-3.5`, one batch call), a re-run
   embeds 0 (idempotence verified live), and hybrid search returns fused vector+lexical
   results. Ingesting exposed a real bug first - the settings' `.env` path pointed one
   level above the repo (copied from the deeper `store.py`), so the key read as unset and
   the lexical fallback hid it. Fixed in PR #8 with a regression test pinning the path.
3. **Three additive error codes await the §0 paperwork**: `TIMELINE_STORE_TIMEOUT` and
   `EVENT_STORE_TIMEOUT` (both replacing the contract's `PROMETHEUS_TIMEOUT` where the store
   is actually Postgres) and `CHAOS_SCOPE_REQUIRED` (the Day 7 permission gate). All additive,
   so patch level under §0: one dated entry in `docs/design-notes/contract-changes.md`, a §9
   row, and a bump to `1.0.1` would clear all three at once. Each is flagged in its module
   docstring; none is done silently.
4. **Prompt caching not enabled.** The system prompt plus tool schema is byte-identical on
   every call and sits at the front of the prefix. Obvious win, not yet taken.
5. **Haiku is a Day 17 question, not a closed one.** It scored 1/3 on contract-valid
   structured output where Sonnet scored 3/3, and blind retry made it no cheaper than Sonnet.
   A retry loop that re-sends *with the validation error attached* should change that. The
   user wants Haiku; the answer is "once Day 17 exists".
6. **Delegation is verified live on one query, not a set.** `scripts/check_day7_delegation.py`
   passes (2 calls: one plan, one diagnose) and is worth re-running after any coordinator
   prompt change, but the routing check has five cases and this has one. Adding cases costs
   2 calls each. `check_day8_docs.py` has the same single-query shape.
7. ~~The Docs agent has not been proven live yet.~~ **Resolved 2026-08-22 by the Day 10
   demo (run 2)**: the Docs agent answered live from the seeded corpus through the
   executor - 7 supported claims across 4 documents, every quote verbatim (the in-code
   grounding checks passed on a real model response). `check_day8_docs.py` (1 call)
   remains available as the dedicated single-agent form but is no longer a pending proof.
8. ~~The Day 9 trace artifact does not exist yet - no Langfuse account.~~ **Resolved
   2026-08-22**: keys are in `.env` (US region - see the §4 trap; the first attempt
   401'd against the EU default and crashed on a `trace_url` nicety, both now fixed and
   regression-tested), and `check_day9_trace.py --fake-agents` **passed** for zero Claude
   calls (trace `a15143c60aa6fd3c8b97c18ad2eb97dc`). The real-request form is also done:
   both Day 10 demo runs traced end to end, run 2 with Incident + Docs spans in parallel
   (trace `812168341e05075daf5a96571cee75c0`).
9. **`langfuse>=4.14.4` is a new runtime dependency** (the v4 observation API is what the
   adapter targets). It pulls the OTel SDK; nothing imports it unless a `LangfuseTracer`
   actually starts a request, so offline cost is import weight only.

## 8. Where things are written down

| Need | File |
|---|---|
| What the project is, conventions, layout | `CLAUDE.md` (auto-loaded) |
| The frozen contract | `docs/CONTRACTS.md` - normative, read before touching a schema |
| Rationale for any frozen change, written before the code | `docs/design-notes/contract-changes.md` |
| Day-by-day plan and done-whens | `docs/EXECUTION_PLAN.md` |
| Running and reading tests | `docs/guides/running-tests.md` |
| The corpus schema and how to extend it | `docs/guides/incidents-table.md` |
| Why things are shaped this way | `docs/interview-prep/decisions.md` (Day 9 added #15 and #16) |
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
