# Decisions

The architectural calls, why they were made, and what I would change.
Each one is a "why did you do it that way?" answer.

---

## 1. Freeze the integration contract on day one

**Decision.** `docs/CONTRACTS.md` defines every shape crossing between the two layers and is frozen at `1.0.0`. Changing anything frozen needs written agreement, a `schema_version` bump, and a changelog row. There is exactly one pre-authorised exception, documented in advance.

**Why.** Two engineers building a reasoning layer and a platform layer in parallel have one dominant failure mode: the interface drifts, and every integration attempt breaks for a new reason. Freezing the interface is what converts "we integrate at the end and lose a week" into "we integrate continuously."

**What makes it real rather than aspirational.** The contract is *executable* - `src/aioc/contracts/` is the same shapes as Pydantic v2 models with `extra="forbid"`, and `tests/test_contract.py` validates them against the worked example read directly out of the Markdown. A prose contract nobody can run is a wish.

**The uncomfortable part, and the honest answer.** Freezing on day one means freezing while you are at your most ignorant. It has already cost something: `get_incident_timeline`'s error list names `PROMETHEUS_TIMEOUT`, written when I assumed timelines would be metric-derived, but the events actually live in Postgres. Emitting `PROMETHEUS_TIMEOUT` for a Postgres timeout would put a false value in a field the contract says is matched programmatically, so the tool emits `TIMELINE_STORE_TIMEOUT` and the deviation is flagged in the module docstring as a changelog candidate rather than done silently.

That is the tradeoff working as designed, not failing: the cost of a frozen contract is occasional friction like this, and the benefit is that friction is *visible* instead of being a silent divergence someone finds in week four.

**What I would do differently.** Freeze the *envelope* and the error taxonomy on day one - those held perfectly - and leave per-tool error code lists explicitly unfrozen until the tool exists. The generic structure was knowable up front; the specific codes were not.

---

## 2. Structured output via forced `tool_use`, not JSON mode or parsing

**Decision.** `IncidentAgent.diagnose` forces a single tool with `tool_choice={"type": "tool", "name": ...}` and reads the `ToolUseBlock.input` directly. The tool's JSON Schema is *generated* from the frozen Pydantic models.

**Why forced tool_use.** It guarantees the output matches a schema, and generating that schema from the contract models means the wire shape cannot drift from the contract. Hand-writing the schema would create two sources of truth that agree until they don't.

**Why not "return JSON" plus a parser.** Because then the schema lives in prose and the failure mode is a parse error with no field-level information. Forced tool use gives structural conformance for free and lets validation focus on the interesting rules.

**Why generate rather than hand-write.** The schema has 18 `$defs`. Maintaining that by hand alongside the models is a guaranteed drift source.

**The catch, and the interesting part.** A generated schema states *shape* but not *cross-field rules*. The contract requires `*_detail` to be non-null exactly when its partner enum is `other` - and `model_json_schema()` emits only auto-derived titles, so the wire said `kind_detail: string | null` with no hint when it applied. Measured: every model tested filled those fields regardless. The rule was in the system prompt, and the schema is where a model looks hardest.

So there is an **annotation layer** over the generated schema - a model-facing top-level description plus per-field rule text. Descriptions only, never shape, and `contracts/` is untouched, because these are prompt affordances rather than data. A guard raises at import if an annotated field disappears.

**What I would do differently.** Nothing structural, but I would write the annotation layer *first* rather than discovering the need from three failed live calls. "Generated schema plus hand-written field guidance" is the pattern; I arrived at it empirically instead of by design.

---

## 3. Enforce the graded orchestration behaviours in the schema, not the prompt

**Decision.** Dynamic selection, explicit context passing, and the parallel/sequential distinction are enforced by validators, not requested in prompt text.

- `AgentInvocation.context_passed` must be non-empty - an empty value means context was assumed inherited.
- Every agent must appear in `selected_agents` or `skipped_agents` exactly once - a missing agent is indistinguishable from one nobody considered.
- `parallel` requires empty `depends_on`; `sequential` requires non-empty, and every id must resolve inside the plan.

**Why.** A prompt that usually produces the right behaviour is not evidence of the behaviour. A validator that rejects the wrong shape is. This project exists to demonstrate that context is passed explicitly rather than inherited, so "the model is told to do it" is a weaker claim than "a response that fails to do it does not validate."

**The one I am most pleased with.** The contract catches *empty* `context_passed`. It cannot catch the subtler failure: a coordinator that satisfies the check by echoing the user's question back as context. That is inheritance with extra steps - the subagent learns nothing it would not have had. So the planner additionally rejects a `context_passed` that only restates the query. Measured output: 37-75 words of real facts per agent, not a restatement.

**Where the enforcement stops, deliberately.** Selecting all four agents is structurally *legal* - some queries genuinely need everything. Making it an error would be wrong. It is a signal to watch rather than a rule to enforce, and that distinction is itself a decision: not every quality property should be a hard constraint.

---

## 4. The MCP boundary is JSON Schema, and tool servers may not import the models

**Decision.** No tool server imports `aioc.contracts`. Enum members and input schemas are written out longhand in each server. A test asserts the longhand copies still match the Python enums.

**Why accept the duplication.** Because the alternative couples the two halves the wire format exists to separate. A tool server that imports the reasoning layer's models cannot be deployed independently, and a Pydantic-only refactor can break a tool. The duplication is the price of independence, and the drift guard is what makes it affordable - a copy that cannot silently diverge is a very different thing from a copy.

**Why a test rather than discipline.** Discipline does not survive a hurried afternoon. `test_the_server_does_not_import_the_contract_models` parses the module's *import statements* with `ast` rather than grepping the text - because the docstring explaining the rule contains the very string a substring search looks for. (I wrote the naive version first and it failed on its own explanation.)

**A related call worth mentioning.** The MCP library validates tool input against the schema by default and returns a **plain-text** error on failure. The contract requires a structured `validation` error carrying `details.field` and `details.expected`. Those are incompatible, so framework validation is off and validation is done by hand. That is a case where the library's convenient default was wrong for the contract, and noticing it required reading the library's source rather than its docs.

---

## 5. Never show the agent the answer key

**Decision.** The demo app publishes every injected chaos knob as a Prometheus gauge - which makes it the eval's ground truth - and `chaos_knob_value` is excluded from any agent's context by an enforced guard, at both the query level and the rendered-output level.

**Why enforce rather than intend.** Because the failure is *silent and it looks like success*. If that metric leaked into context, the agent would read the fault off the gauge and eval scores would jump to near-perfect. Nothing would error. The only symptom of a broken eval would be excellent results, which is the hardest kind of bug to notice and the easiest to want to believe.

**Why publish it at all.** Because the alternative - a manifest file the injector writes - can drift from what actually happened to the app. Reading ground truth out of Prometheus means the eval scores against the app's real state rather than against what a script believed it requested. Same reason `check_day5_checkpoint.py` recovers the injected mode from the gauges instead of from the injector's return value.

**Transferable shape.** Any eval with injected ground truth has this hazard. The rule I would carry forward: ground truth and model input should be *structurally* separated, with a guard on the boundary, not merely kept apart by convention.

---

## 6. Absolute timestamps in the seed corpus

**Decision.** The 18 seeded incidents use fixed literal timestamps, not `now() - interval`.

**Why.** The corpus is both the RAG corpus and the eval set. If it shifts between reseeds, two eval runs score different data - and the Day 24 token-reduction comparison against the Day 20 baseline would measure the corpus drifting rather than the context work. Deterministic beats fresh-looking.

**The cost, acknowledged.** The dataset ages. In six months the incidents look stale, and any "last 7 days" query finds nothing. That is the right trade for a fixed 30-day project with a baseline comparison in it; for a long-lived system I would generate relative timestamps from a seeded PRNG with a pinned epoch, which keeps determinism without the staleness.

---

## 7. Enum values as CHECK constraints, not Postgres ENUM types

**Decision.** The corpus schema uses `CHECK (col IN (...))` rather than `CREATE TYPE ... AS ENUM`.

**Why.** Adding an enum member is a minor version bump under the contract's change process, so it *will* happen. `ALTER TYPE ... ADD VALUE` cannot run inside a transaction on older Postgres and cannot be reverted; `ALTER TABLE ... DROP/ADD CONSTRAINT` is one reviewable statement either way. Optimise for the change you know is coming.

**Bonus.** The contract's `other`-plus-detail pairing is expressible as a `CHECK` too, so a bad seed row is rejected at insert rather than surfacing later inside a Pydantic validator where it reads as an agent bug.

---

## 8. Schema in `docker/postgres/init/`, migration tool deferred

**Decision.** Schema and seed live in `init/`. No migration tool yet.

**Why.** The volume was empty, so a destructive reset cost nothing, and a migration tool on day five is scaffolding that delays the checkpoint it is meant to support.

**What this contradicted.** `01-extensions.sql` said table creation must *not* live in that directory - the stated concern being two engineers racing in one shared file. A numbered file per concern avoids that, so the comment was revised rather than quietly violated.

**The limitation that actually matters** - and it is not the one the original comment named. `init/` runs *only* on first initialisation of an empty local volume. It never runs against hosted Postgres. So the Day 24 deployment has to apply these files by hand, every file is written to be runnable standalone, the seed is idempotent, and the Day 24 plan entry carries the commands. Adopt a real migration tool the moment a schema change must survive an already-populated database, because `init/` cannot express that at all.

---

## 9. Test-run records as structured JSON

**Decision.** Every `pytest` run and every live check writes a run summary plus per-event JSONL under `test-results/`, gitignored. Events are line-delimited; the summary is one object.

**Why JSONL for events.** They are appended one at a time and must survive the process dying mid-run. A truncated JSONL still parses line by line; a truncated JSON array is unrecoverable - and the run that crashed is the one worth reading.

**Whether it earned its keep.** Yes, immediately and more than once. The per-model failure diagnosis in `numbers.md` came out of `events.jsonl`, not scrollback: three models, distinct failures, each with the individual validation errors attached. That is what made "the `*_detail` rule is missing from the schema" visible as one pattern rather than three unrelated bugs.

**What I would add.** Token counts and cost per live call. The records have durations and outcomes; they should have spend. That is the input to every "is the cheaper model actually cheaper" question, and I had to do that arithmetic by hand.

---

## 10. Cost as a design constraint, not an afterthought

**Decision.** The test suite (186 tests as of Day 7) makes **zero** API calls. Everything model-facing is driven by scripted fake clients. Live verification lives in four separate opt-in scripts, and the model-matrix script defaults to one call per model.

**Why.** A test suite that costs money per run stops being run, and a suite that is not run is not a suite. Splitting free-and-fast from costed-and-deliberate is what keeps the fast one honest.

**What it does not buy.** Fakes prove plumbing, never model behaviour. Every fake-driven test in this repo could pass while the live agent fails - which is exactly what happened on the first live run, when three models failed a path with 14 green offline tests. The fakes were not wrong; they were answering a different question. That is why the live scripts exist and why their results are recorded rather than glanced at.

**The honest framing for an interview.** Offline tests prove I did not break the wiring. Live scripts prove the model can do the task. Conflating the two is how you get a green build and a broken product.

---

## 11. An agent that does not exist returns a Gap, never a placeholder

**Decision.** The Day 7 executor runs a plan in which the coordinator may legitimately select agents that are not built yet (Docs, GitHub, Deployment land on Days 8, 11, 12).
For such an invocation the executor records a `Gap` with `kind_detail: agent_not_implemented` and `resolvable: false`, weakens `status` to `partial` or below, and returns *no* `AgentResponse` for it.
It never stubs one.

**Why.** A plausible placeholder response is precisely the failure mode the contract's null-vs-`[]` rule exists to prevent: downstream consumers cannot distinguish "the docs agent found nothing" from "there is no docs agent".
The `resolvable: false` flag is load-bearing - the Day 14 refinement loop consumes gaps mechanically, and a resolvable-looking gap for an absent agent would burn a refinement round per request forever.
A *failed* invocation, by contrast, gets `resolvable: true` with `suggested_agent` and `suggested_query` filled in, because a retry genuinely can clear model nondeterminism or a transient upstream.
The two situations look similar and must not be encoded the same way.

**The related call: synthesis is deterministic on Day 7.** The coordinator's `answer` is adopted from the highest-confidence agent report and cites that report's own evidence ids, so the invariant "the coordinator cites its subagents and never mints evidence ids" holds by construction instead of by model compliance.
The first consumer that needs a model-written synthesis is the refinement loop (Day 14, gap detection); buying it earlier would add a token cost and a new failure mode to every request for prose nothing yet reads.

**What I would watch.** When Day 8 registers the Docs agent in `default_runners()`, the `agent_not_implemented` path stops firing for it silently - nothing forces the registration.
The Day 10 integration demo is the check that the wiring actually happened.

---

## 12. The chaos namespace is a permission boundary, at every layer it could leak

**Decision.** Any tool request naming a `chaos*` service returns a structured `permission` error (`CHAOS_SCOPE_REQUIRED`, `required_scope: eval:ground_truth`) from every MCP server, via one shared policy module (`aioc.tools.policy`).
This extends the Day 5 guard that already keeps `chaos_knob_value` out of the Incident agent's Prometheus context.

**Why permission and not business.** The signals exist - the injector publishes every fault it injects, and that is the Day 19 eval's ground truth.
Answering `UNKNOWN_SERVICE` (business) would be a lie the caller could disprove, and an empty success would read as "looked, nothing there", which is worse.
The four-class taxonomy exists so that "not for you" reaches the agent as exactly that, letting it record an `insufficient_permission` gap instead of retrying or concluding falsely.

**Why one shared module.** The Day 6 timeline server predates the gate, so Day 7 added it there too - a boundary only one tool enforces is a boundary an agent can route around by asking the other tool.
The test for this lives with the policy: both servers are asserted to return the same class and code for the same restricted name.

**Why it matters beyond hygiene.** An agent that can read the injected ground truth turns every eval score into transcription, and the failure is silent - the evals simply start passing.
Guards against silent-pass failures have to be structural, because no one investigates a green result.

---

## 13. Docs-agent grounding is enforced in code, not requested in the prompt

**Decision.** The Day 8 Docs agent retrieves before the model call, renders the retrieved documents into the prompt, and then *checks* the model's report against that set: a cited `document_id` retrieval never returned raises, and a quote or excerpt that does not appear verbatim (whitespace-normalised) in the retrieved text raises.
The prompt states the rules too, but the prompt is not what makes them true.

**Why.** "Answers from retrieved documents only" is the Docs agent's entire identity, and a hallucinated citation is its one unforgivable failure - worse than no answer, because it looks like provenance.
Day 4 already measured what happens when an invariant lives only in prompt text: every model tested violated it.
The same lesson applied here means the invariant lives where the model cannot vote on it.

**The related call: stamped accounting.** `Coverage.documents_searched/retrieved/cited` and `corpus_snapshot` are facts the runtime measured, so the emit schema does not even contain them - the model reports only the sub-question decomposition, and the runtime assembles the frozen `Coverage` itself.
Same reasoning as war story #7 (`round`): plumbing the process already knows is a field the model can only get wrong.
The retrieval call itself is recorded as a real `ToolCallRef` (`search_corpus` on `aioc-docs`, measured duration), and its id is stamped into every document evidence entry.

**What I would watch.** The verbatim check is whitespace-normalised only; a model that "fixes" a typo inside a quote will be rejected, which is correct but will look like flakiness until the Day 17 validation-retry loop re-requests with the error attached.

---

## 14. Embeddings live in a separate table, and retrieval degrades honestly

**Decision.** Vectors go in `incident_embeddings` keyed `(incident_id, model)` with the sha256 of the embedded text, never in a column on `incidents`.
The model is `voyage-3.5` at 1024 dims - the dimension is baked into the DDL, so the model choice is a schema decision and is recorded as one.
Hybrid search fuses pg_trgm and pgvector halves with Reciprocal Rank Fusion, and when the vector half is unavailable - no key, no vectors, provider down - the result says so in a `degraded` field and carries on lexical-only.

**Why a separate table.** The corpus doubles as the Day 19 eval set; re-embedding must never touch it.
The `model` column exists because vectors from different models are not comparable, and the hash makes re-ingestion idempotent - unchanged rows cost nothing, which is what makes the ingest script safe to run casually.

**Why RRF and not score mixing.** Trigram similarity and cosine similarity live on incomparable scales; ranks are the only thing they share.
The reported relevance is the better raw similarity, because an RRF sum is meaningless outside the fusion.

**Why degradation is a field and not a fallback.** A silent fallback hides a misconfigured provider forever - retrieval keeps "working" and recall quietly halves.
The degraded reason is rendered into the Docs agent's document block, so reduced coverage is visible to the model, in the response, and in the run records.

**Why Voyage at all.** Anthropic has no embeddings endpoint, so an external provider was unavoidable; the `Embedder` protocol keeps it swappable and the offline suite runs on a deterministic fake.

---

## 15. Parallelism is a thread pool with per-runner accumulators, not async and not a lock

**Decision.** The executor runs the plan's parallel group on a `ThreadPoolExecutor` and keeps the sequential chain as a plain ordered loop.
Every runner - concurrent or not - receives a fresh `Usage` accumulator, and the executor folds each one into the request total only after that runner has returned, on the coordinating thread.
Results are merged in plan order, not completion order.

**Why threads.** The agents block on the synchronous Anthropic SDK, so threads buy real wall-clock overlap for exactly the cost of a pool; an async rewrite would have touched every agent and the harness for the same observable behaviour.
The Day 7 loop was written to be replaced this way - `mode` and `depends_on` were already recorded on every invocation, so the execution strategy changed without touching the plan or the accounting.

**Why per-runner accumulators and not a lock.** `Usage` is a plain mutable dataclass and `+=` from two threads loses counts silently - corrupted `cost` with no failing test.
A lock inside `Usage` would fix that by making every single-threaded consumer (the harness loop, the agents, the planner) pay for one call site's concurrency.
Fresh accumulators summed after the join keep `Usage` a value object, make the merge point explicit and testable, and are correct by construction rather than by discipline.
The offline proof does not sleep-and-hope: a barrier test blocks each fake runner until the other arrives, so the suite fails if the group ever goes serial again.

**What I would watch.** The pool is sized to the group (at most four agents), so there is no queueing today; if agent counts ever grow, sizing becomes a real decision.

---

## 16. Tracing is a seam with a null object, and it is opt-in at the entry point

**Decision.** The executor talks to three small protocols (`Tracer`, `RequestTrace`, `AgentSpan`) in `aioc.observability.tracing`.
The default everywhere is `NullTracer`, a complete no-op whose `trace_id` is null; live entry points pass `default_tracer()`, which returns the Langfuse adapter only when both keys are set.
Spans open and close in the worker thread that runs the agent, so their timing is the agent's real wall clock and a parallel plan shows visibly overlapping spans - which is the Day 9 checkpoint artifact.

**Why opt-in rather than environment-activated.** The offline suite must make zero network calls, and it must keep that property on a machine whose `.env` carries real Langfuse keys.
A tracer that self-activates from the environment would break that silently the day the keys land - the same shape of failure as the retrieval layer silently falling back to lexical, and caught the same way: make the choice explicit and visible.

**Why a null object rather than `Optional`.** `if tracer is not None` branches would thread through exactly the code Day 9 made concurrent; the null object keeps the executor straight-line.

**Why the agents stay uninstrumented.** The executor and `respond()` already see everything worth a span - context in, summary and tokens out, and the contract `ToolCallRef` records each agent stamps.
Instrumenting inside the agents would buy timing granularity the trace does not need yet, at the cost of a tracing dependency in every agent.
The one honesty compromise: `ToolCallRef` timings are recorded after the fact, so they ride as child events whose metadata carries the measured `started_at` and `duration_ms` rather than as retro-timed spans.

**What I would watch.** The Langfuse adapter is pinned to the v4 SDK observation API by offline stub tests; an SDK major bump is a deliberate adapter change, not a transitive surprise.

---

## 17. The GitHub agent consumes a custom MCP server over the real wire, and the model never owns a fact

**Context.** Day 11 needed the GitHub agent to read repositories.
The plan said "GitHub MCP server wired in", which could mean GitHub's official server (a ~50-tool surface, Docker at runtime) or a custom one.
Until Day 11 no agent had consumed an AIOC MCP server at all - Incident reads Prometheus and Docs reads the corpus in-process - so the contract's tool envelope and error taxonomy were real for tests and for Claude Code, but not for the reasoning layer.

**Decision.** A custom stdio server, `aioc-github`, with three tools (`get_pull_request`, `list_commits`, `diff_refs`) over the GitHub REST API, and a reusable client seam (`aioc.llm.mcp.McpStdioToolset`) that launches any stdio server as a subprocess and exposes its tools as harness `ToolSpec`s.
The agent drives those in an ordinary `run_tool_loop`, then is forced through `emit_github_report`.
The official server was rejected for three reasons: its descriptions are not ours (part 4 of the template is the routing-case-study intervention), its error shape is not the contract's, and a Docker-at-runtime dependency for one agent is a demo liability.

**The schema split is the interesting part.** `PullRequestAnalysis` is mostly facts - title, state, head SHA, counts, paths - that the tool just returned.
The model is asked for a PR number plus its two judgements (`risk`, `summary`), and the runtime stamps every fact from a ledger built from the tool replies.
`CommitRef` is all facts, so the model reports SHAs only.
This is the planner's `round` lesson (war story #7) applied wholesale: plumbing the process already knows is a field the model can only get wrong.
The same ledger grounds the report - a PR, SHA, `change_ref`, or excerpt that no tool call returned raises, exactly as the Docs agent rejects an unretrieved document.

**Why a thread owns the MCP session.** The MCP client is async and its transport must be entered and exited from one task; the agents are synchronous and already run on the executor's worker threads in the parallel group.
So the session lives on one dedicated thread with its own event loop for the toolset's lifetime, and calls are submitted to it from whichever thread asks.
One subprocess per agent run, closed on exit; the test for this opens the real server with a blanked token so the wire is exercised and the network is not.

**What I would watch.** The keys-only redaction in patches is a regex over `KEY=value` / `KEY: value` lines plus known token shapes; it is tested, but it is a heuristic, and a value assigned to a lower-case key passes through.
The contract's rule is enforced one layer up by `diff_release` on Day 12 for the config surface that matters most.

**What the live run taught, the same day.** Seven attempts to the first clean pass, and not one failure was the model inventing data - the grounding check itself was wrong twice (it compared against JSON-escaped wire text, then refused a literal wire quote), the schema was silent about nesting once, and the emit tool was not on the loop's list.
The last one changed the design: the emit tool is now offered *during* the investigation with a capturing handler, so a model that is done simply says so, and the forced second call is the fallback rather than the rule - 2 calls and 27 s instead of 3 and 82 s on the same PR.
`GitHubAgentError` now carries the report it refused, which is what the Day 17 retry loop will feed back.

---

## 18. A release diff is a structural comparison of manifests with the values hashed at parse time, and a rollout's version is a metric

**Context.** Day 12 needed the two contract-named deployment tools.
`diff_release` (sec 7.3) has to answer "which configuration keys changed" without ever returning a value, and `check_rollout_health` (sec 7.4) has to say which version is running and how it behaves.
The obvious implementation of the first is to scan the GitHub patch for `+KEY=` / `-KEY=` lines; the obvious source for the second is each service's `/version` endpoint.

**Decision.** Neither.
`diff_release` fetches the release manifests (compose files, `.env`-style files, Kubernetes manifests) at *both* refs and parses them - a value is replaced by its sha256 the moment it is read, and only the digest is kept - then diffs the parsed structures.
`check_rollout_health` reads the version from a `service_build_info{service, git_sha} = 1` gauge the demo app now exports, the way real services export `*_build_info`, and reads everything else from the same Prometheus.

**Why structural.** A patch-line scan cannot tell a key whose value changed from one that was removed and re-added, reports an image that moved because a block was re-indented as a change, and has no idea which compose service a changed line belongs to.
Parsing both ends answers all three, and the test that a `yaml.safe_dump` re-indentation of the same compose file diffs to nothing is the one that would fail under the patch approach.
The cost is two GitHub reads per changed manifest (bounded at 40, flagged in `meta.truncated`) and a `pyyaml` dependency.

**Why hash on read.** "Config values are never returned" (sec 4.4) is enforced at every layer that could leak one; this is the layer where the value first exists in memory.
Hashing it there means the comparison still works ("did it change?") while the value is not held anywhere a serialiser, a log line, or a future refactor could reach.
The test is over the whole serialised payload, not a field: it asserts the fixture's secret strings appear nowhere in the response.

**Why a metric and not `/version`.** `/version` answers "what is running now"; it has no history.
The gauge in Prometheus answers "what is running now", "what ran before it" (the most recent other `git_sha` in the window, which becomes `compared_to_baseline`), and "did the version I was asked about ever run" (which separates `rolled_back` from `VERSION_NOT_DEPLOYED`) from one series, with the same retention as every other signal.
The status rule is deterministic and stated in the tool description, so an agent reading `degraded` can also read why from the signals beside it; the judgement about what to do stays with the agent, where it carries a confidence.

**What I would watch.** The status thresholds (5% error rate; any restart or failed scrape) are stated, not tuned - the Day 19 eval is where they get tested against injected truth.
And an image rebuild is now required for the gauge to exist; a stale image reports `version: null` honestly, but it is one more thing a fresh clone must do.

---

## 19. "Not looked" is a gap, never an empty list - and the second tool-driven agent shares the first one's ledger

**Context.** `DeploymentFindings.changed_config_keys` and `image_changes` are non-null lists, and `health_signals` is a block of nullable scalars.
A Deployment agent that answers a health-only question never runs `diff_release`; a diff-only question never runs `check_rollout_health`.
The contract's null-versus-`[]` rule (sec 1) says `[]` means "looked and found nothing", so an agent that did not look may not say `[]` - but the schema gives it nothing else to say.

**Decision.** The runtime stamps the lists and the signals from the tool replies for the reported releases, and refuses the report when a tool that would have filled a field was not run *unless* the report carries a gap whose `blocks_field` names that field (`findings.changed_config_keys` or `findings.image_changes` for the diff; `findings.health_signals` for health).
The gap is the honest reading of the empty list, and it is what the Day 14 refinement loop consumes to go and look.
A health reply that assessed a *different* version than the one reported does not count as having looked: signals measured on v1.4.4 presented as v1.4.3's health would be a fabrication with real numbers in it.

**The shared ledger.** The GitHub agent's `Toolset` protocol and `_Ledger` were lifted into `agents/_toolset.py` the moment a second agent needed them, as the Day 11 handoff said to.
The shared part is everything about a tool call that is not domain-specific: the honest `ToolCallRef` per wire call, the grounding text (decoded strings, raw wire text, and a `key: value` line per scalar leaf, so `"error_rate": 0.31` quoted with or without its JSON quotes is the tool's fact), and the agent's own context block as a third grounding source - the agent literally has no information beyond the context it was handed and the replies it received, so an excerpt must appear in one of them.
Each agent's subclass overrides one hook, `index`, to pick out what it stamps from (PRs and commits; diffs and health replies with their call arguments, because the health lookback is an argument, not a reply field).

**What I would watch.** The gap requirement is a rule the model has to satisfy, and rules the model has to satisfy are where live runs fail first (Day 11 took seven).
It is stated on the schema object and in the system prompt, and the first live run satisfied it without a retry - but a model that runs both tools and then reports a release neither returned will still be refused, correctly, and that is the case the Day 17 retry loop should feed back with the exact reason.


## 20. The sequential handoff is a digest composed at execution time, appended to the planner's block, and recorded verbatim

**Context.** The planner writes every invocation's `context_passed` before anything runs.
For the sequential path (GitHub reads the PR, then Deployment diffs the release) that means Deployment's context could only ever say "diff the release containing the PR GitHub reports" - the one fact it needs, the PR's commit, does not exist when the plan is written.
The contract's sec 5 promise is that `context_passed` is the literal block embedded in the subagent's prompt.

**Decision.** The executor composes a dependent's context at the moment its dependencies have returned: the planner's block first and unchanged, then a structured digest of each *direct* dependency's response, and the composed block is what the runner receives and what the response records.
The digest (`coordinator/handoff.py`) keeps the facts a downstream agent acts on - PR numbers, SHAs, touched paths, judgements with their confidence and evidence ids, gaps with their `suggested_query`, evidence references - and drops the envelope.
Lists are capped with an honest `(+N more)`, values are clipped, and the block has a hard ceiling that cuts on a line boundary and says so; a two-hop chain sees the middle agent's digest, never its composed context, which is what keeps a handoff from snowballing.
It is plain text rather than JSON so a tool-driven agent can quote a line of it as evidence through the same substring grounding it applies to tool replies.

**Why not the raw response.** It was the easy alternative and the one `BUILD_PLAN.md` Phase 4 names as wrong ("pass Incident's digest to Deployment, not the raw dump").
Measured on the fixtures, a sixty-commit GitHub response digests to under a third of its serialised size; on the live run the whole handoff was about 3,000 characters against a Deployment tool loop that re-sends its context every round.

**Why the chain cannot be parallel, in one sentence.** The dependent's context is composed from the dependency's response, so until the dependency returns there is nothing to hand over; starting early would mean starting with the planner's block alone and letting the agent assume the rest, which is the inherited-context failure the project exists to demonstrate the absence of.
The comment lives at the sequential loop in `executor.py`, where a future reader tempted to widen the thread pool will see it.

**What I would watch.** The first live run proved the coordinator will *not* plan a dependency the data does not require: told both SHAs up front, it ran the two agents in parallel, correctly.
A sequential scenario has to carry a real data dependency, and a demo that hands the coordinator the answer is testing the executor against a plan it will never produce.

## 21. A value the runtime can derive is settled by the runtime, not asked of the model - `status` included

**Context.** CONTRACTS.md sec 3: `status` must be `partial` or weaker whenever any findings `Assessment.value` is null.
The first live sequential run's Deployment report left one judgement honestly null with a gap against it and wrote `status: complete`; the envelope validator refused the whole response, and the executor's gap said "1 validation error" because a first-line cut had dropped the line that named the rule.

**Decision.** Every agent now settles `complete` over a null judgement to `partial` in the runtime (`agents/_status.py`), in that direction only - a reported `partial` or `insufficient_evidence` is a judgement and is believed.
The executor's failed-invocation gap keeps the whole exception text, whitespace-collapsed and capped, so the next such refusal names its rule in the response.

**Why this is the same decision as #7 and #17.** `round`, SHAs, file counts, `requires_approval`, and now this half of `status` are values the code already knows; asking a model for one adds a way to be wrong and no way to be right.
The other half of `status` - whether the question was answered - is a judgement and stays the model's.

**What I would watch.** This closes one specific refusal, not the class.
The Day 17 validation-retry loop is still the general answer for a report the contract rejects; the settled `status` just means it will be re-requested for a real defect rather than for bookkeeping.

## 22. The refinement loop consumes gaps as data and stops on four rules, none of them a judgement call

**Context.** BUILD_PLAN Phase 1: "coordinator checks synthesis for gaps and re-delegates with targeted queries until coverage is sufficient."
The contract had already decided what a gap is for: `Gap.suggested_agent` and `Gap.suggested_query` "exist for a machine, not a reader", and `resolvable: false` "is what stops the refinement loop from spinning".
Every agent and the executor's own failure path had been setting them honestly since Day 7.

**Decision.** The loop lives in the executor, after the plan has run, and it reads gaps, not prose.
Every open gap that is `resolvable` and names an agent is grouped by that agent; each agent gets one new `AgentInvocation` per round with `round: 1+`, the gaps' `suggested_query` verbatim as its query, and a context composed the Day 13 way - the planner's block for that agent, a refinement block naming the gaps, then the digest of each response that raised one - recorded verbatim.
A re-delegation that answers closes the gaps it was asked about; its own gaps take their place.
It stops when there is nothing left to ask, when the round cap is reached (default 2), when a gap names an agent that is not registered, or when the same agent would be asked the same query again.
`refinement_rounds` counts the rounds that ran.

**Why "the same query twice" is a stopping rule.** `resolvable: false` is the agent's honesty; the identical-gap rule is the executor's, for the case where an agent is honest but wrong about resolvability.
A gap that comes back with the same suggested query after a round was spent on it is not progress, and the cap alone would spend the second round finding that out.

**Why one invocation per agent per round.** Every round is a full agent run, and the Day 13 sequential run was 264k input tokens before any refinement.
Two gaps that point at the same agent share one invocation with both queries listed verbatim; the cost is one run, not two, and the agent sees both questions together.

**What I would watch.** A closed gap is closed because the re-delegation *answered*, not because it answered well; the new report's own gaps are the only signal that it did not.
That is honest but coarse, and the Day 19 eval is where "did the round actually improve the answer" gets measured.

## 23. Synthesis is a seam with a deterministic fallback, and the model form is grounded in code

**Context.** The Day 7 synthesis adopted the highest-confidence report's summary as the answer and cited that report's evidence ids, so the coordinator never minted an id.
The Day 10 demo showed its limit: with Incident and Docs both reporting, it picked one summary as the headline and dropped the other half of the answer.

**Decision.** `coordinator/synthesis.py` holds both forms behind one `Synthesiser` protocol.
The deterministic form is the default and the one every offline test runs.
`ModelSynthesiser` is opt-in at the entry point, exactly as tracing is: it reads the query, the intent, one bounded digest per response (the same block a dependent agent is handed), and the open gaps, and is forced through one `emit_synthesis` tool.
`check_grounding` then rejects an evidence id no agent carries and a confident answer that cites nothing; on any rejection, truncation, or missing tool call the executor falls back to the deterministic form and says why in `answer.reasoning`.

**Why fall back rather than raise.** By the time synthesis runs, every agent has been paid for.
Throwing the request away for one ungrounded sentence at the end would be the expensive kind of honesty; carrying the sentence would be the dishonest kind.
The fallback keeps the agents' work and loses only the prose, and the reasoning field records that it happened.

**Why the digests and not the raw responses.** The same argument as #20: the digest is what the agents said, bounded, with the evidence ids visible; the raw envelopes are thousands of tokens of plumbing the model would have to re-parse.
It also means the synthesis reads exactly what a downstream agent would have read, so the two consumers of an agent's report cannot drift apart.

**What I would watch.** The grounding check proves the answer cites real ids, not that the prose is faithful to the reports.
A claim-level check (the Docs agent's verbatim-quote rule, applied to the synthesis) is the next step if the Day 19 eval finds the prose drifting.

## 24. The `1.1.0` split changed the name and part 4, and nothing else

**Context.** The routing case study needed an "after".
The baseline had come back 0/40 misrouted, so the split was made because the method requires it, not because the tools had misrouted.

**Decision.** `analyze_logs` / `analyze_events` became `search_container_logs` / `search_recorded_events` in a new server module that imports the v1 implementation and input schemas, composes parts 1-3 from the v1 descriptions at import, and adds a part 4 that names the other tool and states the discriminator in both directions.
A test asserts parts 1-3 are byte-identical to v1; the v1 test that pins its part 4 weak still passes; the v1 server stays runnable, and its definitions stay in CONTRACTS.md struck through.
The rationale was written in `docs/design-notes/contract-changes.md` before the code, per sec 0, and the entry records the tension with sec 0 step 3 (a rename is a removal by the letter, and the pre-authorization fixes the version at `1.1.0` anyway) rather than resolving it silently.

**Why not just rewrite part 4.** A name is the first routing signal a model reads, and the plan said "split and rename"; measuring part 4 alone would understate the intervention the case study was designed around.

**Why keep the v1 module shipped.** The before must be re-runnable over the same wire as the after, or the numbers in `docs/case-study-tool-routing.md` are a memory rather than a measurement.

**What I would watch.** The after came back 0/40 too, and the write-up says so; the levers that would produce a non-zero baseline (a cheaper router model, parts 1-3 written loosely) are named there and are one flag or one module away.

## 25. A bounded digest decides what it may not lose, and the evidence ids are it

**Context.** The handoff digest has a hard 4,000-character ceiling, cut on a line, announced by a marker.
Until Day 15 the cut simply took the tail, and the tail is the evidence list.
A ten-claim Docs report lost nine of its eleven evidence refs that way, and the model synthesis, reading claim lines that showed document ids in brackets, cited `doc_` ids; the grounding check refused the synthesis, correctly.

**Decision.** The evidence list is rendered first and kept out of the cut; the findings and gaps above it are bounded to whatever the ceiling leaves.
The ceiling itself did not move.
Docs claim lines say `docs=doc_0005` rather than `[doc_0005]`, so that across every digest a bracketed list means evidence ids and nothing else.

**Why not raise the ceiling.** The ceiling is what keeps a handoff at a few hundred tokens and a two-hop chain from snowballing; a larger one moves the cliff rather than removing it, and the next long report falls off it the same way.

**Why not loosen the grounding check to accept document ids.** The contract says the coordinator's evidence resolves against the union of its subagents' `evidence[]`.
A document id names a source, not the evidence record with the quote in it; accepting it would make the coordinator's citations weaker than its agents'.
The check was right and the digest was misleading, so the digest changed.

**What I would watch.** The evidence list is itself capped at ten with an honest `(+N more)`, so an eleventh evidence id still cannot be cited by a reader of the digest.
That is a limit on what gets cited, never a wrong citation, which is the right way round.

## 26. The planner's roster is a capability claim, so it gets a test

**Context.** The coordinator knows its agents through one block of text in the planning prompt.
On Day 15 that block said the Incident agent "reads the historical incident corpus" and described Docs as a runbook corpus; neither was true of the system as built, and the plan skipped Docs on a four-part question with a reason that quoted the sentence.

**Decision.** The roster was rewritten from what the agents do today - Incident has no tools and reasons over the context it is handed; Docs answers from the corpus of past incidents and is the agent for precedent - and a test pins the load-bearing phrases.

**Why a test for prompt text, when prompt text is explicitly not frozen.** Most of the prompt is instruction and may churn freely.
The roster is different in kind: it is a statement of fact about other modules, and the coordinator's selection is only as right as it is.
The test does not freeze the wording; it fails when the roster claims corpus access for an agent that has none, which is the drift that cost a live run.

**What I would watch.** Day 15 found this because the scenario finally asked for precedent and diagnosis in one query.
The five-case selection check would not have; it needs a case of that shape, at one planning call.

## 27. The Day 15 cost review kept caching on Day 19 and named a different lever

**Context.** Day 15's second track was the cost review against the $100 alert.
The obvious move was prompt caching, carried in HANDOFF for weeks as an obvious win not yet taken.

**Decision.** `scripts/cost_review.py` prices every recorded live run; measured spend is $7.14, 7% of the alert, and 80% of it is the two multi-round `respond()` checks.
Caching stays on Day 19 where the plan put it, because Day 20 wants the cached-versus-uncached delta measured on the eval suite and that needs an uncached baseline first, and because at this spend the saving is cents per run.

**The lever the review actually found.** In the passing four-agent run, the plan had answered every part of the question by 146.8 s.
Each agent receives the whole multi-part query, so each raised resolvable gaps for the parts that were not its own, pointing at the sibling already answering them.
The refinement loop re-delegated all four agents; that round was about half the bill, three of the four re-delegations were refused by the agents' own rules, and the run ended with 14 open gaps on a correct answer.
The fix belongs in context composition - an agent told which parts of the question other invocations already own has no reason to raise a gap for them - and is recorded as HANDOFF sec 7 item 22 rather than rushed into the integration day.

**What I would watch.** Both cost estimates given before the Day 15 runs were low by 2-3x.
An estimate for a `respond()` run has to be made from the last measured run of the same shape, not from the size of the inputs.
