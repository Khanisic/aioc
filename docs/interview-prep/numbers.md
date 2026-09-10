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
| Total tests | **422** (412 offline + 10 `integration`-marked) |
| Runtime | ~80s offline (the Day 11-12 MCP-wire tests launch real server subprocesses, ~20s of it) |
| Live API calls needed | **0** |

Split: 26 contract, 13 LLM harness, 19 incident agent, 4 chaos mapping, 13 seed corpus, 15 Prometheus context, 29 coordinator, 23 executor, 28 timeline tool, 30 correlate tool, 18 docs agent, 27 retrieval, 10 tracing, 20 github agent, 41 github tool, 9 MCP toolset (real stdio wire, credentials blanked so no network), 24 deployment agent, 73 deployment tool.

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

## What is not measured yet

Say this plainly rather than letting it be discovered:

- **No eval harness.** Accuracy, hallucination rate, and tool-success rate are Day 19. The Day 5 checkpoint is a single-case preview of it.
- **No token-reduction baseline.** `meta.token_estimate` exists on every tool response so there *will* be a baseline; nothing has been reduced yet.
- **No cost/latency aggregate.** Langfuse now traces every request (Day 9), and each response carries measured cost - but nothing aggregates across requests yet.
- **Delegation is verified live on two ad-hoc queries, not a set.** The Day 7 check plus the Day 10 demo runs; the coordinator's routing check has five scored cases, delegation still has none.
- **The sequential path has not run live.** Both agents on it (GitHub, Deployment) pass their own live checks; the handoff between them is Day 13.
- **Prompt caching not enabled.** The system prompt plus tool schema is identical on every call and is an obvious candidate; not yet done.
