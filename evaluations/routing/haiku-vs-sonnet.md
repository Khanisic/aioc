# Haiku 4.5 subagents against the Sonnet 5 baseline

- Configuration: realtime, cached.
- Baseline run: `20260930T173419Z__llm__evals-realtime` on `claude-sonnet-5`.
- This run: `20261007T004504Z__llm__evals-realtime` on `claude-haiku-4-5`.
- **Two models.** Token and cost changes carry each model's tokenizer and prices as well as what it wrote; the scores are the comparison that matters.

## Tokens and cost

| | Baseline | This run | Change |
|---|---|---|---|
| Input tokens | 378,970 | 340,635 | -10% |
| Output tokens | 97,094 | 84,058 | -13% |
| Paid | $1.2358 | $0.5401 | -56% |
| Wall clock | 773s | 708s | -8% |
| Accepted first try | 31 | 27 | |
| Recovered by a retry | 7 | 9 | |
| Retries exhausted | 0 | 2 | |
| Retry calls | 8 | 14 | |

## Scores

| | Baseline | This run | Change |
|---|---|---|---|
| Answered | 38/38 = 100% | 36/38 = 95% | -5 pts |
| Failure mode correct | 16/18 = 89% | 16/18 = 89% | +0 pts |
| Severity exact | 10/18 = 56% | 9/18 = 50% | -6 pts |
| Severity within one level | 18/18 = 100% | 18/18 = 100% | +0 pts |
| Recall cites the post-mortem | 18/18 = 100% | 18/18 = 100% | +0 pts |
| Retrieval returned it | 18/18 = 100% | 18/18 = 100% | +0 pts |
| Probes declined | 2/2 = 100% | n/a (0) | n/a |
| Ungrounded statements | 1/271 = 0% | 5/299 = 2% | +1 pts |
| Evidence joined from verbatim lines | 6/271 = 2% | 1/299 = 0% | -2 pts |
| Invented precedents | 0/2 = 0% | n/a (0) | n/a |
| Grounding refusals | 0/38 = 0% | 1/38 = 3% | +3 pts |
| Tool calls ok | 20/20 = 100% | 18/18 = 100% | +0 pts |

## Items that changed

| Item | Baseline | This run |
|---|---|---|
| `case_06:diagnose` | no | yes |
| `case_16:diagnose` | yes | no |
| `case_19:recall` | yes | failed |
| `case_20:recall` | yes | failed |
