# Eval run: 20261010T040702Z__llm__evals-batch

- Set: `seeded-incidents` v1, 38 items (sha256 `1dafa5a554b7`).
- Model: `claude-sonnet-5`.
- Mode: batch, prompt caching on (5m TTL).
- Wall clock: 5s.
- Answered: 38/38 = 100%.

## Accuracy

| Measure | Result |
|---|---|
| Failure mode matches the recorded truth | 16/18 = 89% |
| Failure mode abstentions (null value, counted as not correct) | 0 |
| Severity matches exactly | 10/18 = 56% |
| Severity within one level | 18/18 = 100% |
| Affected services, mean precision | 94% |
| Affected services, mean recall | 81% |
| Recall cites the incident's own post-mortem | 18/18 = 100% |
| Retrieval returned that post-mortem | 18/18 = 100% |
| No-precedent probes answered with no answer | 2/2 = 100% |

### Failure mode, by recorded truth

| Truth | Correct |
|---|---|
| `bad_config_deploy` | 4/4 = 100% |
| `code_regression` | 3/4 = 75% |
| `downstream_latency` | 4/4 = 100% |
| `other` | 1/2 = 50% |
| `resource_exhaustion` | 4/4 = 100% |

## Hallucination

| Measure | Result |
|---|---|
| Diagnosis statements with nothing behind them in the context | 4/298 = 1% |
| Diagnoses carrying at least one | 2/18 = 11% |
| Evidence joined from verbatim lines (counted here, not above) | 6/298 = 2% |
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
| `single_source` | 0.70 | 14 | 12 | 86% | 0.80 |
| `inferred` | 0.50 | 17 | 9 | 53% | 0.58 |
| `hypothesis` | 0.25 | 2 | 2 | 100% | 0.45 |
| `speculation` | 0.00 | 0 | 0 | n/a | n/a |

## Validation retries

| Measure | Count |
|---|---|
| Reports accepted first try | 28 |
| Recovered by a retry | 10 |
| Exhausted | 0 |
| Retry calls made | 10 |

## Cost

| Tokens | Count |
|---|---|
| Input, all of it | 406,387 |
| of which read from the cache | 178,488 |
| of which written to the cache | 138,816 |
| of which billed at the input rate | 89,083 |
| Output | 117,875 |

| The same tokens, priced | USD | Against realtime, uncached |
|---|---|---|
| Realtime, no cache | $1.9915 | baseline |
| This mode, no cache | $0.9958 | -50% |
| As run | $0.8698 | -56% |
| As run, per item | $0.0229 | |

Cache: not assessed - a batch's requests run concurrently, so reads are best-effort: 178,488 tokens were read and 138,816 written.

| Batch | Requests | Succeeded | Failed | Seconds |
|---|---|---|---|---|
| `msgbatch_01CuGsixLyyEL2nUfVLtZFEr` | 38 | 38 | 0 | 1 |
| `msgbatch_01TFPchc2MJNwdL16iJuVrEf` | 11 | 10 | 1 | 1 |
| `msgbatch_01KXWhvKbucuu1evwJAZBmbu` | 1 | 1 | 0 | 1 |
| `msgbatch_01PcX6XwPFPKFABCEkKNFYft` | 2 | 2 | 0 | 1 |
| `msgbatch_016oEJSz9wBRXUjyg5G2BhwD` | 1 | 1 | 0 | 1 |

## Items

| Item | Truth | Answer | Confidence | Correct | Ungrounded | Attempts | Input | Output |
|---|---|---|---|---|---|---|---|---|
| `case_01:diagnose` | resource_exhaustion | resource_exhaustion | 0.90 | yes | 0 | 2 | 19,331 | 6,826 |
| `case_01:recall` | doc_0001 | doc_0001 | 0.85 | yes | 0 | 1 | 7,972 | 1,597 |
| `case_02:diagnose` | resource_exhaustion | resource_exhaustion | 0.70 | yes | 0 | 2 | 18,734 | 5,450 |
| `case_02:recall` | doc_0003 | doc_0003, doc_0014 | 0.82 | yes | 0 | 2 | 17,990 | 3,953 |
| `case_03:diagnose` | resource_exhaustion | resource_exhaustion | 0.90 | yes | 2 | 1 | 7,873 | 3,265 |
| `case_03:recall` | doc_0006 | doc_0006, doc_0003 | 0.88 | yes | 0 | 2 | 18,067 | 4,318 |
| `case_04:diagnose` | resource_exhaustion | resource_exhaustion | 0.85 | yes | 0 | 1 | 7,802 | 2,840 |
| `case_04:recall` | doc_0010 | doc_0010 | 0.82 | yes | 0 | 1 | 7,993 | 1,652 |
| `case_05:diagnose` | code_regression | code_regression | 0.85 | yes | 0 | 1 | 7,907 | 3,174 |
| `case_05:recall` | doc_0002 | doc_0002, doc_0007 | 0.82 | yes | 0 | 1 | 8,032 | 2,336 |
| `case_06:diagnose` | code_regression | bad_config_deploy | 0.55 | no | 0 | 2 | 18,747 | 5,990 |
| `case_06:recall` | doc_0007 | doc_0007, doc_0017, doc_0014 | 0.78 | yes | 0 | 1 | 7,987 | 1,759 |
| `case_07:diagnose` | code_regression | code_regression | 0.60 | yes | 0 | 2 | 18,958 | 6,146 |
| `case_07:recall` | doc_0012 | doc_0012 | 0.85 | yes | 0 | 1 | 8,023 | 994 |
| `case_08:diagnose` | code_regression | code_regression | 0.45 | yes | 0 | 1 | 7,760 | 2,495 |
| `case_08:recall` | doc_0016 | doc_0016, doc_0002 | 0.85 | yes | 0 | 2 | 16,208 | 2,036 |
| `case_09:diagnose` | bad_config_deploy | bad_config_deploy | 0.93 | yes | 0 | 1 | 7,900 | 3,089 |
| `case_09:recall` | doc_0004 | doc_0004, doc_0009, doc_0013 | 0.75 | yes | 0 | 1 | 8,063 | 2,509 |
| `case_10:diagnose` | bad_config_deploy | bad_config_deploy | 0.75 | yes | 2 | 1 | 7,880 | 3,324 |
| `case_10:recall` | doc_0008 | doc_0008 | 0.85 | yes | 0 | 1 | 7,938 | 1,612 |
| `case_11:diagnose` | bad_config_deploy | bad_config_deploy | 0.85 | yes | 0 | 1 | 7,852 | 3,167 |
| `case_11:recall` | doc_0011 | doc_0011 | 0.85 | yes | 0 | 1 | 8,008 | 1,710 |
| `case_12:diagnose` | bad_config_deploy | bad_config_deploy | 0.55 | yes | 0 | 1 | 7,825 | 3,278 |
| `case_12:recall` | doc_0015 | doc_0015, doc_0017, doc_0003 | 0.82 | yes | 0 | 1 | 7,975 | 1,657 |
| `case_13:diagnose` | downstream_latency | downstream_latency | 0.75 | yes | 0 | 1 | 7,860 | 3,100 |
| `case_13:recall` | doc_0005 | doc_0005, doc_0009 | 0.85 | yes | 0 | 1 | 8,024 | 2,081 |
| `case_14:diagnose` | downstream_latency | downstream_latency | 0.85 | yes | 0 | 1 | 7,897 | 2,837 |
| `case_14:recall` | doc_0009 | doc_0009, doc_0004, doc_0013, doc_0005 | 0.85 | yes | 0 | 1 | 8,035 | 2,090 |
| `case_15:diagnose` | downstream_latency | downstream_latency | 0.55 | yes | 0 | 2 | 18,771 | 6,080 |
| `case_15:recall` | doc_0014 | doc_0014, doc_0005, doc_0017 | 0.78 | yes | 0 | 1 | 8,027 | 2,584 |
| `case_16:diagnose` | downstream_latency | downstream_latency | 0.55 | yes | 0 | 2 | 19,085 | 6,644 |
| `case_16:recall` | doc_0017 | doc_0017 | 0.85 | yes | 0 | 1 | 7,948 | 1,516 |
| `case_17:diagnose` | other | other | 0.55 | yes | 0 | 1 | 7,774 | 2,386 |
| `case_17:recall` | doc_0013 | doc_0013 | 0.80 | yes | 0 | 1 | 8,039 | 2,411 |
| `case_18:diagnose` | other | resource_exhaustion | 0.55 | no | 0 | 1 | 7,860 | 3,613 |
| `case_18:recall` | doc_0018 | doc_0018 | 0.85 | yes | 0 | 1 | 7,967 | 1,536 |
| `case_19:recall` | no precedent | doc_0017, doc_0006, doc_0011, doc_0009, doc_0010 | 0.30 | yes | 0 | 2 | 18,261 | 4,475 |
| `case_20:recall` | no precedent | nothing cited | 0.12 | yes | 0 | 1 | 8,014 | 1,345 |

## What was refused or ungrounded

- `case_03:diagnose`: affected service 'service-2-unnamed' is not in the context
- `case_03:diagnose`: affected service 'service-3-unnamed' is not in the context
- `case_10:diagnose`: affected service 'session-service' is not in the context
- `case_10:diagnose`: affected service 'idempotency-service' is not in the context
