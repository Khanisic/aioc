# Eval baseline (Day 20)

- Set: `seeded-incidents` v1, 38 items (sha256 `1dafa5a554b7`).
- Model: `claude-sonnet-5`.
- Runs: 3.
  - realtime, uncached: `20260930T173417Z__llm__evals-realtime`
  - realtime, cached: `20260930T173419Z__llm__evals-realtime`
  - batch, cached: `20260930T151610Z__llm__evals-batch`

## Scores

| | realtime, uncached | realtime, cached | batch, cached |
|---|---|---|---|
| Answered | 37/38 = 97% | 38/38 = 100% | 38/38 = 100% |
| Failure mode correct | 15/18 = 83% | 16/18 = 89% | 17/18 = 94% |
| Severity exact | 10/18 = 56% | 10/18 = 56% | 12/18 = 67% |
| Severity within one level | 18/18 = 100% | 18/18 = 100% | 18/18 = 100% |
| Recall cites the post-mortem | 18/18 = 100% | 18/18 = 100% | 18/18 = 100% |
| Retrieval returned it | 18/18 = 100% | 18/18 = 100% | 18/18 = 100% |
| Probes declined | 1/1 = 100% | 2/2 = 100% | 2/2 = 100% |
| Ungrounded statements | 2/270 = 1% | 1/271 = 0% | 1/291 = 0% |
| Evidence joined from verbatim lines | 5/270 = 2% | 6/271 = 2% | 5/291 = 2% |
| Invented precedents | 0/1 = 0% | 0/2 = 0% | 0/2 = 0% |
| Grounding refusals | 0/38 = 0% | 0/38 = 0% | 0/38 = 0% |
| Tool calls ok | 19/19 = 100% | 20/20 = 100% | 20/20 = 100% |
| Failure mode abstentions | 0 | 0 | 0 |
| Retry calls | 9 | 8 | 8 |

## Cost

| | realtime, uncached | realtime, cached | batch, cached |
|---|---|---|---|
| Input tokens | 389,666 | 378,970 | 383,829 |
| of which read from the cache | 0 | 279,621 | 269,046 |
| of which written to the cache | 0 | 20,493 | 32,391 |
| Output tokens | 99,020 | 97,094 | 106,482 |
| Paid | $1.7695 | $1.2358 | $0.7065 |
| The same tokens, realtime and uncached | $1.7695 | $1.7289 | $1.8325 |
| What the levers changed, same tokens | +0% | -29% | -61% |
| Paid, against the reference run | +0% | -30% | -60% |
| Wall clock | 801s | 773s | 8218s |
| Batches | 0 | 0 | 3 |

The same-tokens row prices each run's own tokens both ways, so it is the lever and nothing else.
The reference row compares two different runs, so it carries the difference in what the model happened to write as well.
Cache, realtime, uncached: prompt caching was off for this run.
Cache, realtime, cached: every agent's later requests read the prefix the first one wrote.
Cache, batch, cached: a batch's requests run concurrently, so reads are best-effort: 269,046 tokens were read and 32,391 written.

## Agreement between the runs

35 of 38 items were scored the same in every run.
A lever changes how a request is billed and not what it says, so these are the model answering the same request differently.

| Item | realtime, uncached | realtime, cached | batch, cached |
|---|---|---|---|
| `case_06:diagnose` | no | no | yes |
| `case_15:diagnose` | no | yes | yes |
| `case_19:recall` | failed | yes | yes |

## Calibration

| Band | realtime, uncached | realtime, cached | batch, cached |
|---|---|---|---|
| `two_sources` | 3/3 = 100% | 2/2 = 100% | 3/3 = 100% |
| `single_source` | 12/14 = 86% | 13/15 = 87% | 13/14 = 93% |
| `inferred` | 8/16 = 50% | 9/17 = 53% | 10/15 = 67% |
| `hypothesis` | 2/3 = 67% | 2/2 = 100% | 3/4 = 75% |
| `speculation` | none | none | none |
