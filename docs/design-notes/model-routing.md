# Model routing: a role setting, and a projection at $0 (Day 23)

The plan's Day 23 asks for a routing experiment: Haiku or Sonnet for the subagents, Opus for the coordinator, with cost and latency measured per configuration.
From Day 22 the project spends nothing on the API (HANDOFF sec 1), so this note records what was built, what was projected instead of measured, and what the projection cannot say.
The generated tables are `evaluations/routing/projection.md`; this note is the method.

## Routing is a configuration change

Two settings, read by `LLMSettings` like every other harness setting, so a value in `.env` takes effect:

- `AIOC_COORDINATOR_MODEL` - the planner and the model-written synthesis.
- `AIOC_SUBAGENT_MODEL` - the four agents, and the eval scripts, which score two of them.

Either falls back to `AIOC_MODEL`.
`LLMSettings.for_role("coordinator" | "subagent")` is the one place a role's model is chosen; every component's default client goes through it.

Three defects sat under the old seam, all found by reading the code before projecting anything:

- The planner read `AIOC_COORDINATOR_MODEL` from the process environment only, and `.env.example` told the reader to set it in `.env`, where it did nothing.
- `AIOC_SUBAGENT_MODEL` was listed in `.env.example` and read by nothing.
- The model-written synthesis is coordinator work and ran the subagents' model, so "Opus for the coordinator" would have moved planning and not synthesis.

Each is pinned by a test in `tests/test_model_routing.py`.

## What can be calls-free, and what cannot

| Question | Answered here? | How |
|---|---|---|
| Cost of each route on work already done | Projected | Measured tokens, rescaled per model by counted tokenizer ratios, priced by `llm/pricing.py` with the measured cache behaviour |
| Whether a route can run on today's harness | Yes | API documentation, read 2026-10-06 |
| Whether a route's prefixes still cache | Yes | Prefixes counted per model against each model's cache minimum |
| Latency per model | No | Needs calls; the source run's wall clock is reported as measured |
| Quality per model | No | Needs calls; the eval baseline exists for Sonnet 5 only |
| How long each model's reports are | No | Output is rescaled by the tokenizer ratio only, which assumes the same report in each model's tokens |

## Method

`scripts/project_model_routing.py --count` is the one network step, and it is free: the token-counting endpoint, 12 requests per model, 60 in all.
It saves every count, with the four-agent run's measured total, to `evaluations/routing/token-counts.json`, so the projection itself is offline and reproducible from committed files.

**The split by role.**
The Day 21 four-agent run recorded only the request's total.
Its two coordinator calls are rebuilt with the shipped code - `planner.plan_prompt` over the recorded situation and query, `synthesis.render_prompt` over the recorded responses - and counted; the plan the model returned is rebuilt from the response and counted as output.
The agents' share is the total less the coordinator's (`evals.routing.remainder`, which refuses a part larger than the whole).
Each coordinator call is a single forced call that writes its prefix to the cache and reads nothing back, which is how the cache counters split.
The rebuilt-and-counted coordinator plus the measured remainder prices back to the run's bill: $0.741 projected against $0.74 measured.

One number on that run is a stand-in: its model synthesis was rejected (the format slip Day 21 fixed by putting synthesis inside the retry loop) and what it wrote was never kept.
The deterministic synthesis that replaced it, 1,570 tokens, is counted in its place, and the report says so.

**Tokenizer ratios.**
The same texts counted on each model: every role's prefix, the rebuilt coordinator calls, the run's contexts (prompt), and its agent reports and the plan (output).
Haiku 4.5 counts 0.78 of Sonnet 5's prompt tokens and 0.74 of its output tokens.
The 5-series models share a tokenizer: plain text counts within 2 tokens of Sonnet 5 on every one of them, and a request that carries tools counts 66 to 68 fewer on every one - a per-model tool-use preamble rather than tokenization, which shows as a 0.99 ratio.

**Cache.**
A role keeps the cache share its run measured, rescaled.
A prefix that falls below the model's minimum cacheable length is named; every route also carries a ceiling with no cache at all.
On Haiku 4.5 (minimum 4096) every subagent prefix still caches (smallest: Docs, 4,682 tokens); the planner (3,330) and synthesis (1,419) prefixes do not.
Since a coordinator call only ever writes its prefix, not caching it is slightly cheaper than the projection says.

## What it says

On the four-agent run (Sonnet 5 for both roles, $0.74 measured):

- **The coordinator is 8% of the bill** ($0.058). Moving it to Opus 5, the plan's configuration, costs **+12%** ($0.83); to Opus 5.5, +8%.
- **The subagents are 92% of it.** Haiku 4.5 subagents take the run to **$0.32 with a Sonnet coordinator (-57%)** and $0.40 with the plan's Opus coordinator (-46%).
- On the eval set, Haiku 4.5 for the two forced-emit agents is **-63%** ($0.46 against $1.24).

So the plan's premise - a dearer coordinator paid for by cheaper subagents - holds on cost, and the coordinator's model is close to irrelevant to the bill.
The decision is entirely about whether Haiku 4.5 holds quality as a subagent, and that is the half that cannot be answered here.
What is known: on Day 4, Haiku broke the contract's timeline-ordering invariant on 2 of 3 attempts (`scripts/check_structured_output.py`), which is why the default became Sonnet; the Day 17 retry loop now answers exactly that kind of format refusal, and has never been measured on Haiku.

## What the harness cannot run yet

Claude Opus 5.5 and Claude Sonnet 5.5 refuse a forced `tool_choice` with a 400, on the token-counting endpoint too.
Every emit in this project is forced (`agents/_retry.py`, `coordinator/planner.py`), so a route naming either model is priced and marked not runnable.
Opus 5 accepts a forced tool, so it is the Opus the plan's routes use.
Moving to the current generation means steering the emit with `tool_choice: auto`, an instruction naming the tool, and `strict: true` on its schema - and re-measuring the retry loop's rates, because "forced" is what the format-versus-grounding split currently leans on.

## Measured: Haiku 4.5 as the subagent model (2026-10-07)

The owner approved one eval run: the whole set, realtime with caching, `AIOC_SUBAGENT_MODEL=claude-haiku-4-5`, read against the Sonnet 5 baseline run of the same configuration with `scripts/eval_baseline.py --against evaluations/baseline.json <run> --across-models` (`evaluations/routing/haiku-vs-sonnet.md`).
$0.60 in all: a 4-call smoke test ($0.06) and the run ($0.54).

| | Sonnet 5 | Haiku 4.5 |
|---|---|---|
| Answered | 38/38 | 36/38 |
| Failure mode correct | 16/18 | 16/18 |
| Severity exact / within one | 10/18 / 18/18 | 9/18 / 18/18 |
| Recall cites its post-mortem | 18/18 | 18/18 |
| No-precedent probes declined | 2/2 | 0 answered: both failed |
| Ungrounded statements | 1/271 | 5/299 |
| Retry calls / retries exhausted | 8 / 0 | 14 / 2 |
| Paid | $1.24 | $0.54 (-56%) |
| Wall clock | 773 s | 708 s |

**Haiku does the job where there is an answer and fails where there is none.**
Diagnosis and recall hold: the same failure-mode accuracy, the same recall, one severity fewer exact.
The two items it lost are the two no-precedent probes, the questions whose honest answer is that the corpus has nothing.
Both times the Docs agent's report was refused by the contract, three attempts each with the error fed back: in one, two unanswered sub-questions with no gap reporting them; in the other, a null answer with no gap against it.
That is the null-plus-gap rule, the behaviour this project exists to demonstrate, and the retry loop could not teach it to Haiku.
In a coordinator run, a refused Docs report becomes a `resolvable: false` gap rather than a wrong answer, so the failure is honest - but the abstention it should have produced is lost.
Ungrounded statements in diagnoses rose from 1 to 5 (2% of checkable statements).

**The projection was 17% low.** $0.46 projected, $0.54 paid: Haiku made 14 retry calls to Sonnet's 8, and wrote 17% more output than the tokenizer ratio predicted (84k against 72k).
The method's stated weakness - that verbosity per model is not measured - is the larger of the two errors, and retries are a cost a token ratio cannot see.

**What it means for routing.** Haiku for every subagent is not a drop-in: it would save about half of the subagents' bill and lose the abstention path on the Docs agent.
The Incident agent's results on Haiku are as good as Sonnet's on this set, so per-agent routing - Haiku for Incident, Sonnet for Docs - is the cheaper configuration this run supports; it needs a per-agent model setting, which the role settings do not provide.
One run per model is a smoke test of quality, not a rate: two items is the whole difference, and Sonnet's own runs disagreed on three items across three configurations on Day 20.
