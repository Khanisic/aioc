# Day 24 re-run against the Day 20 baseline

- Configuration: batch, cached.
- Baseline run: `20260930T151610Z__llm__evals-batch`.
- This run: `20261010T040702Z__llm__evals-batch`.

## Tokens and cost

| | Baseline | This run | Change |
|---|---|---|---|
| Input tokens | 383,829 | 406,387 | +6% |
| Output tokens | 106,482 | 117,875 | +11% |
| Paid | $0.7065 | $0.8698 | +23% |
| Wall clock | 8218s | 5s | -100% |
| Accepted first try | 31 | 28 | |
| Recovered by a retry | 7 | 10 | |
| Retries exhausted | 0 | 0 | |
| Retry calls | 8 | 10 | |

## Scores

| | Baseline | This run | Change |
|---|---|---|---|
| Answered | 38/38 = 100% | 38/38 = 100% | +0 pts |
| Failure mode correct | 17/18 = 94% | 16/18 = 89% | -6 pts |
| Severity exact | 12/18 = 67% | 10/18 = 56% | -11 pts |
| Severity within one level | 18/18 = 100% | 18/18 = 100% | +0 pts |
| Recall cites the post-mortem | 18/18 = 100% | 18/18 = 100% | +0 pts |
| Retrieval returned it | 18/18 = 100% | 18/18 = 100% | +0 pts |
| Probes declined | 2/2 = 100% | 2/2 = 100% | +0 pts |
| Ungrounded statements | 1/291 = 0% | 4/298 = 1% | +1 pts |
| Evidence joined from verbatim lines | 5/291 = 2% | 6/298 = 2% | +0 pts |
| Invented precedents | 0/2 = 0% | 0/2 = 0% | +0 pts |
| Grounding refusals | 0/38 = 0% | 0/38 = 0% | +0 pts |
| Tool calls ok | 20/20 = 100% | 20/20 = 100% | +0 pts |

## Items that changed

| Item | Baseline | This run |
|---|---|---|
| `case_06:diagnose` | yes | no |
