# Eval run: 20260930T173417Z__llm__evals-realtime

- Set: `seeded-incidents` v1, 38 items (sha256 `1dafa5a554b7`).
- Model: `claude-sonnet-5`.
- Mode: realtime, prompt caching off.
- Wall clock: 801s.
- Answered: 37/38 = 97%.

## Accuracy

| Measure | Result |
|---|---|
| Failure mode matches the recorded truth | 15/18 = 83% |
| Failure mode abstentions (null value, counted as not correct) | 0 |
| Severity matches exactly | 10/18 = 56% |
| Severity within one level | 18/18 = 100% |
| Affected services, mean precision | 97% |
| Affected services, mean recall | 84% |
| Recall cites the incident's own post-mortem | 18/18 = 100% |
| Retrieval returned that post-mortem | 18/18 = 100% |
| No-precedent probes answered with no answer | 1/1 = 100% |

### Failure mode, by recorded truth

| Truth | Correct |
|---|---|
| `bad_config_deploy` | 4/4 = 100% |
| `code_regression` | 3/4 = 75% |
| `downstream_latency` | 3/4 = 75% |
| `other` | 1/2 = 50% |
| `resource_exhaustion` | 4/4 = 100% |

## Hallucination

| Measure | Result |
|---|---|
| Diagnosis statements with nothing behind them in the context | 2/270 = 1% |
| Diagnoses carrying at least one | 1/18 = 6% |
| Evidence joined from verbatim lines (counted here, not above) | 5/270 = 2% |
| Probes answered with an invented precedent | 0/1 = 0% |
| Reports the agents' own grounding rule refused | 0/38 = 0% |

## Tool success

| Measure | Result |
|---|---|
| Tool calls that returned ok | 19/19 = 100% |

## Calibration

Judgements that stated a value, in the contract band their confidence names.

| Band | From | Judgements | Correct | Accuracy | Mean confidence |
|---|---|---|---|---|---|
| `two_sources` | 0.90 | 3 | 3 | 100% | 0.91 |
| `single_source` | 0.70 | 14 | 12 | 86% | 0.78 |
| `inferred` | 0.50 | 16 | 8 | 50% | 0.58 |
| `hypothesis` | 0.25 | 3 | 2 | 67% | 0.40 |
| `speculation` | 0.00 | 0 | 0 | n/a | n/a |

## Validation retries

| Measure | Count |
|---|---|
| Reports accepted first try | 30 |
| Recovered by a retry | 7 |
| Exhausted | 1 |
| Retry calls made | 9 |

## Cost

| Tokens | Count |
|---|---|
| Input, all of it | 389,666 |
| of which read from the cache | 0 |
| of which written to the cache | 0 |
| of which billed at the input rate | 389,666 |
| Output | 99,020 |

| The same tokens, priced | USD | Against realtime, uncached |
|---|---|---|
| Realtime, no cache | $1.7695 | baseline |
| This mode, no cache | $1.7695 | +0% |
| As run | $1.7695 | +0% |
| As run, per item | $0.0466 | |

Cache: not assessed - prompt caching was off for this run.

## Items

| Item | Truth | Answer | Confidence | Correct | Ungrounded | Attempts | Input | Output |
|---|---|---|---|---|---|---|---|---|
| `case_01:diagnose` | resource_exhaustion | resource_exhaustion | 0.90 | yes | 0 | 1 | 7,914 | 3,549 |
| `case_01:recall` | doc_0001 | doc_0001 | 0.88 | yes | 0 | 1 | 7,972 | 959 |
| `case_02:diagnose` | resource_exhaustion | resource_exhaustion | 0.65 | yes | 0 | 1 | 7,876 | 3,026 |
| `case_02:recall` | doc_0003 | doc_0003, doc_0012, doc_0014 | 0.85 | yes | 0 | 1 | 7,917 | 1,653 |
| `case_03:diagnose` | resource_exhaustion | resource_exhaustion | 0.90 | yes | 2 | 1 | 7,873 | 2,731 |
| `case_03:recall` | doc_0006 | doc_0006, doc_0003 | 0.88 | yes | 0 | 2 | 18,137 | 4,358 |
| `case_04:diagnose` | resource_exhaustion | resource_exhaustion | 0.85 | yes | 0 | 1 | 7,802 | 2,927 |
| `case_04:recall` | doc_0010 | doc_0010 | 0.85 | yes | 0 | 1 | 7,915 | 1,725 |
| `case_05:diagnose` | code_regression | code_regression | 0.85 | yes | 0 | 1 | 7,907 | 2,688 |
| `case_05:recall` | doc_0002 | doc_0002, doc_0011 | 0.80 | yes | 0 | 1 | 7,861 | 1,377 |
| `case_06:diagnose` | code_regression | bad_config_deploy | 0.55 | no | 0 | 2 | 18,289 | 4,875 |
| `case_06:recall` | doc_0007 | doc_0007, doc_0017, doc_0016 | 0.82 | yes | 0 | 2 | 17,656 | 3,557 |
| `case_07:diagnose` | code_regression | code_regression | 0.60 | yes | 0 | 2 | 18,937 | 5,824 |
| `case_07:recall` | doc_0012 | doc_0012, doc_0003 | 0.83 | yes | 0 | 1 | 7,917 | 1,071 |
| `case_08:diagnose` | code_regression | code_regression | 0.40 | yes | 0 | 1 | 7,760 | 2,736 |
| `case_08:recall` | doc_0016 | doc_0016, doc_0002 | 0.87 | yes | 0 | 1 | 7,888 | 1,234 |
| `case_09:diagnose` | bad_config_deploy | bad_config_deploy | 0.92 | yes | 0 | 1 | 7,900 | 2,821 |
| `case_09:recall` | doc_0004 | doc_0004, doc_0009 | 0.82 | yes | 0 | 1 | 7,954 | 1,219 |
| `case_10:diagnose` | bad_config_deploy | bad_config_deploy | 0.82 | yes | 0 | 1 | 7,880 | 3,080 |
| `case_10:recall` | doc_0008 | doc_0008 | 0.85 | yes | 0 | 1 | 7,935 | 1,020 |
| `case_11:diagnose` | bad_config_deploy | bad_config_deploy | 0.85 | yes | 0 | 1 | 7,852 | 2,696 |
| `case_11:recall` | doc_0011 | doc_0011 | 0.85 | yes | 0 | 2 | 17,604 | 3,139 |
| `case_12:diagnose` | bad_config_deploy | bad_config_deploy | 0.55 | yes | 0 | 1 | 7,825 | 2,913 |
| `case_12:recall` | doc_0015 | doc_0015 | 0.85 | yes | 0 | 1 | 7,855 | 1,000 |
| `case_13:diagnose` | downstream_latency | downstream_latency | 0.75 | yes | 0 | 1 | 7,860 | 2,971 |
| `case_13:recall` | doc_0005 | doc_0005, doc_0009 | 0.85 | yes | 0 | 1 | 7,951 | 2,161 |
| `case_14:diagnose` | downstream_latency | downstream_latency | 0.80 | yes | 0 | 1 | 7,897 | 2,286 |
| `case_14:recall` | doc_0009 | doc_0009 | 0.85 | yes | 0 | 2 | 17,470 | 2,833 |
| `case_15:diagnose` | downstream_latency | resource_exhaustion | 0.55 | no | 0 | 1 | 7,824 | 2,392 |
| `case_15:recall` | doc_0014 | doc_0014, doc_0005, doc_0017, doc_0009 | 0.85 | yes | 0 | 1 | 7,922 | 2,174 |
| `case_16:diagnose` | downstream_latency | downstream_latency | 0.55 | yes | 0 | 2 | 18,781 | 5,986 |
| `case_16:recall` | doc_0017 | doc_0017 | 0.85 | yes | 0 | 1 | 7,889 | 881 |
| `case_17:diagnose` | other | other | 0.40 | yes | 0 | 1 | 7,774 | 2,936 |
| `case_17:recall` | doc_0013 | doc_0013 | 0.85 | yes | 0 | 1 | 7,974 | 1,635 |
| `case_18:diagnose` | other | resource_exhaustion | 0.55 | no | 0 | 1 | 7,860 | 2,460 |
| `case_18:recall` | doc_0018 | doc_0018 | 0.85 | yes | 0 | 1 | 7,906 | 1,478 |
| `case_19:recall` | - | no response | - | failed | 0 | 3 | 26,222 | 5,550 |
| `case_20:recall` | no precedent | nothing cited | 0.10 | yes | 0 | 1 | 7,910 | 1,099 |

## What was refused or ungrounded

- `case_03:diagnose`: affected service 'service_2_unnamed' is not in the context
- `case_03:diagnose`: affected service 'service_3_unnamed' is not in the context
- `case_19:recall` produced no response: ValidationError: 1 validation error for DocsAgentResponse Value error, null Assessment at findings.answer requires a Gap whose blocks_field references it (CONTRACTS.md sec 2.1) [type=value_error, input_value={'request_id': 'req_case_...=datetime.timezone.utc)}, input_type=dict] For further information visit https://errors.pydantic.dev/2.13/v/value_error | validation-retry loop: 3 attempt(s), rejected 3 time(s) [format, format, format]; the retry cap of 2 was reached
