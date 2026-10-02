# AIOC — 30-Day Execution Plan

Companion to `BUILD_PLAN.md`. That document says **what** to build and why.
This one says **what gets built on which day, and how you know it's done.**

> **Staffing changed after Day 6.** This plan was written for two engineers. The second
> engineer left; one maintainer now owns both layers. The **A** and **B** labels below are
> kept on every day, but they no longer name people - they name **layers**, and that is
> now their whole job: they mark which side of the frozen contract a day's work sits on.
> Read them as a reminder to finish one side before starting the other, not as a handoff.

---

## Ground rules

**Cadence.** The plan is 30 *working days*, not calendar days. With one engineer doing
both tracks, a numbered day is now closer to two sittings than one:

| Availability | Calendar duration |
|---|---|
| Full-time, one engineer | ~10–12 weeks |
| ~20 hrs/week | ~20 weeks |
| ~12 hrs/week | ~30 weeks |

Days are numbered, not dated. Don't re-plan when you slip — just move the pointer.
The original two-engineer estimates (~6 weeks full-time) are gone, not merely optimistic.

**Tracks.** Each numbered day still splits into two, and the split is load-bearing:

- **A — Reasoning Layer.** Orchestrator, four subagents, context passing, output
  schemas, evals. Domains 1, 4, and half of 5.
- **B — Platform Layer.** MCP tools, Claude Code config, CI, demo environment,
  deployment, observability. Domains 2, 3, and half of 5.

Do A and B of a given day in separate sittings, and make each cross the contract only at
the wire. The two-engineer structure was what kept the layers honestly decoupled; with
one person it is easy to reach across the boundary because both sides are in your head.
The contract is frozen precisely so that convenience is not available.

**Rituals.** These were built around a daily sync that no longer has a second party. What
survives is the part that was never about coordination:

- Start each working day by reading `HANDOFF.md`, then re-reading the previous day's
  done-when before writing code. This replaces the sync - the check is against the plan
  rather than against a colleague.
- Day 5 of every sprint is an **integration day**. No new features. This one matters
  *more* alone, not less: nobody else will hit your interface and find it wrong.
- Every sprint ends with a working demo, even if thin.
- Anything you would have said out loud in a sync and then forgotten goes into
  `HANDOFF.md` §6 as a carried-over item.

**The one hard rule.** The tool contract and output schemas are frozen on **Day 1**.
Everything else can churn. If those churn, you lose a week to integration pain.

---

## Accounts checklist (Day 0, 1 hour)

Already covered: Claude Max, Claude Code.

- [ ] **Anthropic Console** — API key + prepaid credits. *Max does not cover programmatic
      API or CI usage.* Set a spend alert at $100.
- [ ] **GitHub** — repo, Actions enabled, `ANTHROPIC_API_KEY` in repo Secrets, GHCR for images
- [ ] **Neon** or **Supabase** — Postgres **+ pgvector** (this *is* your vector database;
      it covers both the episodic and semantic memory tiers in one service)
- [ ] **Upstash** — Redis (working memory)
- [ ] **Voyage AI** — embeddings key (Day 8; Anthropic has no embeddings endpoint).
      Optional in the sense that retrieval degrades to lexical-only without it.
- [ ] **Langfuse Cloud** — public/secret keys
- [ ] **Railway** or **Render** — linked to the GitHub repo

**No signup needed — self-hosted in `docker-compose.yml`:** Prometheus, Grafana, the demo
app services. Running these locally is deliberate: the chaos scripts need metrics you
control, and a hosted account adds an auth dance for no benefit.

**Optional, worth considering:** a free **Notion** workspace (10–15 runbook pages + the
Notion MCP server) gives the Docs agent a genuine external knowledge source instead of
seeded local files, and gives you a second MCP server to compare tool descriptions
against — which strengthens the Domain 2 case study. Roughly one day of work.
- [ ] *(optional)* Slack free workspace, Grafana Cloud, Loom

Store everything in `.env.example` (committed, no values) + `.env` (gitignored).

---

# Sprint 1 — Foundations (Days 1–5)

*Goal: repo, config layer, demo environment, and one agent that returns valid JSON.*

### Day 1 — Integration: contracts and scaffold
- **Both tracks (first sitting):** Kickoff. Write `docs/CONTRACTS.md` — the four agent output schemas
  (field names, types, nullable fields, enums) and the tool interface signature. **Freeze it.**
- **A (second sitting):** Draft Pydantic models for all four agent outputs.
- **B (second sitting):** Repo scaffold per `BUILD_PLAN.md` structure, `docker-compose.yml`
  with Postgres + Redis, `.env.example`.
- **Done when:** `docker compose up` runs clean and `CONTRACTS.md` is merged.

### Day 2 — Harness and Claude Code config
- **A:** Claude API harness — messages, streaming, and a working `tool_use` loop.
- **B:** Domain 3 config layer: `CLAUDE.md` hierarchy (project + directory scopes),
  `.claude/rules/` with globs (`agents/**`, `tools/**`, `**/*.test.*`), two custom
  slash commands.
- **Done when:** A can round-trip a tool call; B's rules demonstrably fire in a session.

### Day 3 — First agent, first services
- **A:** Incident agent skeleton — system prompt (expert SRE, cite evidence, estimate
  confidence), single-turn, no tools yet.
- **B:** Demo app — 2–3 containerized services with Prometheus scraping them.
- **Done when:** Incident agent returns prose; demo app exposes metrics.

### Day 4 — Structured output, chaos
- **A:** Incident agent returns schema-validated output via `tool_use` + `tool_choice`.
- **B:** Chaos scripts — four injectable failure modes: memory leak, bad config deploy,
  slow downstream dependency, 500-spike tied to a specific commit.
- **Done when:** `make chaos-<mode>` reliably breaks the demo app.

### Day 5 — Integration day: wiring + seed data ✅
- **Both tracks:** Wire agent to real Prometheus data. Seed 15–20 synthetic historical incidents
  into Postgres (these become the RAG corpus *and* the eval set later).
- **Checkpoint:** Break the app → Incident agent produces valid JSON about it.
- **Done:** `aioc.observability.prometheus` renders live metrics as the agent's context block;
  18 incidents + 65 timeline events seeded. Checkpoint verified live by
  `scripts/check_day5_checkpoint.py` — injected `downstream_latency`, diagnosed
  `downstream_latency` @ 0.55 naming both affected services, contract-valid.
  `chaos_knob_value` is excluded from agent context by an enforced guard, so the comparison
  is a diagnosis rather than a transcription of the answer key.

---

# Sprint 2 — Orchestrator + two agents (Days 6–10)

*Goal: coordinator delegating to Incident and Docs in parallel.*

### Day 6 — Coordinator, first custom tool ✅
- **A:** Coordinator skeleton — intent classification, **dynamic agent selection**
  (invoke only what the query needs), `allowedTools` includes `Task`.
- **B:** First custom MCP tool `get_incident_timeline` — description carries inputs,
  example queries, edge cases, when-to-use-vs-alternative.
- **Done when:** Coordinator picks agents correctly on 5 sample queries.
- **Done:** `aioc.coordinator.Coordinator.plan` returns a validated `SelectionPlan`; the graded
  behaviours are enforced by validators rather than prompted (all four agents accounted for,
  `context_passed` non-empty *and* not a restatement of the query, `depends_on` resolving inside
  the plan). `get_incident_timeline` is a real stdio MCP server reading the seeded corpus, with
  the four-part description template and the four-class error taxonomy asserted by tests.
- **Selection measured 5/5.** All five sample queries in the done-when are verified live:
  `narrow_incident` and `sequential_dependency` on Day 6, then `pure_docs`,
  `incident_plus_docs_parallel` and `deployment_only`. Every case selected exactly the
  expected agents, accounted for all four, and gave a reason per skipped agent.
  Re-run with `uv run python scripts/check_agent_selection.py --all` (5 API calls).
- **Carried to Day 7:** `allowedTools`/`Task` wiring lands with delegation - the plan is
  built but not executed.

### Day 7 — Delegation and error taxonomy ✅
- **A:** Task delegation with **explicit context passing** in each subagent prompt.
  No implicit inheritance — this is the most-tested orchestration fact.
- **B:** `correlate_events` tool + structured `isError` responses across all four classes
  (transient / validation / business / permission).
- **Done when:** Subagent receives context it never inherited; all four error classes return distinctly.
- **Done:** `aioc.coordinator.Executor.execute` consumes a `SelectionPlan` into a contract
  `CoordinatorResponse`; `respond()` glues plan + execution into one call.
  The done-when is a named test: the runner receives *exactly* `AgentInvocation.context_passed`,
  character for character, and nothing the coordinator knew but did not write into the plan.
  An invocation the executor cannot run (only Incident exists until Days 8/11/12) produces a
  `Gap` with `resolvable: false` and a weakened `status` - never a fabricated `AgentResponse`.
  Synthesis is deterministic until the Day 14 loop needs a model-written one; `cost` is
  accumulated from `Usage` across the planning call and every agent call, never estimated.
- **Done (B):** `correlate_events` is live as a second stdio MCP server over the seeded corpus
  (`impulse Pearson over 60s event bins`; correlation-not-causation stated on the wire).
  `test_all_four_error_classes_return_distinctly` produces all four classes from real code
  paths and asserts they are structurally distinguishable, not just differently worded.
  New shared `tools/policy.py`: any `chaos*` service returns a `permission` error
  (`CHAOS_SCOPE_REQUIRED`) from **both** servers - the injector's signals are the Day 19
  eval's ground truth, and the class is permission (signals exist, scope missing), not a
  disprovable "no such service".
- **Verified live** by `scripts/check_day7_delegation.py` (2 API calls): the coordinator selected
  `[incident]` and skipped three with reasons, 103 words of context reached the agent
  byte-for-byte, a coordinator-only sentinel planted in the situation block **did not leak**
  into the agent's prompt, cost accumulated to 12,209 in / 4,149 out across both calls, and
  status came back `partial` with 4 resolvable gaps rather than a falsely clean `complete`.
- **The first live run failed, usefully.** `round` was in the model-facing schema and Sonnet
  omitted it, failing the whole plan - something 186 green offline tests could not catch,
  because every fixture was hand-written with the field present. Fixed by asking the model for
  `PlannedInvocation` (no `round`) and stamping the value in the coordinator, matching the
  Day 4 precedent where `IncidentReport` excludes the caller's plumbing. War story #7.
- **Carried to Day 14:** model-written synthesis, alongside the refinement loop that needs it.

### Day 8 — Docs agent and retrieval ✅
- **A:** Docs agent — prompt constrained to retrieved documents only, cites every claim.
- **B:** pgvector ingestion pipeline — chunking, embeddings, metadata, hybrid search.
- **Done when:** Docs agent answers from the seeded corpus with citations.
- **Done:** `DocsAgent.answer` retrieves first (injectable `CorpusRetriever` seam), renders the
  documents into the prompt, and enforces grounding in code - an uncited-in-retrieval
  `document_id` or a paraphrased quote raises `DocsAgentError`; `Coverage` counters and the
  `search_corpus` `ToolCallRef` are stamped by the runtime, never asked of the model.
  Registered in the executor's `default_runners()` (pinned by test).
- **Done (B):** `aioc.retrieval`: sha256-idempotent ingestion into `incident_embeddings`
  (`04-embeddings.sql`, additive), pg_trgm + pgvector hybrid search fused with RRF, Voyage
  behind the `Embedder` protocol, honest lexical-only `degraded` mode without a key.
  Vectors ingested 2026-08-21 (18/18, `voyage-3.5`); hybrid search verified live.
- **Carried:** the Day 8 done-when was proven offline plus real-corpus integration tests;
  the 1-call live proof is `scripts/check_day8_docs.py`, still unspent.

### Day 9 — Parallelism and tracing ✅
- **A:** **Parallel Task calls** — Incident + Docs invoked in a single response.
- **B:** Langfuse instrumentation — traces for every agent call, tool call, token count, cost.
- **Done when:** One trace shows two agents running concurrently.
- **Done:** the executor's parallel group now runs on a thread pool (the agents block on the
  Anthropic SDK, so threads buy real overlap without an async rewrite); the sequential chain
  stays ordered, results merge in plan order, and every runner gets its **own `Usage`
  accumulator folded in after the join** - the HANDOFF's named race (`+=` on a shared
  accumulator losing token counts silently) is closed by construction, not by a lock.
  Concurrency is proven offline by a barrier test: each fake runner blocks until the *other*
  arrives, so two clean responses are proof of overlap.
- **Done (B):** `aioc.observability.tracing` - a `Tracer`/`RequestTrace`/`AgentSpan` seam with
  a `NullTracer` null object and a `LangfuseTracer` adapter (SDK client built lazily, injectable
  stub in tests). One trace per request: a `plan` span with the planning call's own tokens, one
  `agent:<name>` span per invocation opened/closed in the worker thread that ran it (real wall
  clock, visible overlap), `ToolCallRef` records as child events carrying their measured timing,
  and `CoordinatorResponse.trace_id` filled from the trace. Tracing is **opt-in at the entry
  point** (default `NullTracer`) so the offline suite stays network-free even with keys in `.env`.
- **Checkpoint produced (2026-08-22):** with keys in `.env` (US-region host - the EU default
  401s, now a HANDOFF §4 trap), `scripts/check_day9_trace.py --fake-agents` passed for zero
  Claude calls: two agent spans overlapping 1500 ms in trace `a15143c60aa6fd3c8b97c18ad2eb97dc`.
  The first attempt failed usefully - wrong-region 401 plus a `trace_url` crash after the real
  work finished; both fixed with regression tests, and the script now auth-checks up front
  instead of losing spans silently on the background export thread.
- **Carried:** the ~3-call no-flag form (a real planned request, traced) is unspent; Day 10's
  demo exercises the same path.

### Day 10 — Integration: first real demo ✅
- **Both tracks:** End-to-end query: *"Why did latency spike after the last deploy?"*
- **Checkpoint:** Record a GIF. This is your first LinkedIn asset — capture it now.
- **Done:** `scripts/demo_day10.py` runs the whole story live - inject `downstream_latency`,
  render live Prometheus metrics as the situation block, then `respond()` end to end with
  tracing. Two runs, both diagnosing the injected truth (measured numbers in
  `docs/interview-prep/numbers.md`): the canonical query ran **Incident alone** (2 calls,
  40.1s) because dynamic selection correctly judged it needs no docs lookup - the demo
  author's "expect Incident + Docs" prediction losing to the coordinator's judgment is the
  graded behaviour working; the showcase query (canonical + "how have we resolved similar
  incidents before?") ran **Incident + Docs in parallel** (3 calls, 41.2s - two agents for
  the wall-clock price of one), intent `mixed` @ 0.85, `downstream_latency` @ 0.72, 7
  claims cited across 4 corpus documents, traced to Langfuse.
- **Checkpoint captured:** `docs/assets/day10-demo.gif`, rendered from the run's recorded
  transcript by `scripts/render_demo_gif.py` - the real lines and order, with only the
  dead time compressed. Run 2 doubled as the pending Day 8 live proof (Docs agent
  grounded, live) and the Day 9 real-request trace.

---

# Sprint 3 — Agents 3 & 4 + tool depth (Days 11–15)

*Goal: all four agents live, plus the routing case study.*

### Day 11 — GitHub agent ✅
- **A:** GitHub agent — read repos, analyze PRs, explain diffs.
- **B:** GitHub MCP server wired in, scoped credentials, repo access verified.
- **Done (2026-08-23):** `src/aioc/tools/github/` is a custom stdio MCP server (`get_pull_request`,
  `list_commits`, `diff_refs`) over the GitHub REST API - four-class errors, `GITHUB_SCOPE_MISSING`
  for a missing/under-scoped token, keys-only patch redaction, bounded output with honest
  `meta.truncated`. `aioc.llm.mcp.McpStdioToolset` is the client seam; the GitHub agent is the
  first agent to consume an AIOC tool over the real wire. Facts are stamped from tool replies,
  ungrounded PRs/commits/excerpts are rejected in code. Registered in `default_runners()`.
- **Verified:** token is read-only (`pull_requests=read; contents=read`) and reads this repo's
  PRs, files, commits, and compares; the server answered PR #12 over stdio with zero Claude calls.
- **Checkpoint produced (2026-08-23):** after rotating the Anthropic key, `scripts/check_day11_github.py`
  passed on PR #12 - one wire call, two Claude calls, 27.4 s, risk low @ 0.85, every excerpt grounded.
  Seven attempts to get there, each failure a harness or prompt defect now pinned by a test (the
  table is in `docs/interview-prep/numbers.md`).

### Day 12 — Deployment agent
- **A:** Deployment agent — compare releases, check rollout health.
- **B:** `diff_release` and `check_rollout_health` custom tools.
- **Done (2026-09-09):** `src/aioc/tools/deployment/` is the `aioc-deployment` stdio server for the two
  contract-named tools. `diff_release` reads compose, `.env`-style, and Kubernetes manifests at both
  refs and diffs them structurally with every value hashed as it is parsed (keys, images, manifest
  paths, commits; values cannot leave the parser). `check_rollout_health` reads Prometheus - the demo
  app now exports `service_build_info` - and applies a deterministic status rule with every unmeasured
  signal `null`. Both carry the sec 7.3/7.4 error codes from real paths. `src/aioc/agents/deployment.py`
  drives them over the wire; keys, images, and health signals are stamped from the replies, a tool not
  run needs a gap against its fields, excerpts are grounded against replies and context, and
  `requires_approval` is stamped true. The `Toolset` seam and ledger moved to `agents/_toolset.py`.
  Registered in `default_runners()`: all four agents exist.
- **Verified:** the server answered `check_rollout_health` over stdio against the live stack with zero
  Claude calls; 99 new offline tests (73 tool, 24 agent, 2 wire).
- **Checkpoint produced (2026-09-09):** `scripts/check_day12_deployment.py --deploy` **passed on the
  first attempt** - two wire calls (`diff_release` 3.6 s, `check_rollout_health` 0.25 s), four Claude
  calls, 51.9 s, seven config keys and one image change stamped, health stamped, rollout `degraded`
  @ 0.90 (the redeploy's own restart and failed scrape, correctly attributed), `hold_and_monitor`
  @ 0.60, eight grounded excerpts, one honest gap. Numbers in `docs/interview-prep/numbers.md`.

### Day 13 — Sequential paths + routing experiment (part 1)
- **A:** **Sequential dependency path**: GitHub reads the PR → Deployment diffs the release.
  Note in code comments why this one *can't* be parallel.
- **B:** Build two deliberately overlapping tools (e.g. `analyze_logs` vs `analyze_events`).
  Run 20 queries, **record the misrouting rate**.
- **Done (2026-09-13):** the sequential chain is a real handoff. `src/aioc/coordinator/handoff.py`
  digests a dependency's response (facts, judgements with confidence, gaps, evidence refs; lists capped,
  a hard ceiling, plain text so the dependent can quote it); the executor composes each dependent's
  context as the planner's block plus the digest of each direct dependency at the moment they return,
  records the composed block verbatim in that invocation's `context_passed`, and says in a code comment
  why the chain cannot be parallel (the dependent's context does not exist until the dependency
  returns). `src/aioc/tools/incident/analyze_server.py` is the `aioc-analyze` stdio server carrying
  `analyze_logs` (over `docker compose logs`, the only log source the stack has) and `analyze_events`
  (over the seeded timeline), both with the contract's deliberately weak v1.0.0 part 4 verbatim and
  pinned by a test. `scripts/check_tool_routing.py` is the measurement: the tools listed over the wire,
  forced single-tool choice, ground truth by data kind, query sets hashed into every record.
  Two side fixes from the live run: a failed invocation's gap now keeps the whole error text (a first-line
  cut had reduced a pydantic error to "1 validation error"), and every agent settles `complete` over a
  null judgement to `partial` in the runtime (`agents/_status.py`) instead of being refused for a
  field the code could derive.
- **Verified:** 514 offline tests (92 new: 5 executor handoff, 14 digest, 69 analyze tools + reader +
  patterns, 2 wire, 1 status settling, 1 gap text); both analyze tools answered from the live stack and
  the seeded corpus with zero Claude calls.
- **Checkpoint produced (2026-09-13):** `scripts/check_day13_sequential.py --deploy` **passed on the second
  attempt**. The first attempt failed for a correct reason: the situation block handed the coordinator
  both release SHAs, so it planned GitHub and Deployment in *parallel* - dynamic selection working, the
  scenario wrong. With the release identity reachable only through the PR, the coordinator planned
  `sequential` with a `depends_on` edge on its own; GitHub ran 0.0-56.8 s, Deployment 56.8-134.8 s;
  Deployment's recorded context was 3,706 chars (642 planner + the digest), it diffed the merge commit
  it read from the digest, and reported three added config keys, no image change, rollout `degraded`
  @ 0.85 from the redeploy's own restart. 8 Claude calls, 263.8k input tokens (PR #15's patch re-sent
  each round - the Day 21 target), 143.8 s. **Routing baseline: 0/20 misrouted on the plain set and
  0/20 on a second, adversarially worded set** (Sonnet, v1 descriptions) - with parts 1 and 3 of the
  template stating what each tool reads, the weak part 4 cost nothing on 40 queries. Recorded as the
  honest "before"; the Day 14 write-up owns what that means. Numbers in
  `docs/interview-prep/numbers.md`.

### Day 14 — Refinement loop + routing experiment (part 2)
- **A:** Coordinator **refinement loop** — detect gaps in synthesis, re-delegate targeted queries.
- **B:** Split/rename the overlapping tools, re-run the same 20 queries, record the new rate.
  Draft `docs/case-study-tool-routing.md` with before/after numbers.
- **Done (2026-09-17):** the executor runs the **refinement loop** after the plan: every open `Gap` that
  is `resolvable` and names a `suggested_agent` is re-delegated as a new `round: 1+` invocation, its
  query the gaps' `suggested_query` verbatim, its context the planner's block plus a refinement block
  naming the gaps plus the digest of each response that raised one (the Day 13 composition), one
  invocation per agent per round, rounds capped (default 2), an identical gap never asked twice,
  `resolvable: false` and unregistered agents never retried; a re-delegation that answers closes its
  gaps, `refinement_rounds` counts the rounds. **Synthesis is a seam** (`coordinator/synthesis.py`):
  deterministic by default, `ModelSynthesiser` opt-in at the entry point over the agents' digests
  through a flat forced-output tool, grounded in code (an evidence id no agent carries, or a confident
  uncited answer, rejects it) with a deterministic fallback recorded in `answer.reasoning`. The
  **`1.1.0` split** landed under the §0 pre-authorization, rationale written first in
  `docs/design-notes/contract-changes.md`: `analyze_logs` / `analyze_events` → `search_container_logs` /
  `search_recorded_events` (`tools/incident/search_server.py`, `aioc-search`), parts 1-3 byte-identical
  to v1, only the name and part 4 changed, v1 definitions struck through in CONTRACTS.md, v1 server kept
  runnable, `schema_version` `1.1.0`. `check_tool_routing.py --variant v1_1` re-ran the same forty
  queries; the write-up is `docs/case-study-tool-routing.md`.
- **Verified:** 565 offline tests (51 new: 18 refinement, 15 synthesis, 15 search server, 2 wire,
  1 retry span); lint and mypy clean; the flattened synthesiser grounded a live answer over a recorded
  run in one call (six real evidence ids, 0.55).
- **Checkpoint produced (2026-09-17):** `scripts/check_day13_sequential.py --deploy` with the loop and the
  model synthesis on **passed on the third attempt**. Attempt 1 exercised the loop end to end (a failed
  GitHub retried and answering; its gap re-delegated to Deployment with GitHub's digest) and failed the
  check because the demo was deployed at the PR head while GitHub correctly named the merge commit as
  the release, and the synthesis fell back on a nested argument the model wrote as XML text. Attempt 2
  failed on the fix itself (a `merge_commit_sha` field in the PR reply made the model report an
  unfetched commit). Attempt 3: GitHub `partial` @ 0.75 then Deployment `complete` @ 0.62 with the
  digest in context; two refinement rounds re-delegated Deployment's null-baseline gap and both were
  refused by the agent's own rules; the model synthesis grounded a 0.65 answer on nine evidence ids and
  named the open gap. 344.0k input tokens, 191.9 s. The routing re-run: **0/20 plain, 0/20 hard** with
  the `1.1.0` tools (125.9k input tokens). Numbers in `docs/interview-prep/numbers.md`, the case study
  in `docs/case-study-tool-routing.md`, the story in war story #10.

### Day 15 — Integration: four agents live
- **Checkpoint:** A multi-agent query exercising parallel *and* sequential paths.
- **Both tracks:** Cost review — check Console spend against the $100 alert.
- **Done (2026-09-18):** `scripts/check_day15_integration.py` is the checkpoint: a real fault injected,
  the situation block built from live Prometheus metrics plus what the on-call knows about releases, and
  one query that needs all four agents. It asserts all four are planned and answer, that a parallel
  group of two or more really overlapped in wall-clock time, and everything the Day 13 check asserts
  about the sequential half. The PR under test is small on purpose (#11, six files; the previous
  release is main before the merge), which took the PR read from ~22k tokens to ~1.1k and the release
  diff from ~7.4k to ~0.4k. `scripts/cost_review.py` (free) is the cost review: it prices every recorded
  live run by check and by day and says what it cannot see.
- **Three defects the first four-agent run found, all fixed:** the planner's roster said the Incident
  agent "reads the historical incident corpus" (it has no tools; the corpus is the Docs agent's), so
  the plan skipped Docs with a reason quoting that sentence; the digest's ceiling cut the evidence list
  first and the Docs claim lines showed document ids in the slot every other line uses for evidence
  ids, so the model synthesis cited `doc_` ids and the grounding check correctly refused it; and the
  Day 13 evaluator compared a round-1 re-delegation with round 2's context. The roster is corrected
  and pinned by a test, the evidence list now survives the ceiling (the body gives up the room),
  claim lines say `docs=`, and the evaluator reads the planner's block from round 0.
- **Verified:** 581 offline tests (16 new); lint and mypy clean; the synthesis fix confirmed in one call
  over the recorded failing run (a grounded 0.62 answer citing 25 real evidence ids) before the re-run.
- **Checkpoint produced (2026-09-18):** **PASS on the second attempt.** Incident, Docs, and GitHub ran
  concurrently (0.0 -> 22.9 s, 33.5 s, 78.5 s), Deployment ran `sequential` after GitHub with its digest
  (78.5 -> 146.8 s), and the model-written synthesis answered at 0.60 on 13 evidence ids with the right
  conclusion - the release is not the cause; payments-api latency is propagating through the fan-out;
  two precedents. 342.9k input / 47.5k output tokens, 279.0 s. `docs/assets/day15-demo.gif` replays it.
- **Cost review:** measured spend across every recorded live run is **$7.14** (2.58M input / 198k
  output tokens), 7% of the $100 alert, a floor rather than the bill (16 early runs recorded no usage).
  The finding that matters is not caching: in the passing run the plan had answered everything by
  146.8 s, and refinement round 1 then re-delegated all four agents off gaps that said "this part of
  the question is not mine" - roughly half the bill, and three of the four re-delegations were refused
  by the agents' own rules. Prompt caching stays on Day 19 as planned; the out-of-scope gaps are
  HANDOFF §7 item 22. Numbers in `docs/interview-prep/numbers.md`, the story in war story #11.

---

# Sprint 4 — Hardening + reliability (Days 16–20)

*Goal: every output schema-validated; evals running.*

### Day 16 — Schemas everywhere + HITL
- **A:** All four agents on validated schemas — **nullable fields** (absent data returns
  `null`, never a fabricated value), enums using the `"other"` + detail-string pattern.
- **B:** Human-in-the-loop approval gate for critical actions (rollback, restart, merge).
- **Done (2026-09-25):** A: the `contract-audit` skill ran against all four agents' schemas and the
  contract models, and every finding the contract already requires is now enforced - `schema_version`
  read off every agent and coordinator payload with a major mismatch refused (sec 0), gap-to-field
  matching on field boundaries rather than a string prefix, unsupported Docs claims kept out of the
  answer and one gap per unanswered sub-question (sec 4.2), and the `suggested_agent` guidance that
  contradicted its validator fixed in all four emit schemas (pinned identical across the four by
  `tests/test_schemas_everywhere.py`). No shape moved and `schema_version` stays `1.1.0`; the six audit
  findings that *would* be contract changes are listed in `docs/design-notes/contract-changes.md`.
  HANDOFF item 22 landed first: every planned invocation's context carries a roster of its siblings
  and why each was selected, so an agent no longer raises gaps for another agent's part. B:
  `src/aioc/hitl/` - `policy.py` makes the sec 4.1 approval rule executable (a production-write
  classifier ORed with the agent's flag and the risk rule; the Incident runtime stamps the flag
  upward), and `gate.py` turns every recommendation in each agent's latest report into an
  `ApprovalRequest` and an `ApprovalDecision` record, fail-closed (`DenyAll` by default; a broken or
  anonymous approver denies). `scripts/gate_recorded_run.py` (free) replays it over the recorded live
  responses: 15 recommendations, 7 needing a human, and three classifier misreadings found and pinned.
  The live four-agent re-run that measures item 22 is not yet made.

### Day 17 — Retry loop + audit
- **A:** **Validation-retry loop** — on schema failure, re-request with the specific error
  attached. Track retry-resolvable (format) vs. not (info genuinely absent).
- **B:** Audit log for every approved/denied action.
- **Done (2026-09-28):** A: `src/aioc/agents/_retry.py` - every agent's forced emit runs inside
  one loop: a refused report (pydantic's `ValidationError`, or the agent's own grounding error) is
  answered in the same conversation with a `tool_result` carrying `is_error: true` and the rendered
  error, and the emit tool is forced again; no tool is re-run. The two kinds are told apart in the
  feedback and the record - `format` (the model has the information and mis-shaped it) and
  `grounding` (it cited what it was not given; an honest gap is the answer) - and every emit is
  recorded in a `RetryLog` with attempts, rejections, and whether the retry recovered, so
  retry-resolvable is measured per run. Two stopping rules: the cap (`AIOC_MAX_VALIDATION_RETRIES`,
  default 2) and an identical rejection; truncation and errors that are not the model's doing are
  never retried; when the loop gives up the last error is raised unchanged with a note the
  executor's gap keeps. B: `src/aioc/hitl/audit.py` + `docker/postgres/init/05-hitl-audit.sql` -
  the gate writes every decision to an `AuditLog` before returning it (`not_required` included), a
  decision the log refused comes back as a denial that says so, and the Postgres table is
  append-only at the database (triggers refuse UPDATE, DELETE, and TRUNCATE; verified live).
  `respond(gate=...)` is the opt-in entry point; `scripts/gate_recorded_run.py --persist` wrote the
  15 recorded decisions and `scripts/audit_log.py` reads them back. 707 offline tests.

### Day 18 — Confidence + provenance shore-up
- **A:** Field-level confidence scores on all agent outputs.
- **B:** Docs agent **claim → source mapping** and **coverage-gap reporting**
  (the cheap Domain 5 shore-up from `BUILD_PLAN.md`).
- **Done (2026-09-28):** A: `src/aioc/coordinator/confidence.py` - the contract already made
  confidence field-level (every analytic field is an `Assessment`; the floor and the cited-at-0.5
  rule are validated), so Day 18 is the reading: every judgement in a response (every `Assessment`
  via `walk_assessments`, plus the Docs agent's claims) with its path, band, and evidence, one
  `ResponseProfile` per report and a `RequestProfile` per request, and the band table read
  literally as flags - a 0.90+ field citing fewer than two sources, an overall above every field,
  and (found by the first run over recorded output) an unsupported claim above the speculation
  floor. Every digest now carries a one-line confidence profile after its summary. B:
  `src/aioc/coordinator/provenance.py` - each claim's `SourceRef` joined to the response's
  document evidence and the retrieval call behind it (chunk match first, document otherwise),
  every unanswered sub-question paired with the gap that reports it (indexed `blocks_field`
  first, then in order, `None` rather than a guess), and the Docs digest pairing them.
  `scripts/confidence_report.py` (free) over the 9 recorded responses: 105 judgements, no field
  over-claims its band, 5 unsupported claims above the floor (two at 0.90), 6/11 sub-questions
  answered with every unanswered one gapped, 23/23 supported claims traced to an evidence entry.
  The Docs emit guidance now tells the model an unsupported claim's confidence belongs below 0.25.

### Day 19 — Evals + cost levers
- **A:** Eval set of 15–20 cases from the seeded incidents + a scoring harness
  (accuracy, hallucination rate, tool success rate).
- **B:** Prompt caching on shared system prompts; run the eval suite through the Batch API.
- **Done (2026-09-29), offline; the live run is blocked on a revoked API key.** A:
  `src/aioc/evals/` and `evaluations/cases/seeded-incidents.json` - 20 cases, 38 items: every
  seeded incident as a diagnosis (Incident agent, from the signals an on-call engineer would have
  had) and as a recall (Docs agent, of that incident's own post-mortem), plus two no-precedent
  probes. A case selects lines of the seed and never authors them, the expected values are read
  from the seed SQL at load, and a leak guard refuses a context that carries its own answer.
  Scoring is pure: accuracy (failure mode and severity against the `true_*` columns, services as
  precision and recall, abstentions counted apart from wrong answers), hallucination rate (every
  checkable statement in a diagnosis looked for in its context; an invented precedent on a probe;
  the agents' own grounding refusals), tool success rate, and calibration per contract band. B:
  prompt caching is request shaping in `LLMClient` (`AIOC_PROMPT_CACHING`, on by default) - the
  marker on the system block, which caches the tool schemas with it, and on the tool loop's tail -
  and `Usage` carries the cache counters into `CoordinatorResponse.cost`. The Batch API is
  `src/aioc/llm/batch.py`: `DeferredClient` runs the unmodified agents through a batch by
  replaying them until every call is answered, so a refused report's retry is simply a second
  batch. `scripts/run_evals.py` runs either mode, prices the run three ways from its measured
  tokens, and re-scores a recorded run from disk for free; `scripts/cost_review.py` prices cache
  reads, cache writes, and batches from the record. 918 offline tests. **No live score exists:**
  the first live run was refused with `401 API key is invalid` before spending anything, which
  is also how the harness learned to abort on a refused credential rather than score it.

### Day 20 — Integration: baseline
- **Checkpoint:** Full eval run, results committed to `evaluations/baseline.md`.
  Record cached vs. uncached and batch vs. realtime cost deltas — these are portfolio numbers.
- **Not done (2026-09-29): the checkpoint is built and tested, the run has not been made.** The
  API key is revoked, so no eval has ever run live and `evaluations/baseline.md` does not exist.
  What exists is the checkpoint as one command, `scripts/check_day20_baseline.py`: a four-call
  smoke test that stops the run if the wire or the cache is not working, then the whole set once
  each realtime and uncached (the reference), realtime and cached, and batch and cached, then the
  baseline written from what was measured - 118 calls, ~$2.95 projected before caching.
  `src/aioc/evals/baseline.py` puts the runs side by side, refuses runs that are not comparable,
  keeps the lever's delta (a run's own tokens priced both ways) apart from the measured one (one
  run's bill against another's), and names every item the runs scored differently; `compare`
  reads a later run against it, which is Day 24's token reduction. Every report carries a cache
  verdict. 970 offline tests. **The done-when is the committed baseline, and it is still open.**
- **First live run (2026-09-29), stopped by an empty account.** With the key rotated the smoke
  test passed - the cache was measured reading a 7,272-token prefix, four diagnoses 21% cheaper
  than their own tokens uncached - and the first full run scored five items before every call
  returned `credit balance is too low`. Nine items are measured and kept; about $0.65 was spent.
  The run changed the harness: a failure that is the environment's stops the run instead of
  being scored (at once for the key or the account, at the third in a row otherwise); every item
  is written to `progress.jsonl` as it is scored and `--resume` continues a stopped run without
  paying twice or counting twice (`src/aioc/evals/store.py`); an excerpt joined from verbatim
  lines is `stitched`, not invented; and the cost projection was re-calibrated from measured
  requests. 1013 offline tests.
- **Done (2026-09-30).** `evaluations/baseline.md` and `baseline.json`: the 38 items on
  `claude-sonnet-5` realtime uncached, realtime cached, and batch cached. Failure mode correct
  15, 16, and 17 of 18; recall cites its own post-mortem 18/18 in every run; 1-2 ungrounded
  statements in about 270; 35 of 38 items scored the same in all three, so the levers change
  the bill and not the answers. On the same tokens: cached realtime -29%, cached batch -61%,
  with 70-74% of input read from the cache in both. Made in two sittings across an empty
  account and a poll that died while its batch ran on; the run was continued each time without
  paying for an answer twice, and a batch submitted by a dead process was read back
  (`DeferredClient.attach`). The three batches took 2 h 40 min, 1 h 42 min, and 45 min of
  queue. $3.72 recorded; project spend $10.85, 11% of the alert. 1027 offline tests.

---

# Sprint 5 — Context engineering + CI + deploy (Days 21–25)

*Goal: lean context, Claude Code in CI, live URL.*

### Day 21 — Trimming + PR review
- **A:** Trim verbose tool outputs; structured fact extraction *before* content enters context.
- **B:** Claude Code in GitHub Actions — automated PR review on this repo.
- **Done (2026-09-30), the review workflow written and not yet run.** First, HANDOFF item 22's
  live re-run with caching on: `check_day15_integration.py` **PASS, $0.74** (Day 15: ~$1.16),
  one refinement re-delegation instead of four, 62% of input read from the cache, the retry
  loop's first live recovery. It found two harness defects, both reproduced offline and fixed:
  a tool-loop round cut off at `max_tokens` left a `tool_use` unanswered and the API refused
  the next request (now answered and never run; every fake client enforces the rule,
  `tests/wire.py`), and the model synthesis was the one forced emit outside the retry loop
  (now inside it). A: the tool servers extract facts before content enters the context - a
  commit is its subject line, `message_truncated`, and a merge commit's PR title; patches
  share a budget a reply spent on config and code first, with `patch_paths` for what it cut;
  `diff_release` commits are subject-only with no shape change (`contract-changes.md`).
  -34% on the run's five replies (`scripts/measure_tool_replies.py`). B: `ci.yml` (lint,
  mypy, the offline suite, no secrets) and `claude-review.yml` (`claude-code-action@v1`, a
  prompt naming this repository's defect classes, read-only tools plus inline comments). The
  review needs the `ANTHROPIC_API_KEY` secret and the Claude GitHub App (HANDOFF item 45).

### Day 22 — Ordering + false positives
- **A:** Position-aware input ordering — freshest signals and the query where attention is strongest.
- **B:** Tune the review prompt for **low false-positive** feedback; add test generation.

### Day 23 — Handoffs + model routing
- **A:** Structured digests across handoffs — Incident passes a digest to Deployment, not a raw dump.
- **B:** Model routing experiment — Haiku/Sonnet for subagents, Opus for the coordinator.
  Measure cost and latency per configuration.

### Day 24 — Measure + deploy
- **A:** Re-run evals. Record token reduction vs. the Day 20 baseline.
- **B:** Deploy to Railway/Render. Keep K8s manifests in `infrastructure/` as documented artifacts.
- **B — schema and corpus on hosted Postgres.** `docker/postgres/init/` runs *only* on
  first initialisation of an empty local volume, so the hosted database starts with no
  extensions, no tables, and no incident corpus. Apply them explicitly, in order, before
  the app points at it:
  ```bash
  for f in docker/postgres/init/*.sql; do psql "$DATABASE_URL" -f "$f"; done
  ```
  The seed is idempotent (`ON CONFLICT DO NOTHING`), so re-running is safe; the schema
  files are not, and will fail loudly if the tables already exist. Neon and Supabase both
  need `vector` enabled on the instance — `01-extensions.sql` raises a clear exception if
  it is missing rather than failing later inside retrieval.
  **This is also the point to decide on a migration tool.** `init/` cannot express a change
  to an already-populated database, so any post-deploy schema edit needs either a
  destructive reseed or a real migration path. See `docs/guides/incidents-table.md` for the
  tradeoff.

### Day 25 — Integration: production smoke test
- **Checkpoint:** Live URL, chaos injected against the deployed stack, traces landing in Langfuse.

---

# Sprint 6 — Evidence + launch (Days 26–30)

*Goal: a reviewer can verify all five domains in ten minutes.*

### Day 26 — Evidence packaging
- **A:** README section per CCA-F domain: *domain → implementing module → design note.*
- **B:** Architecture diagrams, Langfuse screenshots, eval result tables.

### Day 27 — Design notes
- **A:** `docs/design-notes/` — the workflow-vs-agent tradeoff, why parallel here and
  sequential there, what the retry-loop error taxonomy revealed.
- **B:** Polish the tool-routing case study. Secrets audit — scan history for leaked keys.

### Day 28 — Integration: demo video
- **Both tracks:** Script and record 2–3 minutes. Structure: break production (0:00–0:30) →
  agents diagnose (0:30–1:30) → trace and cost view (1:30–2:15) → architecture (2:15–end).
  Lead with the break, never the diagram.

### Day 29 — Integration: polish
- **Both tracks:** Edit video, final README pass, fresh-clone test (`git clone` → running in
  under 10 minutes on a machine that isn't yours). With one maintainer this test is the
  only thing standing between `HANDOFF.md` and a repo nobody else can start.

### Day 30 — Launch
- LinkedIn post in your own words. Repo public.
  (Originally two cross-linked posts, one per engineer. One post now - do not manufacture
  a second voice for a project with one author.)
- Hold the tool-routing case study back as a **second post 3–5 days later**; it's your
  strongest standalone content and shouldn't be buried in a launch announcement.

---

## Risk register

| Risk | Signal | Mitigation |
|---|---|---|
| Schema churn | Integration breaks repeatedly | Contracts frozen Day 1; changes need both engineers to agree |
| Scope creep to 6 agents | "It'd be easy to add Security…" | Four is the plan. Extra agents are a post-launch v2 |
| Demo env under-built | Agents have nothing real to read | Sprint 1 protects it; do not defer Days 3–5 |
| Infra rabbit hole | Days lost to K8s/ArgoCD | Railway for the live demo; manifests stay documentation |
| Cost surprise | Console spend climbing | $100 alert Day 0; Day 15 and Day 20 reviews |
| ~~Uneven contribution~~ | ~~One engineer's name on 90% of commits~~ | Retired after Day 6 - one maintainer owns everything, so contribution balance is no longer a risk. The three rows below replace it. |
| Layer boundary erodes | A tool server imports `aioc.contracts`; an agent reaches into a tool's internals instead of the wire | The boundary used to be enforced by two people not sharing a head. Now it is enforced by tests: `tests/test_timeline_tool.py` asserts the longhand enum copies still match, and no `tools/` module may import `aioc.contracts` |
| Contract changes unrecorded | `schema_version` still `1.0.0` after a frozen shape moved; a §9 row with no design note | The CONTRACTS.md §0 process, with the written rationale required *before* the code. `/contract` restates it on demand |
| Single point of failure | Nobody else can run the stack, and the traps live only in one head | `HANDOFF.md` is the mitigation and must stay current. The Day 29 fresh-clone test is what proves it |

---

## Definition of done (the whole project)

- [ ] Four agents live, orchestrated with dynamic selection, parallel + sequential paths, refinement loop
- [ ] Custom MCP tools with structured errors, plus a routing case study with before/after numbers
- [ ] Every agent output schema-validated with nullable fields, enums, confidence, retry loop
- [ ] Claude Code config hierarchy + CI review job running on real PRs
- [ ] Context engineering with a measured token reduction against baseline
- [ ] Eval suite with committed results
- [ ] Live URL, public repo, README mapping each CCA-F domain to its implementing module
- [ ] 2–3 minute demo video
- [ ] Fresh clone to running in under 10 minutes

---

## Appendix — Services deliberately excluded, and why

The original AIOC architecture named a much larger set of integrations. Most are absent
from this plan by decision, not oversight. Documented here so the scope reads as
engineering judgment rather than gaps.

### Covered by a different route

| Service | How it's handled |
|---|---|
| **Vector database** (Pinecone / Weaviate) | **pgvector** on Neon/Supabase. One service, one connection string, no separate SDK. Vector-store selection and hosting are not graded on the CCA-F blueprint, and pgvector handles a 20-document corpus comfortably. Swapping to Pinecone later is ~half a day. |
| **Prometheus** | Self-hosted container. Required by the plan (Sprint 1, Day 3) — just not a signup. |
| **Grafana** | Self-hosted container. Grafana Cloud remains optional and is only for a hosted dashboard. |

### Removed by the four-agent decision

Choosing Incident + Docs + GitHub + Deployment retired the agents these belonged to:

| Service | Belonged to |
|---|---|
| **Jira**, **Slack** | Communication Agent |
| **Trivy**, **Snyk**, **AWS IAM**, **GitHub Security** | Security Agent |
| **Datadog**, **CloudWatch**, **ElasticSearch** | Incident Agent's alternative data sources — Prometheus covers this need at zero cost |

Jira's free tier covers up to 10 users and a Jira MCP server exists, so the Communication
Agent is a clean v2 bolt-on. It earns nothing on the blueprint now, because third-party
MCP consumption is already proven via the GitHub MCP server.

### Cut on purpose

| Service | Reason |
|---|---|
| **AWS** (EC2/ECS/Lambda/S3) | Billing risk, zero blueprint credit |
| **ElasticSearch** | Heavyweight for a 20-document corpus; pgvector hybrid search suffices |
| **Kafka**, **RabbitMQ**, **Celery** | Real complexity, no graded competency |
| **Managed Kubernetes**, **Helm**, **ArgoCD**, **Terraform** | Weeks of work for a point that committed manifests in `infrastructure/` make just as well. The sibling Architect exam explicitly lists infrastructure and container orchestration as out of scope. |
| **NGINX / gateway tuning** | Handled by the PaaS layer |

### Known simplification

The Docs agent was originally specified to read **Confluence and Notion**; in this plan
its corpus is seeded markdown plus Postgres. This is the one exclusion that costs a
little narrative strength — "retrieves organizational knowledge from a real wiki" reads
better than "reads files in the repo." The optional Notion workspace in the accounts
checklist closes it for about a day of work.

### Deployment note

When the stack ships (Day 24), the Prometheus/Grafana/demo-app containers need a home
too. Two acceptable options: deploy the full compose stack to Railway (multi-service
projects are supported), or keep the demo environment local and record the video against
localhost. Local is cheaper and entirely honest — the live URL demonstrates the
orchestrator; the local stack demonstrates the incident flow.

---

*Companion to `BUILD_PLAN.md`. Domain weights per the CCA-F Foundations exam guide.*
