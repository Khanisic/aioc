# Numbers

Every measured figure in this project, with how it was obtained.
**Do not quote a number in an interview that is not on this page.**

Where a row says "recorded", the run is under `test-results/` with per-attempt JSON, and the query that reproduces it is in `docs/guides/running-tests.md`.

---

## Model selection: structured output vs the frozen contract

Measured by `scripts/check_structured_output.py`, one live call per model per repeat, against the same fixture.

| Model | Contract-valid | Notes |
|---|---|---|
| `claude-haiku-4-5` | **1 / 3** | A different invariant broken each time: a dangling evidence id, then an invalid `suggested_agent` enum |
| `claude-sonnet-5` | **3 / 3** | Stable; `overall_confidence` 0.62-0.68 across runs |
| `claude-opus-5` | 1 / 1 | Correct, ~52s, most expensive |

Single-pass run before the repeats: **all three passed 1/1.** That result would have selected Haiku. The `--repeat 3` run is what caught it.

**Calibration signal.** On the same input, Haiku reported `overall_confidence: 0.82` where Sonnet said 0.62 and Opus 0.63, and Haiku found 3 gaps where Opus found 6. Overconfident and less thorough.

**All three reached the correct diagnosis** (payments-api `resource_exhaustion`, sev2, cascading to checkout-api). The failures were contract discipline, not reasoning.

### The cost arithmetic

| Model | Input $/Mtok | Output $/Mtok |
|---|---|---|
| Haiku 4.5 | $1.00 | $5.00 |
| Sonnet 5 | $3.00 list, **$2.00 introductory** through 2026-08-31 | $15.00 list, **$10.00 intro** |

At intro pricing Haiku is **2x cheaper, not 3x**.
At 1-in-3 validity, ~3 Haiku attempts per usable answer costs the same or more than one Sonnet call.
**Cheap-model-plus-blind-retry was not cheaper.** A retry that re-sends *with the validation error attached* changes that arithmetic; it is Day 17 work.

---

## Before the schema annotation layer

First live structured-output calls, before per-field descriptions were added:

| Model | Failure |
|---|---|
| Haiku 4.5 | **7** validation errors, all `*_detail` set on non-`other` enums |
| Sonnet 5 | Wrapped the entire payload in a `report` key absent from the schema |
| Opus 5 | `overall_confidence: Field required` - actually truncation, see below |

The API **accepted** the generated schema (top-level `object`, `additionalProperties: false`, 18 `$defs`, heavily `$ref`-based). Schema-compatibility was never the problem.

**Truncation.** Opus's `stop_reason` was `max_tokens` at exactly **4096** output tokens - the then-default. Harness default is now **8192**.

---

## Day 5 checkpoint: agent vs injected fault

`scripts/check_day5_checkpoint.py`, one live call, recorded.

| | |
|---|---|
| Injected (read from `chaos_knob_value`) | `downstream_latency`, payments-api `extra_latency_ms=800` |
| Diagnosed | `downstream_latency` @ confidence **0.55** |
| Affected services named | `checkout-api`, `payments-api` |
| Evidence / gaps | 6 / 2 |
| Schema-validated | yes |

The agent saw **only** live Prometheus metrics. `chaos_knob_value` is excluded from agent context by an enforced guard, so this is a real diagnosis rather than a transcription of the answer key.

---

## Day 6 checkpoint: coordinator agent selection

`scripts/check_agent_selection.py`, one live call per case, recorded. **5 of 5 cases pass**, across two runs (the two discriminating cases first, the remaining three later).

| Case | Result |
|---|---|
| `narrow_incident` | **PASS** - selected `[incident]`, skipped 3 with specific reasons, intent `incident_diagnosis` @ 0.92 |
| `sequential_dependency` | **PASS** - `github: parallel`, `deployment: sequential` with `depends_on: ['inv_1']`, intent `mixed` @ 0.85 |
| `pure_docs` | **PASS** - selected `[docs]`, intent `documentation_lookup` @ 0.92 |
| `incident_plus_docs_parallel` | **PASS** - selected `[docs, incident]`, both parallel, intent `mixed` @ 0.85 |
| `deployment_only` | **PASS** - selected `[deployment]`, intent `deployment_check` @ 0.90 |

Context passed per agent, in words: **75** (narrow_incident), **37 / 62** (sequential_dependency), **49** (pure_docs), **37 / 31** (incident_plus_docs_parallel), **60** (deployment_only). Non-trivial in every case, so explicit context passing is doing real work rather than satisfying a non-empty check.

---

## Day 7 checkpoint: delegation end to end

`scripts/check_day7_delegation.py`, 2 live calls (one plan, one diagnose), recorded. Query: *"Checkout is returning 502s to customers. What is actually broken, and how bad is it?"*

| | |
|---|---|
| Selection | `[incident]`, three agents skipped with specific reasons, intent `incident_diagnosis` |
| Context passed | **103 words**, handed to the agent byte-for-byte |
| Sentinel leak | **None.** A coordinator-only marker planted in the situation block did not reach the agent's prompt |
| Cost (both calls) | **12,209 input / 4,149 output tokens**, accumulated from `Usage`, not estimated |
| Status | `partial`, with 4 resolvable gaps - honest rather than complete |
| Answer confidence | **0.55**, correctly implicating payments-api tail latency |

The sentinel is the measurement that matters: the coordinator *saw* a fact it was told was bookkeeping-only, and did not forward it. Explicit context passing is proven at the wire, not just at the runner - the check compares the agent's actual outgoing prompt against `context_passed`.

**The first run of this check failed**, and that is the point of it existing. See war story #7: `round` was in the model-facing schema, Sonnet omitted it, and 186 green offline tests could not have caught it because every fixture was hand-written with the field present.

---

## Chaos injection: the four failure modes

`demo-app/chaos/inject.py`, verified live against the running stack.

| Mode | Probe result | Distinguishing signal |
|---|---|---|
| `downstream_latency` | 8x 200, **mean 812ms** | Slow but *succeeding* |
| `code_regression` | 2/8 **500** | checkout-api fails itself |
| `bad_config_deploy` | 4/8 **502** | 502 not 500 - fault is downstream |
| `resource_exhaustion` | 8x 200, fast | RSS **273 -> 599 MB** over 30 requests, no plateau |

`code_regression` and `bad_config_deploy` separate on **500 vs 502**, which is exactly the call the agent has to make.

**Reversibility is real, not knob-deep.** `--reset` returns latency to 9ms, all 200s, and RSS drops **599 -> 53 MB** - the app frees the ballast.

---

## The incident corpus

`docker/postgres/init/03-seed-incidents.sql`, verified against live Postgres.

| | |
|---|---|
| Incidents | **18** |
| Timeline events | **65** |
| Failure-mode coverage | 4 rows each for the four real modes, 2 for `other` |
| Severity spread | sev1 x3, sev2 x7, sev3 x6, sev4 x2 |

Coverage is deliberate: the Day 19 eval scores `failure_mode` against ground truth, and a mode with zero rows **cannot be scored at all** - the agent could never be right or wrong about it.

---

## Test suite

| | |
|---|---|
| Total tests | **581** (568 offline + 13 `integration`-marked), as of Day 15 |
| Runtime | ~25-45s with the stack up (the Day 11-14 MCP-wire tests launch real server subprocesses, most of it) |
| Live API calls needed | **0** |

Split: 26 contract, 13 LLM harness, 19 incident agent, 4 chaos mapping, 13 seed corpus, 15 Prometheus context, 30 coordinator, 29 executor, 16 refinement loop, 17 synthesis, 16 handoff digest, 28 timeline tool, 30 correlate tool, 69 analyze tools (logs, events, the compose reader, pattern maths), 16 search tools (the `1.1.0` split), 18 docs agent, 27 retrieval, 10 tracing, 20 github agent, 41 github tool, 13 MCP toolset (real stdio wire, credentials blanked so no network), 25 deployment agent, 73 deployment tool, 13 Day 15 dev tooling (the four-agent check's evaluators, the cost review).

Everything model-facing is driven by scripted fake clients. The four live-checking scripts are separate and opt-in, because a suite that costs money per run stops being run.

**And the honest counterweight, measured:** those 186 green tests did not catch the `round` bug that two live calls found on the first attempt (war story #7). Fake-driven tests inherit the assumptions of whoever wrote the fixtures. The offline suite proves the wiring; only a live call proves the model will fill it.

---

## The Day 10 end-to-end demo, measured

Two live runs of `scripts/demo_day10.py` against injected `downstream_latency` chaos (payments-api +800ms), 2026-08-22, both traced to Langfuse and recorded under `test-results/`.

**Run 1 - the canonical query** ("Why did latency spike after the last deploy?"):

| | |
|---|---|
| Claude calls | **2** (plan + Incident; the coordinator *skipped* Docs, GitHub, Deployment with reasons) |
| Cost | 12,469 in / 4,635 out tokens |
| Wall clock | 40.1s |
| Diagnosis | `downstream_latency` @ 0.62 - matches the injected truth |

The interesting number is the 2, not the diagnosis: the demo script predicted Incident + Docs in parallel, and the coordinator correctly judged a pure diagnostic query needs no documentation lookup. Dynamic selection deciding *against* the demo author's expectation is the behaviour working, not failing - an empty `skipped_agents` would have been the bug.

**Run 2 - the showcase query** (same, plus "how have we resolved similar payments-api latency incidents before?"):

| | |
|---|---|
| Claude calls | **3** (plan + Incident + Docs, the two agents in parallel) |
| Cost | 20,285 in / 7,742 out tokens |
| Wall clock | 41.2s - two agents for roughly the wall-clock price of one (run 1 ran one agent in 40.1s) |
| Intent | `mixed` @ 0.85 |
| Diagnosis | `downstream_latency` @ 0.72 - matches the injected truth |
| Docs grounding | 7 supported claims across 4 corpus documents, every quote verbatim (the in-code checks passed live) |

Run 2 is also the first live proof of the Day 8 Docs agent and the Day 9 trace-on-a-real-request, in one spend. The near-identical wall clocks are the parallel executor visible in production numbers; the trace shows the two agent spans overlapping.

One visible limitation, on purpose: the deterministic Day 7 synthesis adopted the Docs report (confidence 0.60) as the top-line answer over the Incident diagnosis (0.58), so the headline answers the historical half of the question. Merging both halves is exactly what the Day 14 model-written synthesis exists to buy.

---

## The Day 11 GitHub agent, live

`scripts/check_day11_github.py`, PR #12 of this repository, Sonnet, 2026-08-23. Seven live attempts before the first clean pass; every failure was in the harness or the prompt, never in the wire - and each is now a named regression test.

| Attempt | Outcome | Cause | Fix |
|---|---|---|---|
| 1 | `ValidationError`: `findings.gaps` extra field | the model nested `gaps: []` inside `findings`; the rule lived only in the system prompt | the no-nesting rule stated on the schema object itself (the Day 4 lesson again) |
| 2 | grounding rejected a verbatim excerpt | the check compared excerpts against the raw JSON wire text, where quotes and newlines are escaped | ground against the decoded string values |
| 3-4 | grounding rejected `"touched_paths": [...]` | the model quoted the wire text literally, which the decoded-only check then refused; also a list quoted as `a, b, c` | accept raw wire text, decoded leaves, `", "`-joined string lists, and patch hunks quoted without their `+`/`-` markers |
| 5 | contract sec 2.1: `symptom_link` value non-null at confidence 0.1 | the model listed a change to say it did *not* explain a symptom | schema guidance: a change below 0.25 is not a suspect; say so in `diff_summary` |
| 6 | **PASS**, but two phantom `transient` tool calls | the model called `emit_github_report` during the investigation, where it was not offered; the harness's "unknown tool" error was recorded as a wire call | the emit tool is offered in the loop with a capturing handler; non-wire records never become `ToolCallRef`s |
| 7 | **PASS**, clean | | |

(Attempt 4 was a wasted repeat of 3: an edit script aborted before writing and the run went out unchanged. Counted honestly.)

| | Attempt 6 (three-call path) | Attempt 7 (emit inside the loop) |
|---|---|---|
| Claude calls | 3 (two investigation rounds + forced emit) | 2 |
| Wire calls | 1 (`get_pull_request`, include_patch) | 1 |
| Input / output tokens | 89,984 / 7,908 | 56,643 / 2,295 |
| Wall clock | 81.8 s | 27.4 s |
| Verdict | PR #12 merged, 8 files +549/-48, risk **low @ 0.85**, `diff_summary` @ 0.85 | same |
| Evidence / gaps | 6 / 2 (two patches truncated at 4000 chars - reported, not hidden) | 3 / 1 |

The input-token number is dominated by the PR's patch (~7.1k tokens by `meta.token_estimate`) being re-sent on every round, which is the Day 21 trimming work's target. The rejected reports from attempts 1-6 are saved under `test-results/runs/2026-08-23/` - `GitHubAgentError` now carries the report it refused, which is the input the Day 17 validation-retry loop consumes.

---

## The Day 12 Deployment agent, live

`scripts/check_day12_deployment.py --deploy`, this repository, Sonnet, 2026-09-09. **Passed on the first attempt** - the Day 11 harness lessons (emit tool offered in the loop, grounding against decoded text and raw wire text, no-nesting rule on the schema object) carried over, and the two new rules the agent enforces (a service or release no tool returned is refused; a tool not run needs a gap against its fields) were both satisfied by the model with no guidance beyond the schema.

The setup is part of the measurement: the demo services report whatever `DEMO_GIT_SHA` they were started with, so the script recreated the containers at `origin/main` (`1522000`) and confirmed the deployed version through the server itself (zero Claude calls) before the agent ran.

| | Value |
|---|---|
| Releases compared | `7e5c94f` (16 first-parent commits back) -> `1522000` (`origin/main`) |
| Claude calls | 4 (three investigation rounds + forced emit) |
| Wire calls | 2: `diff_release` 3,577 ms (~7,398 tokens), `check_rollout_health` 250 ms (~90 tokens) |
| Input / output tokens | 62,655 / 5,212 |
| Wall clock | 51.9 s |
| Stamped from the diff | 7 config keys (`DEMO_GIT_SHA`, `DOWNSTREAM_URLS`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `SELF_TRAFFIC_SECONDS`, `SERVICE_NAME`, `VOYAGE_API_KEY`), 1 image change (`checkout-api`: none -> `aioc-demo-service:day3`) |
| Stamped from health | ready 1/1, error rate 0.0, p99 19.1 ms, 1 restart, 1 failed scrape, over 1,800 s |
| Verdict | rollout **degraded @ 0.90**, regression suspected **true @ 0.55**, recommend **hold_and_monitor @ 0.60**, risk low, blast radius "checkout-api in development, single replica" |
| Evidence / gaps | 8 (4 config, 2 deployment, 2 metric - every one stamped with the tool call that returned it) / 1 |
| Status / overall confidence | `complete` / 0.68 |

Three things worth saying about the verdict rather than just the pass:

- **The `degraded` is correct and its cause is the measurement itself.** Recreating the containers at the release under test is one restart and one failed scrape inside the 30-minute window, and the tool's deterministic rule says any restart or failed probe is `degraded`. The model read it that way, cited `"status": "degraded"` and the replica block verbatim, and put the regression at 0.55 with the reasoning that the release rewrote `services.checkout-api.healthcheck` and the probe failure is temporally correlated - a plausible hypothesis, scored as one.
- **The one gap is honest.** `compared_to_baseline` was `null` (the previous identity, `baseline`, had been replaced 20 seconds earlier, inside the two-scrape settling margin, so it was not yet a baseline), and the model recorded that as a resolvable gap against `findings.regression_suspected.value` with a wider-lookback query suggested - which is exactly what the Day 14 refinement loop will consume.
- **Every excerpt was a fact.** Four quoted the diff reply (a key list, an image-change object, two manifest paths), four quoted the health reply (the status, the replica block, the signal line with its numbers, and `"compared_to_baseline": null`). Nothing was paraphrased and nothing came from outside the two replies and the context.

The input-token number is the same story as Day 11: the diff reply (~7.4k tokens, mostly the 16 commit messages at up to 1000 characters each) is re-sent every round. It is the Day 21 trimming target and is carried in `HANDOFF.md` sec 7.

---

## The Day 13 sequential path, live

`scripts/check_day13_sequential.py --deploy`, PR #15 of this repository, Sonnet, 2026-09-13. Two attempts; the first failed for a reason worth keeping.

| Attempt | Outcome | Cause |
|---|---|---|
| 1 | FAIL: both agents planned `parallel`, Deployment refused with a validation error | the situation block told the coordinator both release SHAs, so Deployment needed nothing from GitHub and the coordinator (correctly) did not serialise them; separately, Deployment wrote `status: complete` over one honestly null judgement |
| 2 | **PASS** | the release identity is reachable only through the PR; `status` is settled by the runtime |

Attempt 1 is the graded behaviour working against the demo author: dynamic selection will not invent a dependency the data does not require. The scenario was changed, not the coordinator.

| | Attempt 2 |
|---|---|
| Plan | `mixed` @ 0.95; github `parallel`, deployment `sequential` after `inv_github`; incident and docs skipped with reasons |
| Claude calls | 8 (plan 1, GitHub 2 rounds + emit, Deployment 3 rounds + emit) |
| Wire calls | GitHub: `get_pull_request` 1,375 ms (~22.3k tokens), `list_commits` 358 ms. Deployment: `diff_release` 2,391 ms (~7.2k tokens), `check_rollout_health` x2 (264 ms, 327 ms) |
| Timing | GitHub 0.0 -> 56.8 s, Deployment 56.8 -> 134.8 s; 143.8 s wall |
| Deployment's context | 3,706 chars: 642 of planner block, then the GitHub digest (24 lines) |
| What crossed the handoff | PR #15 merged, head `d22eba1`, merge commit `a921f4c` (in GitHub's summary line), 27 files +4,619/-177, touched paths, risk medium @ 0.60, two gaps |
| What Deployment did with it | diffed `9de137a` -> `a921f4c` (the merge commit it read from the digest), checked health for the running `d22eba1` and for `a921f4c` |
| Deployment verdict | 3 config keys added (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `VOYAGE_API_KEY`), no image change; rollout `degraded` @ 0.85 (2 restarts, 1 probe failure - the redeploy itself), regression `true` @ 0.40, `hold_and_monitor` @ 0.55, risk medium; 9 evidence, 2 gaps |
| Cost | 263,758 in / 13,644 out tokens |
| Status | `partial` (GitHub's two honest gaps carry through), 4 unresolved gaps |
| Trace | `62281faa39eebea2b509191da7838917` - the two agent spans do not overlap |

Two things to say plainly:

- **The digest was consumed, not cited.** Deployment used the merge commit from GitHub's summary as its `to_version`, which it could not have known otherwise, but all nine of its evidence entries quote tool replies; none quotes the context block. The grounding path for context quotes exists and is tested; this model preferred the wire.
- **The input-token number is the Day 21 target, twice over.** PR #15 is a 27-file, +4.6k-line PR, and `get_pull_request` returned ~22k tokens that were re-sent on every GitHub round; Deployment's diff reply was ~7k tokens re-sent on every Deployment round. Attempt 1 alone was 176.6k input tokens for the GitHub half.

---

## The Day 13 routing baseline (the case study's "before")

`scripts/check_tool_routing.py`, v1 descriptions listed from the `aioc-analyze` server over the wire, Sonnet, `tool_choice: any`, 2026-09-13.

| Query set | Misrouted | Input / output tokens | `queries_sha256` |
|---|---|---|---|
| `plain` (20: ten need process output, ten need the recorded history; phrased as an operator would) | **0 / 20** | 57,305 / 2,478 | `b0da5960...` |
| `hard` (20: same ground truths, surface vocabulary pulling the wrong way - "the deploy log", "the operational output", "analyze its activity") | **0 / 20** | 57,334 / 2,474 | `9e31bedb...` |

The `hard` set was written after the `plain` set came back clean and before it was run, because a set that cannot produce a misroute cannot measure an intervention. It did not produce one either.

What this says, honestly: with parts 1 and 3 of the sec 6.5 template stating what each tool reads (what the process printed versus what was recorded as happening to it), the contract's deliberately weak part 4 cost nothing on 40 queries with this model. The premise of the case study - that overlapping tools misroute - did not materialise under these conditions. Day 14 still splits and renames under the pre-authorised `1.1.0` bump and re-runs both sets, and the write-up reports 0 -> 0 if that is what it measures. The levers that would produce a non-zero baseline, in the order I would pull them: a cheaper router model (Haiku, which is also the Day 23 question), and parts 1 and 3 written as loosely as part 4 (what a hastily written real tool looks like). Neither was run unasked.

---

## The Day 14 routing re-run (the case study's "after")

`scripts/check_tool_routing.py --variant v1_1`, the `1.1.0` descriptions listed from the `aioc-search` server over the wire, Sonnet, `tool_choice: any`, 2026-09-17.
Same forty queries, same hashes.

| Query set | Misrouted | Input / output tokens | `queries_sha256` |
|---|---|---|---|
| `plain` | **0 / 20** | 62,925 / 2,475 | `b0da5960...` |
| `hard` | **0 / 20** | 62,954 / 2,522 | `9e31bedb...` |

Before and after side by side:

| | `v1` (before) | `v1_1` (after) |
|---|---|---|
| Names | `analyze_logs` / `analyze_events` | `search_container_logs` / `search_recorded_events` |
| Part 4 | the contract's v1 sentence, no alternative named | names the other tool, states the discriminator both ways |
| Parts 1-3 | complete | byte-identical to v1 (pinned by a test) |
| `plain` misrouted | 0 / 20 | 0 / 20 |
| `hard` misrouted | 0 / 20 | 0 / 20 |
| Input tokens per call | ~2,866 | ~3,147 (+10%, the longer part 4) |
| Mean latency per call | - | 1.8 s |

**0 -> 0.** The intervention did not move a number that was already at the floor.
What that means, and the two levers that would produce a non-zero baseline (a cheaper router via `--model`, or parts 1-3 written as loosely as part 4 in a second `v1` module), are in `docs/case-study-tool-routing.md`.
Neither lever was pulled unasked.

---

## The Day 14 refinement loop and synthesis, live

`scripts/check_day13_sequential.py --deploy` with the loop (cap 2) and the model-written synthesis on, PR #15, Sonnet, 2026-09-17. Three attempts; each taught something and the record keeps all three.

| Attempt | Outcome | Cause | Cost |
|---|---|---|---|
| 1 | FAIL: Deployment produced no response; synthesis fell back | round 0 GitHub refused its own report (paraphrased excerpt); the loop retried it (round 1) and it answered with a gap pointing at Deployment; the loop ran Deployment (round 2) with GitHub's digest, and Deployment refused its own report because the demo was deployed at the PR *head* while GitHub had correctly named the merge commit as the release. The synthesis model wrote the nested `answer` object as XML-style text | 373.7k in / 16.8k out, 175.0 s |
| 2 | FAIL: GitHub refused twice | the fix added `merge_commit_sha` to the PR reply, and the model then reported the merge commit as a commit it had never fetched with `list_commits` | 232.7k in / 12.8k out, 132.5 s |
| 3 | **PASS** | merge commit resolved from the local clone, synthesis schema flat, the check evaluates the invocation that stands for each agent | 344.0k in / 18.6k out, 191.9 s |

Attempt 3, round by round:

| Round | Invocation | Mode | Context | Result |
|---|---|---|---|---|
| 0 | GitHub | parallel | 352 chars (planner) | `partial` @ 0.75, `get_pull_request` + `list_commits`, 6 evidence, 1 gap -> Deployment |
| 0 | Deployment | sequential after GitHub | 3,399 chars (planner + GitHub digest) | `complete` @ 0.62, `diff_release` + `check_rollout_health`, 7 evidence; 3 config keys added, no image change, rollout `degraded`; 1 gap (`compared_to_baseline` null, wider lookback suggested) -> Deployment |
| 1 | Deployment | sequential after both round-0 invocations | 7,246 chars (planner + refinement block + two digests) | refused by the agent's grounding: `from_version` not returned by a tool |
| 2 | Deployment | parallel (retry of the round-1 failure) | 1,313 chars (planner + refinement block) | refused by report validation: a gap with `suggested_agent` and no `suggested_query` |

| | |
|---|---|
| Plan | `mixed` @ 0.90; incident and docs skipped with reasons |
| Refinement rounds | 2 (the cap) |
| Synthesis | model-written, 2,505 chars; answer @ 0.65 citing 9 evidence ids across both agents; says two follow-up attempts to close the baseline gap failed |
| Status | `partial`, 4 unresolved gaps (GitHub's out-of-scope gap, Deployment's null baseline, the two failed re-delegations) |
| Timing | GitHub 0.0 -> 39.5 s, Deployment 39.5 -> 93.5 s, round 1 93.5 -> 133.5 s, round 2 133.5 -> 168.1 s; 191.9 s wall |
| Trace | `f89929cd56da402d6b94dc1f84286617` |

Things to say plainly:

- **The loop did the right thing every time and closed nothing.** Both re-delegations targeted the right agent with the right query and the right context (the round-1 context carried both round-0 digests), and both were refused by the Deployment agent's own honesty rules. The general fix is the Day 17 validation-retry loop - re-request *with the error attached* - not a looser refinement loop.
- **The synthesis is the first model-written coordinator answer, and it is grounded.** Nine evidence ids, all real, both agents cited, the open gap named. The flat schema (five scalars) is what made it parse; the nested form did not.
- **Cost.** Three attempts were 950k input tokens. Each Deployment round re-sends the ~7k-token diff reply and each GitHub round the ~22k-token PR read (the Day 21 target); the synthesis call itself was ~3.5k input tokens.

---

## The Day 15 four-agent run, live

`scripts/check_day15_integration.py --deploy`, Sonnet, 2026-09-18.
`downstream_latency` injected (payments-api +800 ms), the demo deployed at PR #11's merge commit `c729c72`, the previous release its first parent `884b0b6`.
The query asks what is failing, what past incidents say, what PR #11 changed, and whether the release that shipped it changed `checkout-api`'s configuration or images.
The release's identity is reachable only through the PR, as on Day 13.

| Attempt | Outcome | Cause | Cost |
|---|---|---|---|
| 1 (cap 2) | FAIL: the plan did not select Docs; synthesis fell back; one evaluator complaint | the planner's roster said the Incident agent "reads the historical incident corpus", so the plan skipped Docs with a reason quoting it (the loop then ran Docs in round 1 off Incident's gap); round-0 GitHub refused its own report (paraphrased excerpt) so round-0 Deployment never ran; the model synthesis cited `doc_0005` / `doc_0009` / `doc_0017` and the grounding check refused it - the Docs digest had lost 9 of its 11 evidence refs to the ceiling and showed document ids in the evidence-id slot; the evaluator compared a round-1 re-delegation with round 2's context | 348.0k in / 38.5k out, 265.5 s |
| replay | grounded | the model synthesis re-run over attempt 1's five recorded reports after the digest fix: answer @ 0.62 citing 25 real evidence ids across all four agents | 8.9k in / 2.3k out, 1 call |
| 2 (cap 1) | **PASS** | roster corrected, evidence list kept out of the cut, claim lines say `docs=`, evaluator reads the planner's block from round 0 | 342.9k in / 47.5k out, 279.0 s |

Attempt 2, invocation by invocation:

| Round | Invocation | Mode | Context | Span | Result |
|---|---|---|---|---|---|
| 0 | Incident | parallel | 1,108 chars | 0.0 -> 22.9 s | `partial` @ 0.45, no tools; latency propagating from payments-api; 4 gaps |
| 0 | Docs | parallel | 586 chars | 0.0 -> 33.5 s | `partial` @ 0.50, `search_corpus`; two precedents (inc_0004, inc_0017); 3 gaps |
| 0 | GitHub | parallel | 439 chars | 0.0 -> 78.5 s | `partial` @ 0.55, `get_pull_request` + 2x `list_commits`; PR #11 touches tracing only; names the merge commit; 3 gaps |
| 0 | Deployment | sequential after GitHub | 4,118 chars (planner + GitHub digest) | 78.5 -> 146.8 s | `partial` @ 0.60, `diff_release` + `check_rollout_health`; no config, image, or manifest change; rollout `degraded`; 3 gaps |
| 1 | GitHub | sequential | 9,038 chars | 146.9 -> 186.5 s | refused by the agent's grounding: a paraphrased excerpt |
| 1 | Deployment | sequential | 16,932 chars | 146.9 -> 240.1 s | refused by the agent's grounding: `from_version` not returned by a tool (null baseline) |
| 1 | Docs | sequential | 5,233 chars | 146.9 -> 191.1 s | refused by report validation: an extra field the model invented |
| 1 | Incident | sequential | 18,429 chars (four digests) | 146.9 -> 189.9 s | `partial` @ 0.50; rules PR #11 out using GitHub's and Deployment's findings |

| | |
|---|---|
| Plan | `mixed` @ 0.90; all four selected, nothing skipped - correct for a query with four parts |
| Parallel path | three overlapping pairs: Incident + Docs, Incident + GitHub, Docs + GitHub |
| Sequential path | Deployment started 0.0 s after GitHub ended, GitHub's digest after the planner's block |
| Synthesis | model-written; answer @ 0.60 citing 13 evidence ids; names the open gaps and the three failed re-delegations |
| Status | `partial`, 14 unresolved gaps |
| Trace | `e4619ab3810b99b8da538764c599ed7e` (attempt 1: `c0ca4148f190838cc44cce2a06d19f75`) |

Things to say plainly:

- **The answer was right, and it contradicted the premise.** The on-call suspected the release. Three agents independently said no: the PR touches tracing code only, the release diff is empty, and the metrics put the slow origin in payments-api. The check asserts the orchestration, never the conclusion, so this is the system working rather than the scenario being steered.
- **The small PR did what it was chosen for, and the total did not move.** PR #11's read is ~1.1k tokens against PR #15's ~22k, the release diff ~0.4k against ~7.4k. The run still cost 343k input tokens, the same as Day 14's two-agent run, because there are now four agents and a refinement round that re-ran all four.
- **Round 1 was about half the bill and closed almost nothing.** The plan had answered every part of the question by 146.8 s. Each agent receives the whole four-part query, so each raised gaps for the parts that were not its own, pointing at the sibling that was already answering them. The loop did what it is built to do with a resolvable gap and re-delegated all four agents; three were refused by their own rules (two are HANDOFF §7 item 20's, the third is new) and one answered. 14 open gaps on a correct answer is noise, and it is the next cost lever (HANDOFF §7 item 22).
- **Both estimates given before the runs were low**, and the record should say so: $0.15-0.40 quoted for attempt 1 (actual ~$1.08) and $0.40-0.90 for attempt 2 (actual ~$1.16). The small PR was priced in; the refinement fan-out and 38-47k output tokens were not.

---

## The Day 15 cost review

`uv run python scripts/cost_review.py` (free; reads `test-results/`), 2026-09-18, Sonnet 5 at $2 / $10 per million tokens, no caching on any recorded run.

| Check | Runs | Unmeasured | Input tokens | Output tokens | USD |
|---|---|---|---|---|---|
| `day13-sequential` (Days 13-14) | 5 | - | 1,390,782 | 70,497 | 3.49 |
| `day15-integration` | 2 | - | 690,938 | 85,931 | 2.24 |
| `day11-github` | 9 | 7 | 146,627 | 10,203 | 0.40 |
| `tool-routing-v1_1` | 1 | - | 125,879 | 4,997 | 0.30 |
| `tool-routing-v1` | 2 | - | 114,639 | 4,952 | 0.28 |
| `day10-demo` | 2 | - | 32,754 | 12,377 | 0.19 |
| `day12-deployment` | 1 | - | 62,655 | 5,212 | 0.18 |
| `day7-delegation` | 2 | 1 | 12,209 | 4,149 | 0.07 |
| four early checks | 8 | 8 | - | - | - |
| **Total (measured)** | **32** | **16** | **2,576,483** | **198,318** | **7.14** |

- **7.1% of the $100 alert, as a floor.** Sixteen runs recorded no usage: the Days 4-7 checks predate the `Usage` seam, and seven Day 11 runs died on the revoked key before a first response. Ad-hoc calls (the two one-call synthesis replays) and Voyage are not in it. The Console is the bill; this is the breakdown the Console does not give.
- **80% of measured spend is two checks**, and both are the multi-round `respond()` path. A single-agent check is $0.07-0.20; a full request with the loop on is ~$0.70-1.20.
- **Prompt caching stays on Day 19.** At this spend the saving is cents per run, and Day 20 wants the cached-versus-uncached delta measured on the eval suite, which needs an uncached baseline first. `cost_review.py` prices every input token at the full rate and says so; the run records need the cache counters when caching lands.
- **The lever Day 15 found is not in the token price at all**: the refinement round that re-asks four agents for work their siblings already did.

---

## Day 16 - the approval gate over recorded live output (free)

`scripts/gate_recorded_run.py` put every recorded `respond()` response through the HITL gate with the default fail-closed approver, 2026-09-25.
Zero API calls.

| | |
|---|---|
| Recorded responses gated | 9 (the Day 5 record predates the coordinator and is skipped, not coerced) |
| Recommendations | 15, all from the Incident agent; no Deployment report recommended `rollback_now` |
| Needing a human | 7 - every one flagged by the agent *and* rated medium risk; the classifier added the mutation kind to 5 of them |
| Released without asking | 8, each recorded with the reason (not flagged, low risk, no production write recognised) |
| Withheld under `DenyAll` | 7 of 7 |
| Classifier misreadings the replay found | 3 of 15, all fixed and pinned verbatim in `tests/test_hitl.py` ("after deploy" read as a deploy, "Hold off on rolling back" read as a rollback, "tighten a circuit breaker" missed as a config write) |

- **The agents were already gating honestly.** No recorded action was a production write the agent had left unflagged, so on this sample the classifier's job was confirmation, not rescue. It earns its place on the reports this sample does not contain, and the replay is how to keep checking that.
- **Over-gating is visible in the record.** One action the agent flagged medium is read-only ("Investigate ... timeout configuration values"); the gate believes the agent's stricter judgement and says which signal gated it.

---

## Day 17 - the audit log against the real database, and the retry loop offline (free)

`scripts/gate_recorded_run.py --persist` wrote every recorded decision to `hitl_audit_log` on the stack's Postgres, 2026-09-28.
Zero API calls.

| | |
|---|---|
| Decisions written | 15 (9 recorded responses; the Day 5 record skipped as before) |
| Read back by `scripts/audit_log.py` | 15, in append order; 8 `not_required`, 7 `denied` under `DenyAll` |
| `UPDATE hitl_audit_log SET decision='approved'` at `psql` | refused by the trigger (`hitl_audit_log is append-only: UPDATE is not allowed`) |
| Offline suite | 707 passed with the stack up (666 before Day 17; 24 retry-loop tests, 17 audit-log tests of which 3 need the stack) |

- **The retry loop has no live numbers yet.** It is proven offline against scripted refusals shaped exactly like the three live ones (a paraphrased excerpt, a report about a version no tool returned, a null value with no gap), and every `respond()` script now prints and records the `RetryLog` summary (`retries.json`), so the first live run after Day 17 produces the recovered-by-kind rate for free.
- **What a retry costs is known from Day 15:** one more model call carrying the whole conversation, so for the GitHub agent roughly the PR read again (item 16). The cap of 2 and the identical-rejection rule bound it.

---

## Day 18 - every recorded judgement by band, and the Docs chain (free)

`scripts/confidence_report.py` read the 9 recorded `respond()` responses field by field, 2026-09-28.
Zero API calls.

| | |
|---|---|
| Judgements | 105 across 9 responses (every `Assessment`, plus the Docs claims); 7 null |
| By band | 17 two-sources (0.90+), 50 single-source, 27 inferred, 10 hypothesis, 1 speculation |
| Fields over-claiming their band | 0 - no 0.90+ field cites fewer than two evidence ids, no overall exceeds every field |
| Unsupported claims above the floor | 5 (two at 0.90: "the corpus contains no document about PR #11", stated with two-source confidence and no source) |
| Docs coverage | 6/11 sub-questions answered across 3 reports; every unanswered one carries its own gap |
| Claim chain | 23/28 claims supported; all 23 trace to a document evidence entry and the `search_corpus` call behind it |

- **Calibration floor, not a score.** No field claims more evidence than it cites, which is what the eval harness (Day 19) starts from; whether the confidences are *right* needs the injected ground truth.
- **The flag the report invented.** `unsupported_claim_above_floor` did not exist until the first run showed the 0.90 negatives; the Docs emit guidance now puts an unsupported claim's confidence below 0.25.
- **The Day 15 decomposition is the roster's case in numbers.** Three of the four unanswered sub-questions in that run were siblings' parts, each gapped and pointed at the sibling already answering it.

---

## Day 19 - the eval harness and the two cost levers, offline (free)

Everything below was measured without a model call, 2026-09-29.
The live run was attempted and refused: the API key in `.env` has been revoked (`401 API key is invalid`, on the free `models.list` call too), so **nothing on this page is a live score and nothing was spent**.

| | |
|---|---|
| Eval set | `seeded-incidents` v1: 20 cases, 38 items (18 diagnose, 20 recall of which 2 are no-precedent probes) |
| Answer key coverage | `resource_exhaustion` 4, `bad_config_deploy` 4, `code_regression` 4, `downstream_latency` 4, `other` 2 |
| Lines shown to an agent that are not verbatim from the seed | 0, by construction and by test |
| Seeded lines withheld from the diagnoses | 5 of 37 summary sentences and 22 of 65 timeline events (every finding and every fix), plus every title, root cause, resolution, and recorded severity |
| Request size, `--dry-run` | ~4.7k input tokens a diagnosis, ~5.1k a recall (characters / 4, an estimate) |
| Projected price of one full run | ~$1.13 realtime and uncached, ~$0.57 through the Batch API, before caching (187k input estimated, 2,000 output tokens a report assumed from the Day 10 runs) |
| Tool success over the recorded live runs | 30/30 across 19 agent reports in 10 runs: Deployment 15/15, GitHub 12/12, Docs 3/3 (`run_evals.py --recorded-tools`) |
| Offline suite | 918 passed with the stack up (749 before Day 19; 27 caching and pricing tests, 22 batch, 98 eval harness of which 1 needs the stack, 22 script) |

- **The 100% tool success is a survivor's number.** A recorded response is one the agent accepted; a run that died on a tool error left no response to count. It is the rate among reports that were delivered, and it says so.
- **What the cache will save is arithmetic until it is measured.** The shared prefix (system prompt plus emit schema) is about 4.4k of a diagnosis's ~4.7k tokens and 3.8k of a recall's ~5.1k. There are two prefixes, so 36 of 38 requests would read theirs at a tenth of the input price: about $0.26 off the projected $1.13, since output is two thirds of the bill and the cache does not touch it. The Batch API halves everything and is the larger lever on this workload.
- **The projection is a ceiling for the plan and a floor for surprise.** Both Day 15 estimates were low by 2-3x because refinement rounds multiplied them; an eval item is one forced call with no loop, so the only multiplier is the validation retry, capped at two.

---

## Day 20 - the baseline: the plan, and no measurement

`evaluations/baseline.md` does not exist.
The API key was still refused on 2026-09-29 (`401 API key is invalid`), so the run the day is named for has not been made.
This section is the plan the run will be held to, written before it, so the measured numbers can be read against what was expected rather than explained afterwards.

| Step | Calls | Projected, before caching |
|---|---|---|
| Smoke test: 4 diagnoses, realtime, cached | 4 | ~$0.12 |
| Realtime, uncached - the reference | 38 | ~$1.13 |
| Realtime, cached | 38 | ~$1.13 (about $0.87 if 36 of 38 requests read their prefix) |
| Batch, cached, 1h TTL | 38 | ~$0.57 |
| Total | 118 | ~$2.95 (about $2.60 after caching) |

| | |
|---|---|
| Source of the projection | `scripts/check_day20_baseline.py --plan`: every request built and sized offline, ~187k input tokens a run (characters / 4), 2,000 output tokens a report assumed |
| Offline suite | 970 passed with the stack up (918 before Day 20; 51 baseline tests, 1 for the run-id fix) |
| Measured | nothing |

- **What would count as a surprise.** Any lever that costs more than its own tokens would have uncached; a realtime cached run with no cache reads; the three runs scoring more than a fifth of the items differently. The checkpoint fails on each of these by name.
- **What would not.** Failure-mode accuracy well under 100% (three cases are hard on purpose), severity worse than failure mode (recorded severities are withheld), and a batch with few or no cache reads (its requests run concurrently, so they may all be written before any can be read).
- **The projection's weak assumption is the output.** Output is two thirds of the projected bill and 2,000 tokens a report is one number from the Day 10 runs. If reports run to 3,000, the total is nearer $4.

---

## What is not measured yet

Say this plainly rather than letting it be discovered:

- **No eval score and no baseline.** The harness (Day 19) and the baseline checkpoint (Day 20) exist and have never run against a model: the key was revoked before the first live call. Accuracy, hallucination rate, calibration, and both cost deltas are unmeasured; tool success is measured only over recorded runs.
- **No token-reduction baseline.** `meta.token_estimate` exists on every tool response so there *will* be a baseline; nothing has been reduced yet.
- **No latency aggregate, and the cost aggregate is a floor.** Langfuse traces every request (Day 9) and each response carries measured cost; since Day 15 `scripts/cost_review.py` adds the recorded runs up by check and by day, but half of them predate usage recording and nothing aggregates latency.
- **Delegation is verified live on two ad-hoc queries, not a set.** The Day 7 check plus the Day 10 demo runs; the coordinator's routing check has five scored cases, delegation still has none.
- **The routing case study is 0/40 before and 0/40 after, with Sonnet.** The write-up (`docs/case-study-tool-routing.md`) names the two levers that would produce a non-zero baseline; neither has been run.
- **Prompt caching is on and unmeasured.** Every request marks its system block since Day 19 and the counters are recorded, but no response has reported a cache read yet. Whether a batch's requests read each other's cache at all is also open; that is Day 20's measurement.
- **The Batch API path has never met the real API.** It is proven against a scripted batch endpoint, including the retry as a second batch. Its first live run is Day 20's.
