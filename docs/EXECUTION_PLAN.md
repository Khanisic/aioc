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

### Day 7 — Delegation and error taxonomy
- **A:** Task delegation with **explicit context passing** in each subagent prompt.
  No implicit inheritance — this is the most-tested orchestration fact.
- **B:** `correlate_events` tool + structured `isError` responses across all four classes
  (transient / validation / business / permission).
- **Done when:** Subagent receives context it never inherited; all four error classes return distinctly.

### Day 8 — Docs agent and retrieval
- **A:** Docs agent — prompt constrained to retrieved documents only, cites every claim.
- **B:** pgvector ingestion pipeline — chunking, embeddings, metadata, hybrid search.
- **Done when:** Docs agent answers from the seeded corpus with citations.

### Day 9 — Parallelism and tracing
- **A:** **Parallel Task calls** — Incident + Docs invoked in a single response.
- **B:** Langfuse instrumentation — traces for every agent call, tool call, token count, cost.
- **Done when:** One trace shows two agents running concurrently.

### Day 10 — Integration: first real demo
- **Both tracks:** End-to-end query: *"Why did latency spike after the last deploy?"*
- **Checkpoint:** Record a GIF. This is your first LinkedIn asset — capture it now.

---

# Sprint 3 — Agents 3 & 4 + tool depth (Days 11–15)

*Goal: all four agents live, plus the routing case study.*

### Day 11 — GitHub agent
- **A:** GitHub agent — read repos, analyze PRs, explain diffs.
- **B:** GitHub MCP server wired in, scoped credentials, repo access verified.

### Day 12 — Deployment agent
- **A:** Deployment agent — compare releases, check rollout health.
- **B:** `diff_release` and `check_rollout_health` custom tools.

### Day 13 — Sequential paths + routing experiment (part 1)
- **A:** **Sequential dependency path**: GitHub reads the PR → Deployment diffs the release.
  Note in code comments why this one *can't* be parallel.
- **B:** Build two deliberately overlapping tools (e.g. `analyze_logs` vs `analyze_events`).
  Run 20 queries, **record the misrouting rate**.

### Day 14 — Refinement loop + routing experiment (part 2)
- **A:** Coordinator **refinement loop** — detect gaps in synthesis, re-delegate targeted queries.
- **B:** Split/rename the overlapping tools, re-run the same 20 queries, record the new rate.
  Draft `docs/case-study-tool-routing.md` with before/after numbers.

### Day 15 — Integration: four agents live
- **Checkpoint:** A multi-agent query exercising parallel *and* sequential paths.
- **Both tracks:** Cost review — check Console spend against the $100 alert.

---

# Sprint 4 — Hardening + reliability (Days 16–20)

*Goal: every output schema-validated; evals running.*

### Day 16 — Schemas everywhere + HITL
- **A:** All four agents on validated schemas — **nullable fields** (absent data returns
  `null`, never a fabricated value), enums using the `"other"` + detail-string pattern.
- **B:** Human-in-the-loop approval gate for critical actions (rollback, restart, merge).

### Day 17 — Retry loop + audit
- **A:** **Validation-retry loop** — on schema failure, re-request with the specific error
  attached. Track retry-resolvable (format) vs. not (info genuinely absent).
- **B:** Audit log for every approved/denied action.

### Day 18 — Confidence + provenance shore-up
- **A:** Field-level confidence scores on all agent outputs.
- **B:** Docs agent **claim → source mapping** and **coverage-gap reporting**
  (the cheap Domain 5 shore-up from `BUILD_PLAN.md`).

### Day 19 — Evals + cost levers
- **A:** Eval set of 15–20 cases from the seeded incidents + a scoring harness
  (accuracy, hallucination rate, tool success rate).
- **B:** Prompt caching on shared system prompts; run the eval suite through the Batch API.

### Day 20 — Integration: baseline
- **Checkpoint:** Full eval run, results committed to `evaluations/baseline.md`.
  Record cached vs. uncached and batch vs. realtime cost deltas — these are portfolio numbers.

---

# Sprint 5 — Context engineering + CI + deploy (Days 21–25)

*Goal: lean context, Claude Code in CI, live URL.*

### Day 21 — Trimming + PR review
- **A:** Trim verbose tool outputs; structured fact extraction *before* content enters context.
- **B:** Claude Code in GitHub Actions — automated PR review on this repo.

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
