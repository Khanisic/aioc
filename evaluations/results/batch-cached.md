# Eval run: 20260930T151610Z__llm__evals-batch

- Set: `seeded-incidents` v1, 38 items (sha256 `1dafa5a554b7`).
- Model: `claude-sonnet-5`.
- Mode: batch, prompt caching on (1h TTL).
- Wall clock: 8217s.
- Answered: 38/38 = 100%.

## Accuracy

| Measure | Result |
|---|---|
| Failure mode matches the recorded truth | 17/18 = 94% |
| Failure mode abstentions (null value, counted as not correct) | 0 |
| Severity matches exactly | 12/18 = 67% |
| Severity within one level | 18/18 = 100% |
| Affected services, mean precision | 98% |
| Affected services, mean recall | 84% |
| Recall cites the incident's own post-mortem | 18/18 = 100% |
| Retrieval returned that post-mortem | 18/18 = 100% |
| No-precedent probes answered with no answer | 2/2 = 100% |

### Failure mode, by recorded truth

| Truth | Correct |
|---|---|
| `bad_config_deploy` | 4/4 = 100% |
| `code_regression` | 4/4 = 100% |
| `downstream_latency` | 4/4 = 100% |
| `other` | 1/2 = 50% |
| `resource_exhaustion` | 4/4 = 100% |

## Hallucination

| Measure | Result |
|---|---|
| Diagnosis statements with nothing behind them in the context | 1/291 = 0% |
| Diagnoses carrying at least one | 1/18 = 6% |
| Evidence joined from verbatim lines (counted here, not above) | 5/291 = 2% |
| Probes answered with an invented precedent | 0/2 = 0% |
| Reports the agents' own grounding rule refused | 0/38 = 0% |

## Tool success

| Measure | Result |
|---|---|
| Tool calls that returned ok | 20/20 = 100% |

## Calibration

Judgements that stated a value, in the contract band their confidence names.

| Band | From | Judgements | Correct | Accuracy | Mean confidence |
|---|---|---|---|---|---|
| `two_sources` | 0.90 | 3 | 3 | 100% | 0.91 |
| `single_source` | 0.70 | 14 | 13 | 93% | 0.80 |
| `inferred` | 0.50 | 15 | 10 | 67% | 0.59 |
| `hypothesis` | 0.25 | 4 | 3 | 75% | 0.43 |
| `speculation` | 0.00 | 0 | 0 | n/a | n/a |

## Validation retries

| Measure | Count |
|---|---|
| Reports accepted first try | 31 |
| Recovered by a retry | 7 |
| Exhausted | 0 |
| Retry calls made | 8 |

## Cost

| Tokens | Count |
|---|---|
| Input, all of it | 383,829 |
| of which read from the cache | 269,046 |
| of which written to the cache | 32,391 |
| of which billed at the input rate | 82,392 |
| Output | 106,482 |

| The same tokens, priced | USD | Against realtime, uncached |
|---|---|---|
| Realtime, no cache | $1.8325 | baseline |
| This mode, no cache | $0.9162 | -50% |
| As run | $0.7065 | -61% |
| As run, per item | $0.0186 | |

Cache: not assessed - a batch's requests run concurrently, so reads are best-effort: 269,046 tokens were read and 32,391 written.

| Batch | Requests | Succeeded | Failed | Seconds |
|---|---|---|---|---|
| `msgbatch_01H3EzCXpAAaKUHuaKJ2N2dP` | 38 | 38 | 0 | 1 |
| `msgbatch_01V4aMKXL8gN9W8dhH6zPZjH` | 9 | 9 | 0 | 5497 |
| `msgbatch_01PXadmfrfJgMcF9NJdYHjZZ` | 1 | 1 | 0 | 2715 |

Retrieval ran degraded:
- query embedding failed (Voyage embeddings request failed: Client error '429 Too Many Requests' for url 'https://api.voyageai.com/v1/embeddings'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429); lexical only

## Items

| Item | Truth | Answer | Confidence | Correct | Ungrounded | Attempts | Input | Output |
|---|---|---|---|---|---|---|---|---|
| `case_01:diagnose` | resource_exhaustion | resource_exhaustion | 0.90 | yes | 0 | 1 | 7,914 | 3,361 |
| `case_01:recall` | doc_0001 | doc_0001, doc_0009, doc_0004, doc_0014, doc_0005 | 0.88 | yes | 0 | 1 | 7,972 | 2,316 |
| `case_02:diagnose` | resource_exhaustion | resource_exhaustion | 0.70 | yes | 0 | 1 | 7,876 | 2,780 |
| `case_02:recall` | doc_0003 | doc_0003, doc_0014 | 0.85 | yes | 0 | 1 | 7,917 | 2,064 |
| `case_03:diagnose` | resource_exhaustion | resource_exhaustion | 0.90 | yes | 1 | 1 | 7,873 | 3,152 |
| `case_03:recall` | doc_0006 | doc_0006, doc_0003 | 0.88 | yes | 0 | 1 | 7,871 | 2,304 |
| `case_04:diagnose` | resource_exhaustion | resource_exhaustion | 0.85 | yes | 0 | 1 | 7,802 | 2,885 |
| `case_04:recall` | doc_0010 | doc_0010 | 0.85 | yes | 0 | 1 | 7,993 | 1,336 |
| `case_05:diagnose` | code_regression | code_regression | 0.85 | yes | 0 | 2 | 19,090 | 6,409 |
| `case_05:recall` | doc_0002 | doc_0002, doc_0007 | 0.85 | yes | 0 | 1 | 8,032 | 1,863 |
| `case_06:diagnose` | code_regression | code_regression | 0.55 | yes | 0 | 1 | 7,837 | 2,745 |
| `case_06:recall` | doc_0007 | doc_0007 | 0.80 | yes | 0 | 1 | 7,987 | 1,743 |
| `case_07:diagnose` | code_regression | code_regression | 0.55 | yes | 0 | 1 | 7,897 | 3,467 |
| `case_07:recall` | doc_0012 | doc_0012, doc_0003, doc_0014 | 0.80 | yes | 0 | 2 | 16,297 | 2,204 |
| `case_08:diagnose` | code_regression | code_regression | 0.40 | yes | 0 | 1 | 7,760 | 1,962 |
| `case_08:recall` | doc_0016 | doc_0016 | 0.82 | yes | 0 | 1 | 7,989 | 1,642 |
| `case_09:diagnose` | bad_config_deploy | bad_config_deploy | 0.92 | yes | 0 | 1 | 7,900 | 2,568 |
| `case_09:recall` | doc_0004 | doc_0004, doc_0009, doc_0013 | 0.82 | yes | 0 | 1 | 8,063 | 2,222 |
| `case_10:diagnose` | bad_config_deploy | bad_config_deploy | 0.75 | yes | 0 | 1 | 7,880 | 3,267 |
| `case_10:recall` | doc_0008 | doc_0008 | 0.85 | yes | 0 | 1 | 7,999 | 1,662 |
| `case_11:diagnose` | bad_config_deploy | bad_config_deploy | 0.85 | yes | 0 | 1 | 7,852 | 2,989 |
| `case_11:recall` | doc_0011 | doc_0011 | 0.80 | yes | 0 | 1 | 8,008 | 1,979 |
| `case_12:diagnose` | bad_config_deploy | bad_config_deploy | 0.55 | yes | 0 | 1 | 7,825 | 2,765 |
| `case_12:recall` | doc_0015 | doc_0015, doc_0017, doc_0003 | 0.85 | yes | 0 | 1 | 7,975 | 1,899 |
| `case_13:diagnose` | downstream_latency | downstream_latency | 0.80 | yes | 0 | 1 | 7,860 | 3,031 |
| `case_13:recall` | doc_0005 | doc_0005, doc_0009 | 0.88 | yes | 0 | 1 | 8,024 | 2,262 |
| `case_14:diagnose` | downstream_latency | downstream_latency | 0.85 | yes | 0 | 2 | 18,622 | 5,323 |
| `case_14:recall` | doc_0009 | doc_0009 | 0.83 | yes | 0 | 2 | 18,050 | 3,593 |
| `case_15:diagnose` | downstream_latency | downstream_latency | 0.55 | yes | 0 | 1 | 7,824 | 2,935 |
| `case_15:recall` | doc_0014 | doc_0014, doc_0005, doc_0017 | 0.78 | yes | 0 | 1 | 8,027 | 2,045 |
| `case_16:diagnose` | downstream_latency | downstream_latency | 0.60 | yes | 0 | 2 | 19,034 | 6,476 |
| `case_16:recall` | doc_0017 | doc_0017, doc_0014 | 0.88 | yes | 0 | 1 | 7,948 | 2,079 |
| `case_17:diagnose` | other | other | 0.40 | yes | 0 | 1 | 7,774 | 3,303 |
| `case_17:recall` | doc_0013 | doc_0013 | 0.82 | yes | 0 | 1 | 8,039 | 987 |
| `case_18:diagnose` | other | resource_exhaustion | 0.45 | no | 0 | 1 | 7,860 | 2,444 |
| `case_18:recall` | doc_0018 | doc_0018 | 0.85 | yes | 0 | 1 | 7,967 | 1,549 |
| `case_19:recall` | no precedent | doc_0017, doc_0006, doc_0011, doc_0009, doc_0010 | 0.20 | yes | 0 | 3 | 28,892 | 4,386 |
| `case_20:recall` | no precedent | doc_0003, doc_0017, doc_0007, doc_0009, doc_0013 | 0.20 | yes | 0 | 2 | 18,299 | 4,485 |

## What was refused or ungrounded

- `case_03:diagnose`: affected service '(unnamed third service - not identified in context)' is not in the context
