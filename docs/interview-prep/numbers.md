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
| Total tests | **255** (245 offline + 10 `integration`-marked) |
| Runtime | ~2.7s with the Docker-backed integration tests included |
| Live API calls needed | **0** |

Split: 26 contract, 13 LLM harness, 19 incident agent, 4 chaos mapping, 13 seed corpus, 15 Prometheus context, 29 coordinator, 23 executor, 28 timeline tool, 30 correlate tool, 18 docs agent, 27 retrieval, 8 tracing.

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

## What is not measured yet

Say this plainly rather than letting it be discovered:

- **No eval harness.** Accuracy, hallucination rate, and tool-success rate are Day 19. The Day 5 checkpoint is a single-case preview of it.
- **No token-reduction baseline.** `meta.token_estimate` exists on every tool response so there *will* be a baseline; nothing has been reduced yet.
- **No cost/latency aggregate.** Langfuse now traces every request (Day 9), and each response carries measured cost - but nothing aggregates across requests yet.
- **Delegation is verified live on two ad-hoc queries, not a set.** The Day 7 check plus the Day 10 demo runs; the coordinator's routing check has five scored cases, delegation still has none.
- **Prompt caching not enabled.** The system prompt plus tool schema is identical on every call and is an obvious candidate; not yet done.
