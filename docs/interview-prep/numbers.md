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

`scripts/check_agent_selection.py`, one live call per case, recorded. **2 of 5 cases run live** (budget); 3 defined but unverified.

| Case | Result |
|---|---|
| `narrow_incident` | **PASS** - selected `[incident]`, skipped 3 with specific reasons, intent `incident_diagnosis` @ 0.92 |
| `sequential_dependency` | **PASS** - `github: parallel`, `deployment: sequential` with `depends_on: ['inv_1']`, intent `mixed` @ 0.85 |
| `pure_docs` | not run |
| `incident_plus_docs_parallel` | not run |
| `deployment_only` | not run |

Context passed per agent: **75 words** (narrow_incident), **37 and 62 words** (sequential_dependency). Non-trivial, so explicit context passing is doing real work rather than satisfying a non-empty check.

Be precise about the 2-of-5 in an interview. The plan's done-when is five queries; two are proven.

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
| Total tests | **141** |
| Runtime | ~1.6s |
| Live API calls needed | **0** |

Split: 25 contract, 13 LLM harness, 18 incident agent, 4 chaos mapping, 13 seed corpus, 15 Prometheus context, 25 coordinator, 28 timeline tool.

Everything model-facing is driven by scripted fake clients. The four live-checking scripts are separate and opt-in, because a suite that costs money per run stops being run.

---

## What is not measured yet

Say this plainly rather than letting it be discovered:

- **No eval harness.** Accuracy, hallucination rate, and tool-success rate are Day 19. The Day 5 checkpoint is a single-case preview of it.
- **No token-reduction baseline.** `meta.token_estimate` exists on every tool response so there *will* be a baseline; nothing has been reduced yet.
- **No cost or latency telemetry.** Langfuse is Day 9. Per-call durations are in `test-results/`, but there is no aggregate.
- **3 of 5 coordinator cases unverified live.**
- **Prompt caching not enabled.** The system prompt plus tool schema is identical on every call and is an obvious candidate; not yet done.
