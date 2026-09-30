# Eval run: 20260930T173419Z__llm__evals-realtime

- Set: `seeded-incidents` v1, 38 items (sha256 `1dafa5a554b7`).
- Model: `claude-sonnet-5`.
- Mode: realtime, prompt caching on (5m TTL).
- Wall clock: 773s.
- Answered: 38/38 = 100%.

## Accuracy

| Measure | Result |
|---|---|
| Failure mode matches the recorded truth | 16/18 = 89% |
| Failure mode abstentions (null value, counted as not correct) | 0 |
| Severity matches exactly | 10/18 = 56% |
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
| `code_regression` | 3/4 = 75% |
| `downstream_latency` | 4/4 = 100% |
| `other` | 1/2 = 50% |
| `resource_exhaustion` | 4/4 = 100% |

## Hallucination

| Measure | Result |
|---|---|
| Diagnosis statements with nothing behind them in the context | 1/271 = 0% |
| Diagnoses carrying at least one | 1/18 = 6% |
| Evidence joined from verbatim lines (counted here, not above) | 6/271 = 2% |
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
| `two_sources` | 0.90 | 2 | 2 | 100% | 0.91 |
| `single_source` | 0.70 | 15 | 13 | 87% | 0.79 |
| `inferred` | 0.50 | 17 | 9 | 53% | 0.59 |
| `hypothesis` | 0.25 | 2 | 2 | 100% | 0.40 |
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
| Input, all of it | 378,970 |
| of which read from the cache | 279,621 |
| of which written to the cache | 20,493 |
| of which billed at the input rate | 78,856 |
| Output | 97,094 |

| The same tokens, priced | USD | Against realtime, uncached |
|---|---|---|
| Realtime, no cache | $1.7289 | baseline |
| This mode, no cache | $1.7289 | +0% |
| As run | $1.2358 | -29% |
| As run, per item | $0.0325 | |

Cache: healthy - every agent's later requests read the prefix the first one wrote.

## Items

| Item | Truth | Answer | Confidence | Correct | Ungrounded | Attempts | Input | Output |
|---|---|---|---|---|---|---|---|---|
| `case_01:diagnose` | resource_exhaustion | resource_exhaustion | 0.85 | yes | 0 | 1 | 7,914 | 3,199 |
| `case_01:recall` | doc_0001 | doc_0001 | 0.88 | yes | 0 | 1 | 7,972 | 2,009 |
| `case_02:diagnose` | resource_exhaustion | resource_exhaustion | 0.65 | yes | 0 | 2 | 18,971 | 6,120 |
| `case_02:recall` | doc_0003 | doc_0003, doc_0012, doc_0007, doc_0014 | 0.85 | yes | 0 | 1 | 7,917 | 2,070 |
| `case_03:diagnose` | resource_exhaustion | resource_exhaustion | 0.90 | yes | 1 | 1 | 7,873 | 2,257 |
| `case_03:recall` | doc_0006 | doc_0006, doc_0003 | 0.88 | yes | 0 | 1 | 8,014 | 1,127 |
| `case_04:diagnose` | resource_exhaustion | resource_exhaustion | 0.85 | yes | 0 | 1 | 7,802 | 2,750 |
| `case_04:recall` | doc_0010 | doc_0010, doc_0002, doc_0011, doc_0014, doc_0017 | 0.78 | yes | 0 | 1 | 7,993 | 2,075 |
| `case_05:diagnose` | code_regression | code_regression | 0.85 | yes | 0 | 1 | 7,907 | 3,116 |
| `case_05:recall` | doc_0002 | doc_0002, doc_0011 | 0.85 | yes | 0 | 1 | 7,861 | 2,262 |
| `case_06:diagnose` | code_regression | bad_config_deploy | 0.55 | no | 0 | 1 | 7,837 | 2,776 |
| `case_06:recall` | doc_0007 | doc_0007, doc_0017, doc_0014 | 0.75 | yes | 0 | 1 | 7,844 | 1,839 |
| `case_07:diagnose` | code_regression | code_regression | 0.55 | yes | 0 | 1 | 7,897 | 3,132 |
| `case_07:recall` | doc_0012 | doc_0012 | 0.82 | yes | 0 | 2 | 18,092 | 4,079 |
| `case_08:diagnose` | code_regression | code_regression | 0.40 | yes | 0 | 1 | 7,760 | 2,158 |
| `case_08:recall` | doc_0016 | doc_0016, doc_0002 | 0.85 | yes | 0 | 2 | 17,232 | 2,624 |
| `case_09:diagnose` | bad_config_deploy | bad_config_deploy | 0.92 | yes | 0 | 1 | 7,900 | 3,207 |
| `case_09:recall` | doc_0004 | doc_0004, doc_0009, doc_0013 | 0.82 | yes | 0 | 1 | 7,954 | 1,428 |
| `case_10:diagnose` | bad_config_deploy | bad_config_deploy | 0.80 | yes | 0 | 1 | 7,880 | 3,348 |
| `case_10:recall` | doc_0008 | doc_0008 | 0.85 | yes | 0 | 1 | 7,935 | 1,076 |
| `case_11:diagnose` | bad_config_deploy | bad_config_deploy | 0.85 | yes | 0 | 1 | 7,852 | 2,673 |
| `case_11:recall` | doc_0011 | doc_0011, doc_0002 | 0.85 | yes | 0 | 1 | 7,928 | 1,154 |
| `case_12:diagnose` | bad_config_deploy | bad_config_deploy | 0.55 | yes | 0 | 1 | 7,825 | 2,472 |
| `case_12:recall` | doc_0015 | doc_0015, doc_0017, doc_0003, doc_0007, doc_0012 | 0.85 | yes | 0 | 1 | 7,855 | 1,923 |
| `case_13:diagnose` | downstream_latency | downstream_latency | 0.80 | yes | 0 | 1 | 7,860 | 2,696 |
| `case_13:recall` | doc_0005 | doc_0005, doc_0009 | 0.85 | yes | 0 | 1 | 7,951 | 2,305 |
| `case_14:diagnose` | downstream_latency | downstream_latency | 0.80 | yes | 0 | 1 | 7,897 | 2,582 |
| `case_14:recall` | doc_0009 | doc_0009, doc_0004, doc_0013, doc_0005 | 0.85 | yes | 0 | 1 | 7,929 | 1,815 |
| `case_15:diagnose` | downstream_latency | downstream_latency | 0.60 | yes | 0 | 1 | 7,824 | 2,887 |
| `case_15:recall` | doc_0014 | doc_0014, doc_0005, doc_0017, doc_0009 | 0.83 | yes | 0 | 1 | 7,922 | 2,134 |
| `case_16:diagnose` | downstream_latency | downstream_latency | 0.60 | yes | 0 | 2 | 18,866 | 6,100 |
| `case_16:recall` | doc_0017 | doc_0017 | 0.85 | yes | 0 | 1 | 7,889 | 920 |
| `case_17:diagnose` | other | other | 0.40 | yes | 0 | 1 | 7,774 | 2,277 |
| `case_17:recall` | doc_0013 | doc_0013, doc_0009 | 0.85 | yes | 0 | 1 | 7,974 | 1,196 |
| `case_18:diagnose` | other | resource_exhaustion | 0.55 | no | 0 | 1 | 7,860 | 2,620 |
| `case_18:recall` | doc_0018 | doc_0018 | 0.85 | yes | 0 | 2 | 17,490 | 3,015 |
| `case_19:recall` | no precedent | doc_0006, doc_0015, doc_0017, doc_0013 | 0.15 | yes | 0 | 3 | 26,566 | 3,323 |
| `case_20:recall` | no precedent | nothing cited | 0.15 | yes | 0 | 2 | 17,153 | 2,350 |

## What was refused or ungrounded

- `case_03:diagnose`: affected service 'service (unnamed, 2 additional dependents)' is not in the context
