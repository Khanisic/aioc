# Handoff - point a new session here

Written at the end of **Day 15** of 30, after all four agents ran live on one query
(parallel and sequential paths in one request) and the cost review was done.
`CLAUDE.md` is loaded automatically and covers what the project *is*; this
file covers what a fresh session cannot infer from the code - live state, environment traps,
standing preferences, and what to do next.

Read this, then §6 below, then `docs/EXECUTION_PLAN.md` Day 16.

**This file is the only handover that exists.** The second engineer left after Day 6, so
anything true but unwritten is one forgotten detail away from being lost. Update it at the
end of every working day.

---

## 1. Standing preferences (these are not negotiable defaults, they are the user's)

- **Keep costs low.** The 581-test suite makes **zero API calls and zero network calls**
  and must stay that way - which is why tracing is opt-in at the entry point rather than
  activated by keys in `.env`. Live checks live in `scripts/check_*.py`, are opt-in, and
  cost 1-3 calls each. Before running anything live, say how many calls it will cost -
  and for a `respond()` run, quote the last measured run of the same shape (~$0.70-1.20,
  `scripts/cost_review.py`), not a guess from input sizes: both Day 15 estimates were
  low by 2-3x. Do
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
| **The frozen contract** | **Unchanged and still hard.** `schema_version` is `1.1.0` since Day 14 - the one pre-authorized split, made through the sec 0 process - and nothing else has moved. One head owning both sides makes the boundary easier to erode, not less necessary. |
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
| `contracts/` | Frozen at **`1.1.0`** since Day 14 (the pre-authorized split, §7.5/7.6; rationale in `docs/design-notes/contract-changes.md`), executable as Pydantic v2. Do not change a frozen shape. |
| `llm/` | Harness: `complete`, `stream_text`, `run_tool_loop`. Defaults `claude-sonnet-5`, 8192 tokens - both from measurement. |
| `agents/incident.py` | `investigate` (prose) + `diagnose` (schema-validated), with a `usage` accumulator (the Day 7 cost seam). The schema-annotation helper it pioneered now lives in `agents/_annotate.py`, shared with the Docs agent. |
| `agents/docs.py` | **Day 8.** `DocsAgent.answer`: retrieval first (injectable `CorpusRetriever` seam), documents rendered into the prompt, forced `emit_docs_report` tool, then grounding enforced in code - an unretrieved `document_id` or a paraphrased quote raises `DocsAgentError`. Coverage counters and the retrieval `ToolCallRef` are stamped by the runtime, never asked of the model. Registered in `default_runners()`. |
| `retrieval/` | **Day 8.** `embeddings.py` (Embedder protocol, Voyage client; `default_embedder()` is `None` without `VOYAGE_API_KEY`) + `corpus.py` (sha256-idempotent ingestion into `incident_embeddings`, pg_trgm + pgvector hybrid search, RRF fusion, honest `degraded` field for lexical-only mode). |
| `coordinator/planner.py` | Day 6. `plan()` returns a validated `SelectionPlan`; now rejects cyclic `depends_on`, takes a `usage` accumulator, and stamps `round` itself rather than asking the model (war story #7). Selection measured **5/5**. **Day 15 corrected the agent roster** (`_AGENT_CAPABILITIES`): it claimed the Incident agent reads the incident corpus and called Docs a runbook corpus, and the first four-agent plan skipped Docs quoting that sentence (war story #11). Incident has no tools; the corpus of past incidents is the Docs agent's. `test_the_roster_claims_no_capability_an_agent_lacks` pins it - update the roster when an agent's capabilities change. |
| `coordinator/executor.py` | Day 7, **parallel + traced Day 9, handoff Day 13, refinement loop + synthesis seam Day 14.** `Executor.execute(plan, query)` -> contract `CoordinatorResponse`. Explicit context passing proven by test; unrunnable agents produce `resolvable: false` gaps, never fabricated responses; cost measured, not estimated. The parallel group runs on a thread pool with per-runner `Usage` accumulators; the sequential chain composes each dependent's context from the planner's block plus `handoff.digest` of each direct dependency and records it verbatim. **The refinement loop** runs after the plan: every open gap that is `resolvable` and names a `suggested_agent` is re-delegated as a `round: 1+` invocation (query = `suggested_query` verbatim; context = planner's block + refinement block + the raising responses' digests; one invocation per agent per round; `max_refinement_rounds` default 2; an identical gap never asked twice; `resolvable: false` and unregistered agents never retried); a re-delegation that answers closes its gaps. **Synthesis is a seam**: `synthesiser=None` (default) is the deterministic Day 7 form; `ModelSynthesiser()` is opt-in at the entry point (like tracing), with a deterministic fallback recorded in `answer.reasoning`. `status` is `complete` only when nothing is open and each agent's latest report is complete. `respond()` = plan + execute in one call, and owns the request trace. |
| `coordinator/handoff.py` | **Day 13.** The digest: one bounded plain-text block per dependency response (facts, judgements with confidence and evidence ids, gaps with `suggested_query`, evidence refs; lists capped `(+N more)`, values clipped, a 4,000-char ceiling that cuts on a line and says so). Plain text so a tool-driven agent can quote a line as evidence through its context grounding. Direct dependencies only - a two-hop chain never snowballs. **Day 15:** the evidence list is kept out of the ceiling's cut (the body gives up the room - the synthesiser cites from that list, and a cut list made it cite document ids) and Docs claim lines say `docs=doc_0005`, so brackets mean evidence ids everywhere. **Day 14** added `refinement_block` (the gaps a re-delegated invocation is closing, with the `suggested_query` verbatim) and `refinement_query`. |
| `coordinator/synthesis.py` | **Day 14.** `SynthesisRequest` (query, intent, each response with its invocation, execution gaps, open gaps, rounds), `deterministic` (the Day 7 form), and `ModelSynthesiser`: one forced `emit_synthesis` call over the responses' digests, **flat schema** (`synthesis`, `answer`, `confidence`, `evidence`, `reasoning` - the nested `Assessment` form came back as XML-style text on the first live run, war story #10), `check_grounding` rejects an evidence id no agent carries or a confident uncited answer with `SynthesisError`. Verified live in one call over a recorded run. |
| `agents/_status.py` | **Day 13.** `settle_status`: `complete` over a null findings judgement becomes `partial` in every agent's runtime, that direction only. The first live sequential run was refused for exactly this. |
| `tools/incident/analyze_server.py` (+ `logs.py`, `patterns.py`) | **Day 13.** The `aioc-analyze` stdio server for the case study's two deliberately overlapping tools, now the **`1.0.0` baseline, kept runnable**. `analyze_logs` reads what a container printed through `docker compose logs`; `analyze_events` reads the seeded timeline. **Part 4 is the contract's v1.0.0 sentence verbatim and is pinned by `test_v1_part_four_is_the_contract_sentence_and_names_no_alternative`** - never sharpen it; `check_tool_routing.py --variant v1` must keep measuring the "before". |
| `tools/incident/search_server.py` | **Day 14.** The `aioc-search` server the `1.1.0` split produced: `search_container_logs` / `search_recorded_events`, the v1 implementation and input schemas imported under the new names, parts 1-3 composed from the v1 text verbatim (pinned by a test), part 4 naming the alternative and stating the discriminator both ways. `check_tool_routing.py --variant v1_1` lists it. No agent consumes either server yet. |
| `observability/tracing.py` | **Day 9.** The `Tracer`/`RequestTrace`/`AgentSpan` seam. `NullTracer` is the default everywhere; `default_tracer()` returns the `LangfuseTracer` adapter only when both keys are set (SDK client built lazily, injectable stub in tests). One trace per request; `agent:<name>` spans open/close in the worker thread that runs them, so a parallel plan shows overlapping spans; `ToolCallRef`s ride as child events carrying their measured timing; `CoordinatorResponse.trace_id` comes from the trace. Tracing activates **only** when an entry point passes a tracer - keys in `.env` alone cannot make tests emit spans. |
| `tools/envelope.py` + `tools/policy.py` | The contract wire envelope (sec 6) + the chaos ground-truth permission gate shared by all servers. |
| `tools/incident/timeline_server.py` | Day 6. `get_incident_timeline` stdio MCP server. Now also enforces the chaos gate. |
| `tools/incident/correlate_server.py` | **Day 7.** `correlate_events` stdio MCP server over the corpus (impulse Pearson over 60s event bins). All four error classes return distinctly - there is a named test producing each from real code paths. |
| `tools/incident/store.py` | Shared Postgres settings for both servers (the `.env` port override lives through this). |
| `tools/github/` | **Day 11.** `api.py` (read-only REST client, four-class error mapping: `GITHUB_SCOPE_MISSING` permission, `NOT_FOUND` business, `GITHUB_RATE_LIMITED`/`GITHUB_UNAVAILABLE` transient, `GITHUB_REJECTED_INPUT` validation) + `server.py` (the `aioc-github` stdio server: `get_pull_request`, `list_commits`, `diff_refs`; keys-only patch redaction; bounded output with honest `meta.truncated`). Verified over the wire against this repo, zero Claude calls. |
| `llm/mcp.py` | **Day 11.** `McpStdioToolset`: launches a stdio MCP server as a subprocess (same interpreter, `-m module`) and exposes its tools as `ToolSpec`s; the session lives on a dedicated thread so sync agents on the executor's worker threads can call it. Reuse it for Day 12. |
| `agents/github.py` | **Day 11.** `GitHubAgent.analyze`: opens the toolset (injectable `Toolset` seam), `run_tool_loop` with the three tools, then a forced `emit_github_report`. Facts stamped from the ledger of tool replies; an unfetched PR/SHA/`change_ref` or a paraphrased excerpt raises `GitHubAgentError`; each wire call is a contract `ToolCallRef`. Registered in `default_runners()`. Since Day 12 the `Toolset` seam and the ledger live in `agents/_toolset.py`, shared with Deployment. |
| `tools/deployment/` | **Day 12.** The `aioc-deployment` stdio server for the two contract-named tools. `release.py`: `diff_release` reads compose, `.env`-style, and Kubernetes manifests at both refs (`GitHubApi.file_content`) and diffs them structurally, every value hashed the moment it is parsed - keys, image references, manifest paths, and commits come back, values cannot. `health.py`: `check_rollout_health` runs a Prometheus battery for one service and applies the deterministic status rule; the deployed version is the demo app's new `service_build_info` gauge, which also gives the baseline (previous version in the window) and `rolled_back`. Sec 7.3/7.4 codes plus additive `PROMETHEUS_UNAVAILABLE`, `VERSION_NOT_OBSERVABLE`, `ENVIRONMENT_NOT_MONITORED`. Verified over the wire against the live stack. |
| `agents/deployment.py` | **Day 12.** `DeploymentAgent.assess`: the Day 11 shape over `aioc-deployment`. The model reports the service, the releases, and four judgements (`rollout_status`, `regression_suspected`, `rollback_recommendation`, `approval`); the runtime stamps `changed_config_keys`, `image_changes`, `health_signals` (lookback from the call's arguments) from the replies for those releases, refuses a service or release no tool returned, requires a gap against any tool's fields when that tool was not run (not looked is never `[]`), grounds every excerpt against the replies or the context block, and stamps `requires_approval: true`. Registered in `default_runners()` - the set is complete. |
| `docker/postgres/init/` | 18 incidents, 65 timeline events, seeded. `04-embeddings.sql` (Day 8) adds `incident_embeddings` - additive and `IF NOT EXISTS`, already applied to the running database by the ingest script's table guard. **Vectors are ingested** (18/18, `voyage-3.5`, 2026-08-21) and hybrid search is live. |
| `scripts/check_day15_integration.py` | **Day 15.** The four-agent checkpoint: deploys the demo at PR #11's merge commit, injects a real fault, builds the situation from live metrics plus the Day 13 release note, and runs `respond()` on a four-part query. Asserts all four planned and answering, a parallel group that overlapped in measured time, and the Day 13 sequential assertions (imported from `check_day13_sequential.py`, whose multi-round bug it found). Records `transcript.json`; resets chaos itself. **PASS on attempt 2**, `docs/assets/day15-demo.gif`. |
| `scripts/cost_review.py` | **Day 15.** Free. Prices every recorded live run by check and by day against the alert, and says what it cannot see (runs with no recorded usage, ad-hoc calls, Voyage). `--json`, `--since`, `--alert`. Prices are a table in the file, dated; no caching is assumed. |
| `scripts/demo_day10.py` + `render_demo_gif.py` | **Day 10.** The end-to-end demo (inject -> live metrics -> `respond()` traced; ~3 calls, `--skip-inject` and `--query` to vary) and the GIF renderer (free; PEP 723 inline pillow, replays the run's recorded transcript; `--title` / `--command` since Day 15 so it renders any recorded transcript). The checkpoint asset is committed at `docs/assets/day10-demo.gif`. |
| `demo-app/services/app.py` | **Day 12** added `service_build_info{service,git_sha} = 1` (the deployed version as a metric, next to `/version`). The image must be rebuilt for it to exist: `docker compose up -d --build --wait`. |

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

Verified: `uv run pytest -q` runs everything including the 10 `integration`-marked tests with no
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
- **The demo services report whatever `DEMO_GIT_SHA` they were started with** (`baseline` by
  default). `check_rollout_health` reads that from `service_build_info`, so a release under test
  must be deployed first: `DEMO_GIT_SHA=<sha> docker compose up -d --wait` recreates the
  containers with that identity (the Day 12 check's `--deploy` does exactly this), and a plain
  `docker compose up -d` later puts them back on `baseline`. Recreating a container is one
  restart and one failed scrape in the next health window, and the tool reports it as such.
- **The demo image must be rebuilt after `app.py` changes** (`docker compose up -d --build`).
  A stale image has no `service_build_info`, which the health tool reports honestly as
  `version: null` (or `VERSION_NOT_OBSERVABLE` when a version was asked about) rather than as
  a healthy unknown.

## 5. Verify the state in one go

```bash
uv sync --all-groups
docker compose up -d --wait
uv run pytest -q                                                   # expect 581 passed
uv run ruff check . && uv run ruff format --check . && uv run mypy  # all clean
```

Costs nothing. Last run: **581 passed** with the stack up (568 passed, 13 skipped without
it), lint and mypy clean, at the end of Day 15. The suite took ~25-45s on this machine with the
stack up; the `test_mcp_toolset.py` tests launch the real GitHub, deployment, analyze, and
search server subprocesses and are most of it.

The whole system has now been proven live end to end - the Day 10 demo (**~4 Claude
calls** per run since Day 14 added the synthesis call, needs the stack; `--skip-inject`
reuses active chaos, `--query` overrides the canonical query; not re-run on Day 14, so the
committed GIF still shows the deterministic synthesis):

```bash
PYTHONIOENCODING=utf-8 uv run python scripts/demo_day10.py
uv run scripts/render_demo_gif.py --run test-results/runs/<date>/<run-dir>   # free
```

`check_day15_integration.py --deploy` (~12-20 calls, **~$1.10 measured**, 343-348k input
tokens both times) is the whole system on one query: four agents, the parallel and the
sequential path, the loop, the model synthesis. `--max-rounds 1` is what passed; the run
resets chaos itself, and a plain `docker compose up -d --wait` afterwards puts the demo back
on `baseline` (done at the end of Day 15). `uv run python scripts/cost_review.py` is free
and says what all of this has cost so far.

`check_day13_sequential.py --deploy` (~8-15 calls) is the Day 13 regression **and the
refinement loop's live test since Day 14**: `respond()` on a scenario where the release
identity is only reachable through the PR, with the loop (`--max-rounds`, default 2) and the
model-written synthesis (`--deterministic-synthesis` to skip) on. It asserts the plan is
sequential, that the invocation standing for Deployment (the last one that answered) carries
GitHub's digest after the planner's block and started after GitHub ended, and that every
`round >= 1` invocation carries the refinement block. It recreates the demo at the PR's
**merge commit** (Day 14 change - the head was the wrong release, war story #10); a plain
`docker compose up -d --wait` afterwards puts it back on `baseline` (done at the end of Day
14). Day 14's two attempts are in §7 item 18. `check_tool_routing.py` (20 calls per query
set, 40 for both; `--dry-run` free; `--variant v1` / `v1_1`) is the routing case study's
measurement; both the before and the after are recorded (§7 item 15).
`check_day12_deployment.py --deploy` (~3-5 calls) is the Day 12 regression: it recreates the
demo containers at `origin/main`, then the Deployment agent diffs two real refs and reads the
live rollout over the wire. `check_day9_trace.py --fake-agents` remains the zero-cost tracing smoke test, and Day 7's
delegation check (`check_day7_delegation.py`, 2 calls) is still worth re-running after any
coordinator prompt change. Remember `make chaos-reset` (or
`uv run python demo-app/chaos/inject.py --reset`) after a demo - injected chaos persists.

## 6. Next work: Day 16 - schemas everywhere + HITL

**From the plan:** A: all four agents on validated schemas - nullable fields (absent data
returns `null`, never a fabricated value), enums using the `other` + detail-string pattern.
B: human-in-the-loop approval gate for critical actions (rollback, restart, merge).

What already exists for it:

- The A half is largely built: all four agents already return schema-validated contract
  envelopes through forced structured-output tools, with `null` + `Gap` and `other` +
  detail enforced by the contract models and one negative test per invariant. What Day 16
  adds is the audit that says so - run the `contract-audit` skill, then close what it
  finds - and the one enforcement the contract asks for that nothing does yet: consumers
  reading `schema_version` and failing loudly on a major mismatch (§7 item 19).
- The B half is new code in `src/aioc/hitl/` (empty today). The inputs are already
  stamped: `DeploymentFindings.approval` (`requires_approval: true` since Day 12) and
  `RecommendedAction.requires_approval` on the Incident agent's actions. Nothing in the
  executor consumes them. Day 17's audit log (B) records what the gate decides, so give the
  gate a decision record now rather than retrofitting one.
- **Do item 22 first or alongside** - it is the cheapest large cost win on the table and it
  changes what every live check after it costs.

<!-- superseded Day 15 notes follow, kept for the record -->
**Day 15 as planned:** checkpoint: a multi-agent query exercising parallel *and* sequential
paths; both tracks: cost review against the $100 alert. Done; see §3, §7 items 22-25, war
story #11, and `docs/interview-prep/numbers.md` (PASS on the second attempt, 2026-09-18;
measured spend $7.14, 7% of the alert).


**From the plan (Day 15):** checkpoint: a multi-agent query exercising parallel *and* sequential
paths. Both tracks: cost review - check Console spend against the $100 alert.

What already exists for it:

- Every piece of the checkpoint has been proven live on its own: Incident + Docs in
  parallel (Day 10), GitHub -> Deployment sequential with the handoff (Day 13), the
  refinement loop re-delegating a failed agent and a cross-agent gap (Day 14, attempt 1),
  and the model-written synthesis grounding a real answer (Day 14). What has not been run
  is one query that needs all four - the natural shape is an incident whose suspect is a
  recent deploy: Incident + Docs parallel, GitHub -> Deployment sequential, one synthesis.
  Expect ~12-20 calls and 400k+ input tokens at today's per-round re-sending (item 16);
  say the cost before running it.
- `demo_day10.py` already takes `--query` and now passes `ModelSynthesiser()`; a four-agent
  query through it would record the transcript for a Day 15 GIF the same way Day 10 did.
  `check_day13_sequential.py` is the sequential half with the loop on.
- **The cost review is overdue in one specific way:** every Claude call re-sends the tool
  schema and system prompt, and the tool-driven agents re-send a ~22k-token PR read and a
  ~7k-token diff reply on every round (item 16). Prompt caching (item 4) and the Day 21
  trimming are the two levers; the three Day 14 sequential attempts cost 373.7k, 232.7k,
  and 344.0k input tokens (item 18). Langfuse has every trace; the Console has the spend.
- Day 16's HITL gate reads `DeploymentFindings.approval` (stamped `requires_approval: true`
  by the agent since Day 12) and `RecommendedAction.requires_approval`; nothing in the
  executor consumes them yet. Day 17's validation-retry loop is the general answer to the
  agent-level refusals that the refinement loop retried blind on Day 14 (a paraphrased
  excerpt, a health reply not covering `to_version`): re-request with the error attached.

<!-- superseded Day 14 notes follow, kept for the record -->
**Day 14 as planned:** A: the refinement loop. B: split/rename the overlapping tools, re-run the
same 20 (40) queries, record the new rate, draft the case study. Done; see §3, the case study
is `docs/case-study-tool-routing.md` (0/40 -> 0/40, with the levers named), and the live
sequential run is §7 item 18.

<!-- superseded Day 13 notes follow, kept for the record -->
**Day 13 as planned:** A: the sequential dependency path with a code comment on why it cannot
be parallel. B: the two overlapping tools, 20 queries, the misrouting rate. Done; see §3, the
live check passed on the second attempt on 2026-09-13 (war story #9 is the first attempt),
and the baseline is recorded in `docs/interview-prep/numbers.md`.

What already existed for the sequential path:

- The planner already emits `mode: sequential` with a non-empty `depends_on` for the
  `sequential_dependency` case (measured 5/5), and the executor already runs the chain in
  dependency order after the parallel group, failing dependents honestly when a
  dependency produced nothing (`_dependency_unmet_gap`).
- **What is missing is the handoff itself.** Today `context_passed` for every invocation
  is written by the planner *before* anything runs, so Deployment's context cannot carry
  what GitHub found. Day 13 is the executor composing the dependent's context from the
  dependency's response - a structured digest (BUILD_PLAN Phase 4: "pass Incident's
  digest to Deployment, not the raw dump"), appended to the planner's block, never
  replacing it, and recorded verbatim in the invocation's `context_passed` so the
  explicit-passing test still holds argument for argument.
- The Deployment agent already grounds excerpts against its context block as well as its
  tool replies (a context quote keeps `tool_call_id` null), so a digest passed in context
  can be cited as evidence without a grounding failure. `diff_release` takes any git ref,
  so GitHub's `head_sha` / base ref flow straight into `from_version` / `to_version`.
- Both agents are on the wire and both live checks pass; the natural Day 13 live proof is
  `respond()` on the sequential case with both spans in one Langfuse trace, the second
  starting after the first ends.

For the overlapping tools: there is no log store in the stack (the demo app logs to stdout
and nothing collects it), so `analyze_logs` needs a source before it can misroute against
`analyze_events` (which can read the seeded timeline). Decide that first; a tool over
`docker compose logs` is the cheapest honest option. `check_agent_selection.py`'s five cases
are the routing baseline to extend to 20.

<!-- superseded Day 12 notes follow, kept for the record -->
**Day 12 as planned:** A: Deployment agent - compare releases, check rollout health. B:
`diff_release` and `check_rollout_health` custom tools. Done; see sec 3, and the live check
passed first time on 2026-09-09 (numbers in `docs/interview-prep/numbers.md`).
`check_day12_deployment.py --deploy` is the regression to re-run after any change to the
Deployment agent's prompt or schema guidance.

**From the plan:** A: Deployment agent - compare releases, check rollout health. B:
`diff_release` and `check_rollout_health` custom tools (CONTRACTS.md sec 7.3, 7.4 - these
two ARE contract-named tools, so their input/output schemas are frozen; read sec 7 first).

What already exists for it:

- `DeploymentFindings` is frozen (sec 4.4) and executable. `changed_config_keys` is keys
  only and `approval` is an `ApprovalRequirement` - the Day 16 HITL gate reads it.
- **The pattern to copy is Day 11 end to end:** a stdio server under `tools/deployment/`
  following `tools/github/server.py` (envelope, hand validation, four-part descriptions),
  consumed by the agent through `McpStdioToolset.for_module(...)` exactly as
  `agents/github.py` does, with facts stamped from the tool replies and ungrounded
  references rejected. The `Toolset` protocol and `_Ledger` idea in `github.py` are worth
  lifting into a shared helper once a second agent needs them.
- Data source: the demo app exposes `/version` per service (`DEMO_GIT_SHA`), Prometheus
  has the rollout signals, and `diff_release` can compare git refs through the Day 11
  `GitHubApi.compare` - the release diff is the GitHub diff plus the config-key diff.
- Registration: `default_runners()` + the pinning test, same as every agent day.
- Day 13 chains GitHub -> Deployment sequentially; today's agent only needs to stand alone.

The Day 11 live check has passed (2026-08-23, numbers in `docs/interview-prep/numbers.md`);
`check_day11_github.py` (~2-3 calls) is the regression to re-run after any change to the
GitHub agent's prompt or schema guidance.

<!-- superseded Day 11 notes follow, kept for the record -->
**Day 11 as planned:** A: GitHub agent - read repos, analyze PRs, explain diffs. B: GitHub MCP
server wired in, scoped credentials, repo access verified. Done; see sec 3.

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
10. ~~The Anthropic API key was rejected (401) on 2026-08-23.~~ **Resolved the same day by
   rotating the key**; the Day 11 live check then passed. The WIF notes below stay because
   they are the keyless path for CI. Original record: the key was rejected on a
   free `models.list` call, after working for both Day 10 demo runs the day before. Nothing
   in the repo touched it (the `.env` value hashes identically to what `LLMSettings` loads,
   and no shell variable shadows it), so it was revoked or rotated at console.anthropic.com.
   Until it is replaced, every live script fails before doing anything; the offline suite
   is unaffected. The Day 11 live check is the first thing to run afterwards.

   **Two ways to replace it - a new key, or no key at all.** Verified against the platform
   docs on 2026-08-23 (the earlier in-session claim that the direct API is keys-only was
   wrong and is superseded by this item):

   - **Workload Identity Federation is supported directly on the Anthropic API** - no
     Bedrock or Vertex needed. Release notes date the launch to **2026-05-04**; the docs
     page (`platform.claude.com/docs/en/manage-claude/workload-identity-federation`) does
     not print a GA date, so treat "GA on 2026-06-17" as unconfirmed by the docs. The
     workload presents an OIDC JWT from an IdP you run (GitHub Actions, AWS, GCP, Entra
     ID, Kubernetes, SPIFFE, Okta, or any OIDC issuer); Anthropic exchanges it at
     `POST /v1/oauth/token` (RFC 7523 jwt-bearer) for a short-lived `sk-ant-oat01-...`
     token bound to a **service account** (`svac_...`) under a **federation rule**
     (`fdrl_...`) on a registered **issuer** (`fdis_...`). Console: Settings -> Workload
     identity -> Connect workload creates all three. Default scope `workspace:developer`
     (same access as a key); `workspace:inference` is the tighter Messages-only scope.
     Token lifetime 60-86400 s (default 3600; the wizard prefills 600).
   - **The installed SDK already supports it** (`anthropic 0.119.0` exposes
     `WorkloadIdentityCredentials` / `IdentityTokenFile`), and `LLMClient` needs no
     change: it constructs `anthropic.Anthropic(api_key=None)` when no key is set, and the
     SDK then resolves credentials by precedence - constructor arg, then
     `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`, then `ANTHROPIC_PROFILE`, then the
     federation env vars, then the active profile. Verified offline: with the key unset and
     dummy federation vars exported, the client reached the real exchange endpoint.
   - **Zero-argument setup is four exported variables:** `ANTHROPIC_FEDERATION_RULE_ID`,
     `ANTHROPIC_ORGANIZATION_ID`, `ANTHROPIC_SERVICE_ACCOUNT_ID`, and one of
     `ANTHROPIC_IDENTITY_TOKEN_FILE` / `ANTHROPIC_IDENTITY_TOKEN` (plus
     `ANTHROPIC_WORKSPACE_ID` when the rule spans workspaces). Or a profile file at
     `%APPDATA%\Anthropic\configs\<name>.json` (Windows) / `~/.config/anthropic/...`,
     which Claude Code honours too.
   - **Three traps, all from the docs:** (1) `ANTHROPIC_API_KEY` *shadows* federation, and
     an *empty* `ANTHROPIC_API_KEY=""` still wins its slot - unset it, never blank it.
     (2) The SDK reads the federation variables from the process environment; values in
     `.env` are loaded by pydantic-settings into `LLMSettings`, not exported, so they
     must be in the shell (or a profile file) for the SDK to see them. (3) Every exchange
     denial is an opaque `401 Authentication failed`; the real reason is in the Console's
     authentication-history tab, not the response.
   - **What WIF does not solve on this laptop:** a federated workload needs an IdP that
     will sign a JWT for it. GitHub Actions, a cloud VM, or Kubernetes have that ambiently;
     a Windows dev box does not, short of an Okta/Entra client-credentials app or a GCP
     identity token via `gcloud`. So the practical split is: **rotate the key for local
     work now**, and adopt WIF for CI (Day 22's GitHub Actions work is the natural home -
     the `token.actions.githubusercontent.com` issuer with a `repo:m-misbahuddin/aioc:*`
     subject prefix, no secret in the pipeline).
   - **Code impact when WIF is adopted:** the eight `scripts/check_*.py` / demo scripts
     gate on `LLMSettings().anthropic_api_key is None` and exit 2 - under federation that
     guard is wrong and should become "a key OR the four federation variables". Nothing
     else in the harness assumes a key.
11. **Five more additive error codes** joined item 3's paperwork queue with the GitHub
   server: `NOT_FOUND`, `GITHUB_RATE_LIMITED`, `GITHUB_UNAVAILABLE`, `GITHUB_REJECTED_INPUT`,
   `REPOSITORY_NOT_CONFIGURED`. (`GITHUB_SCOPE_MISSING` is already in sec 6.4.) The same
   `1.0.1` entry clears them all.
12. **Three more from the deployment server (Day 12):** `PROMETHEUS_UNAVAILABLE` (transient -
   unreachable is not a timeout, and the contract's `PROMETHEUS_TIMEOUT` would be a lie for
   a refused connection), `VERSION_NOT_OBSERVABLE` (business - the running image exports
   no `service_build_info`, so a requested version can be neither confirmed nor ruled
   out), and `ENVIRONMENT_NOT_MONITORED` (business - this stack is one environment, and
   `production` must not be answered with `development` numbers). All flagged in the
   server's module docstring; the same `1.0.1` entry clears them. The sec 7.3 / 7.4 codes
   themselves (`SAME_VERSION`, `UNKNOWN_RELEASE_VERSION`, `REGISTRY_UNAVAILABLE`,
   `REGISTRY_SCOPE_MISSING`, `UNKNOWN_SERVICE`, `VERSION_NOT_DEPLOYED`, `INVALID_LOOKBACK`,
   `PROMETHEUS_TIMEOUT`) are contract-named and need no paperwork.
13. **`diff_release` input tokens are the next trimming target.** The Day 12 live run's diff
   reply was ~7.4k tokens by `meta.token_estimate`, mostly commit messages (up to 1000
   chars each, 16 commits between the refs), and it was re-sent on every round: 62.7k input
   tokens for one question. Day 21 owns this; the honest fix is a first-line-only commit
   message with a `message_truncated` flag, or `include: config` by default and commits on
   request.
14. **`pyyaml>=6.0` is a new runtime dependency** (`types-PyYAML` for mypy). The release
   diff parses manifests structurally rather than pattern-matching patch lines, which is
   what makes "an image that moved because a block was re-indented" a non-change and a
   value change a `changed` key rather than removed-then-added.
9. **`langfuse>=4.14.4` is a new runtime dependency** (the v4 observation API is what the
   adapter targets). It pulls the OTel SDK; nothing imports it unless a `LangfuseTracer`
   actually starts a request, so offline cost is import weight only.
15. **The routing case study is 0/40 before and 0/40 after (Days 13-14).** With parts 1
   and 3 of the sec 6.5 template stating what each tool reads, Sonnet never needed part 4:
   0/20 + 0/20 with the weak v1 descriptions, and 0/20 + 0/20 with the `1.1.0` names and a
   part 4 that names the alternative (2026-09-17, 125.9k input tokens, +10% per call for
   the longer part 4). `docs/case-study-tool-routing.md` says what that means and names
   the two levers that would produce a non-zero baseline - a cheaper router
   (`--model claude-haiku-4-5-20251001`, also the Day 23 question) and parts 1-3 written
   as loosely as part 4 (a second `v1` module in `VARIANTS`). Neither was run unasked;
   the standing rule says no model matrix without a request.
16. **The sequential run costs ~264k input tokens, and it is not the handoff's fault.**
   PR #15 is 27 files and +4.6k lines; `get_pull_request` returned ~22k tokens that were
   re-sent on every GitHub round, and Deployment's ~7k-token diff reply on every
   Deployment round. The handoff itself was ~3k characters. Day 21 owns the trimming
   (first-line-only commit messages, `include` filters by default, a `max_patch_chars`
   on the PR read); until then, pick a smaller PR for demos, or pass `--pr`.
17. **`status` is now settled by the runtime in one direction** (`agents/_status.py`):
   `complete` over a null judgement becomes `partial`. This is a behaviour change in all
   four agents, covered by a Deployment test and the existing envelope tests. A reported
   `partial` / `insufficient_evidence` / `error` is never raised. The general fix for a
   report the contract refuses is still the Day 17 validation-retry loop.
18. **The Day 14 live sequential run, three attempts (2026-09-17).** Attempt 1 exercised the
   loop end to end and failed the check honestly: GitHub refused its own report in round 0
   (a paraphrased excerpt), the loop retried it in round 1 and it answered with a gap
   pointing at Deployment, the loop ran Deployment in round 2 with GitHub's digest (4,303
   chars of context) and Deployment refused its own report because the demo was deployed
   at the PR head while GitHub had (correctly) named the merge commit as the release; the
   synthesis fell back because the nested `answer` object came back as XML-style text.
   373.7k input tokens, 175 s, trace `0dc1c1b0ec90232f3fa96ed68c62174d`. Fixes: the check
   deploys the merge commit (resolved from the local clone; exposing `merge_commit_sha` on
   the PR reply was tried in attempt 2 and led the model to report an unfetched commit), the synthesis
   schema is flat, and the check evaluates the invocation that stands for each agent.
   Attempt 2: failed in a third way, and it was the fix's fault: the new `merge_commit_sha`
   field in the PR reply led the model to report the merge commit as a commit it had never
   fetched with `list_commits`, and the agent's grounding rule refused it in round 0 and
   again on the round-1 retry (232.7k input tokens, 132.5 s, `error`). The field was
   reverted; the check resolves the merge commit from the local clone instead.
   Attempt 3: **PASS** (trace `f89929cd56da402d6b94dc1f84286617`, 344.0k input / 18.6k
   output tokens, 191.9 s, `partial`, 4 unresolved gaps). Round 0: GitHub `partial` @
   0.75 (PR read, commits listed, one gap pointing at Deployment), Deployment `sequential`
   after it with a 3,399-char context carrying GitHub's digest, `complete` @ 0.62 (three
   config keys added, no image change, rollout `degraded`, one gap: `compared_to_baseline`
   null, wider lookback suggested). Round 1: the loop grouped both gaps onto one Deployment
   invocation depending on *both* round-0 invocations (7,246-char context, two digests);
   Deployment refused its own report (`from_version` not returned by a tool - the health
   baseline was null). Round 2: the retry (parallel, 1,313 chars) failed report validation
   (a gap with `suggested_agent` but no `suggested_query`). The model synthesis then
   grounded an answer at 0.65 on nine real evidence ids across both agents and said plainly
   that two follow-up attempts to close the baseline gap failed - which is the honest
   result: the loop re-delegated the right things and the agents' own rules refused twice.
   Both refusals are Day 17's (item 20).
19. **The `1.1.0` bump left the §0 step-3 tension on record, not resolved.** By the letter
   a rename is a removal (major); the pre-authorization fixed it at minor. The design-note
   entry says so. The §8 worked example still says `1.0.0` on purpose - a `1.0.0` payload
   is valid under `1.1.0`, and nothing enforces "fail loudly on a major mismatch" yet
   (no consumer reads `schema_version`). That enforcement is a small validator away and
   belongs with the Day 16 schemas-everywhere pass.
20. **Two live-only agent refusals the refinement loop cannot fix by retrying blind:** a
   GitHub excerpt paraphrase (retry worked once, by luck of sampling) and a Deployment
   report whose health reply did not cover `to_version` (retry would fail identically).
   Both are the Day 17 validation-retry loop's job - re-request *with the error attached* -
   and the refinement loop's identical-gap rule already stops it spending a second round
   on them.
21. **The Day 10 GIF predates the model-written synthesis.** `demo_day10.py` now passes
   `ModelSynthesiser()` and prints the synthesis; re-running it (~4 calls) and re-rendering
   the GIF is a Day 15 nicety, not a blocker.

22. **Out-of-scope gaps are the biggest cost lever found so far (Day 15).** Every agent
   receives the whole user query, so on a four-part question each one raised resolvable
   gaps for the parts that were not its own, pointing at the sibling that was already
   answering them ("no documentation describes PR #11" -> github). The loop did what it
   does with a resolvable gap: round 1 re-delegated **all four agents**, with contexts up
   to 18k chars, after the plan had already answered everything at 146.8 s. That round was
   about half of a ~$1.16 run, and the run ended `partial` with 14 open gaps on a correct
   answer. The contract has no per-invocation query (`AgentInvocation` is frozen), so the
   fix belongs in context composition, and the project's own rule says which kind:
   plumbing the runtime knows is stamped, not asked of the model. Recommended: the
   executor appends a short roster block to each invocation's context - which other
   agents are on this request and what each was asked for (their `reason`), and that a gap
   is for what *this* agent could not establish, never for a sibling's part - recorded
   verbatim in `context_passed` like the handoff. Offline-testable; one live re-run
   (~$0.60 if it works) gives the before/after. The alternative (teach the loop to drop a
   gap whose `suggested_agent` already answered this round) is a judgement call the loop
   was designed not to make (decision #22).
23. **A third live-only agent refusal joined item 20's two:** the Docs agent's report was
   rejected for an extra field the model invented (`findings.coverage_answer_placeholder`,
   `extra_forbidden`) on a round-1 re-delegation with a 5k-char context. Day 17's
   validation-retry loop owns all three; the GitHub paraphrase refusal has now happened three
   times in live runs (Day 14 attempt 1, then round 0 of Day 15 attempt 1 and round 1 of
   attempt 2), each costing a whole agent run.
24. **The selection check has no case shaped like the one that broke.**
   `check_agent_selection.py`'s five cases never ask for precedent and a live diagnosis in
   one query, which is why a wrong roster survived from Day 6 to Day 15 at 5/5. One more
   case costs one planning call. Worth adding before the Day 19 eval set is drawn up.
25. **Spend, measured (2026-09-18): $7.14** across every recorded live run (2.58M input /
   198k output tokens, Sonnet 5, no caching), 7% of the $100 alert - a floor, since 16
   early runs recorded no usage. **The Console figure has not been compared with it yet**;
   that is a two-minute check only the account owner can do, and a Console number far
   above the floor means unrecorded spend worth finding. Prompt caching (item 4) stays on
   Day 19 by decision #27.

## 8. Where things are written down

| Need | File |
|---|---|
| What the project is, conventions, layout | `CLAUDE.md` (auto-loaded) |
| The frozen contract | `docs/CONTRACTS.md` - normative, read before touching a schema |
| Rationale for any frozen change, written before the code | `docs/design-notes/contract-changes.md` |
| Day-by-day plan and done-whens | `docs/EXECUTION_PLAN.md` |
| Running and reading tests | `docs/guides/running-tests.md` |
| The corpus schema and how to extend it | `docs/guides/incidents-table.md` |
| Why things are shaped this way | `docs/interview-prep/decisions.md` (Day 14 added #22-#24, Day 15 #25-#27) |
| Eleven debugging narratives worth not repeating | `docs/interview-prep/war-stories.md` (#10 is Day 14's, #11 Day 15's) |
| The routing case study, before and after | `docs/case-study-tool-routing.md` |
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
