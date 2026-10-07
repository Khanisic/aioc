# Eval run: 20261007T004504Z__llm__evals-realtime

- Set: `seeded-incidents` v1, 38 items (sha256 `1dafa5a554b7`).
- Model: `claude-haiku-4-5`.
- Mode: realtime, prompt caching on (5m TTL).
- Wall clock: 708s.
- Answered: 36/38 = 95%.

## Accuracy

| Measure | Result |
|---|---|
| Failure mode matches the recorded truth | 16/18 = 89% |
| Failure mode abstentions (null value, counted as not correct) | 0 |
| Severity matches exactly | 9/18 = 50% |
| Severity within one level | 18/18 = 100% |
| Affected services, mean precision | 94% |
| Affected services, mean recall | 76% |
| Recall cites the incident's own post-mortem | 18/18 = 100% |
| Retrieval returned that post-mortem | 18/18 = 100% |
| No-precedent probes answered with no answer | n/a (0) |

### Failure mode, by recorded truth

| Truth | Correct |
|---|---|
| `bad_config_deploy` | 4/4 = 100% |
| `code_regression` | 4/4 = 100% |
| `downstream_latency` | 3/4 = 75% |
| `other` | 1/2 = 50% |
| `resource_exhaustion` | 4/4 = 100% |

## Hallucination

| Measure | Result |
|---|---|
| Diagnosis statements with nothing behind them in the context | 5/299 = 2% |
| Diagnoses carrying at least one | 4/18 = 22% |
| Evidence joined from verbatim lines (counted here, not above) | 1/299 = 0% |
| Probes answered with an invented precedent | n/a (0) |
| Reports the agents' own grounding rule refused | 1/38 = 3% |

## Tool success

| Measure | Result |
|---|---|
| Tool calls that returned ok | 18/18 = 100% |

## Calibration

Judgements that stated a value, in the contract band their confidence names.

| Band | From | Judgements | Correct | Accuracy | Mean confidence |
|---|---|---|---|---|---|
| `two_sources` | 0.90 | 9 | 9 | 100% | 0.94 |
| `single_source` | 0.70 | 17 | 9 | 53% | 0.82 |
| `inferred` | 0.50 | 7 | 5 | 71% | 0.61 |
| `hypothesis` | 0.25 | 3 | 2 | 67% | 0.43 |
| `speculation` | 0.00 | 0 | 0 | n/a | n/a |

## Validation retries

| Measure | Count |
|---|---|
| Reports accepted first try | 27 |
| Recovered by a retry | 9 |
| Exhausted | 2 |
| Retry calls made | 14 |

## Cost

| Tokens | Count |
|---|---|
| Input, all of it | 340,635 |
| of which read from the cache | 246,522 |
| of which written to the cache | 4,358 |
| of which billed at the input rate | 89,755 |
| Output | 84,058 |

| The same tokens, priced | USD | Against realtime, uncached |
|---|---|---|
| Realtime, no cache | $0.7609 | baseline |
| This mode, no cache | $0.7609 | +0% |
| As run | $0.5401 | -29% |
| As run, per item | $0.0142 | |

Cache: healthy - every agent's later requests read the prefix the first one wrote.

Retrieval ran degraded:
- query embedding failed (Voyage embeddings request failed: Client error '429 Too Many Requests' for url 'https://api.voyageai.com/v1/embeddings'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429); lexical only

## Items

| Item | Truth | Answer | Confidence | Correct | Ungrounded | Attempts | Input | Output |
|---|---|---|---|---|---|---|---|---|
| `case_01:diagnose` | resource_exhaustion | resource_exhaustion | 0.95 | yes | 0 | 1 | 6,129 | 2,874 |
| `case_01:recall` | doc_0001 | doc_0001 | 0.95 | yes | 0 | 1 | 6,114 | 1,371 |
| `case_02:diagnose` | resource_exhaustion | resource_exhaustion | 0.82 | yes | 1 | 1 | 6,101 | 2,526 |
| `case_02:recall` | doc_0003 | doc_0003 | 0.95 | yes | 0 | 1 | 6,064 | 1,284 |
| `case_03:diagnose` | resource_exhaustion | resource_exhaustion | 0.95 | yes | 2 | 1 | 6,094 | 2,036 |
| `case_03:recall` | doc_0006 | doc_0006 | 0.95 | yes | 0 | 2 | 12,916 | 1,886 |
| `case_04:diagnose` | resource_exhaustion | resource_exhaustion | 0.95 | yes | 0 | 1 | 6,039 | 1,988 |
| `case_04:recall` | doc_0010 | doc_0010 | 0.95 | yes | 0 | 1 | 6,080 | 1,179 |
| `case_05:diagnose` | code_regression | code_regression | 0.95 | yes | 0 | 1 | 6,123 | 1,927 |
| `case_05:recall` | doc_0002 | doc_0002 | 0.95 | yes | 0 | 2 | 13,389 | 2,516 |
| `case_06:diagnose` | code_regression | code_regression | 0.69 | yes | 0 | 2 | 14,050 | 3,794 |
| `case_06:recall` | doc_0007 | doc_0007 | 0.90 | yes | 0 | 2 | 13,322 | 2,315 |
| `case_07:diagnose` | code_regression | code_regression | 0.65 | yes | 0 | 1 | 6,111 | 2,373 |
| `case_07:recall` | doc_0012 | doc_0012 | 0.95 | yes | 0 | 1 | 6,059 | 1,318 |
| `case_08:diagnose` | code_regression | code_regression | 0.35 | yes | 0 | 1 | 6,004 | 1,785 |
| `case_08:recall` | doc_0016 | doc_0016, doc_0002 | 0.95 | yes | 0 | 2 | 13,605 | 2,961 |
| `case_09:diagnose` | bad_config_deploy | bad_config_deploy | 0.95 | yes | 0 | 1 | 6,112 | 1,811 |
| `case_09:recall` | doc_0004 | doc_0004 | 0.95 | yes | 0 | 1 | 6,071 | 1,256 |
| `case_10:diagnose` | bad_config_deploy | bad_config_deploy | 0.92 | yes | 1 | 1 | 6,104 | 2,072 |
| `case_10:recall` | doc_0008 | doc_0008 | 0.95 | yes | 0 | 1 | 6,048 | 1,184 |
| `case_11:diagnose` | bad_config_deploy | bad_config_deploy | 0.92 | yes | 1 | 3 | 24,164 | 5,678 |
| `case_11:recall` | doc_0011 | doc_0011 | 0.95 | yes | 0 | 1 | 6,056 | 1,428 |
| `case_12:diagnose` | bad_config_deploy | bad_config_deploy | 0.48 | yes | 0 | 1 | 6,055 | 2,196 |
| `case_12:recall` | doc_0015 | doc_0015 | 0.95 | yes | 0 | 1 | 5,988 | 1,224 |
| `case_13:diagnose` | downstream_latency | downstream_latency | 0.85 | yes | 0 | 2 | 14,468 | 4,460 |
| `case_13:recall` | doc_0005 | doc_0005 | 0.95 | yes | 0 | 1 | 6,102 | 1,514 |
| `case_14:diagnose` | downstream_latency | downstream_latency | 0.88 | yes | 0 | 2 | 14,595 | 4,741 |
| `case_14:recall` | doc_0009 | doc_0009 | 0.90 | yes | 0 | 1 | 6,073 | 1,091 |
| `case_15:diagnose` | downstream_latency | downstream_latency | 0.50 | yes | 0 | 1 | 6,064 | 2,375 |
| `case_15:recall` | doc_0014 | doc_0014 | 0.95 | yes | 0 | 1 | 6,066 | 1,309 |
| `case_16:diagnose` | downstream_latency | resource_exhaustion | 0.62 | no | 0 | 1 | 6,060 | 2,463 |
| `case_16:recall` | doc_0017 | doc_0017 | 0.95 | yes | 0 | 1 | 6,011 | 1,346 |
| `case_17:diagnose` | other | other | 0.75 | yes | 0 | 2 | 14,043 | 3,956 |
| `case_17:recall` | doc_0013 | doc_0013 | 0.95 | yes | 0 | 1 | 6,081 | 1,261 |
| `case_18:diagnose` | other | resource_exhaustion | 0.68 | no | 0 | 1 | 6,086 | 2,315 |
| `case_18:recall` | doc_0018 | doc_0018 | 0.95 | yes | 0 | 1 | 6,037 | 1,323 |
| `case_19:recall` | - | no response | - | failed | 0 | 3 | 20,055 | 1,469 |
| `case_20:recall` | - | no response | - | failed | 0 | 3 | 22,096 | 3,453 |

## What was refused or ungrounded

- `case_02:diagnose`: evidence ev_006: excerpt is not in the context
- `case_03:diagnose`: affected service 'unknown-service-2' is not in the context
- `case_03:diagnose`: affected service 'unknown-service-3' is not in the context
- `case_10:diagnose`: affected service 'other services (unnamed)' is not in the context
- `case_11:diagnose`: evidence ev_temporal_correlation: excerpt is not in the context
- `case_19:recall` produced no response: ValidationError: 1 validation error for DocsAgentResponse Value error, 2 unanswered sub-question(s) but 0 Gap(s) whose blocks_field is 'findings.coverage.unanswered': every unanswered entry requires its own matching Gap (CONTRACTS.md sec 4.2) [type=value_error, input_value={'request_id': 'req_case_...=datetime.timezone.utc)}, input_type=dict] For further information visit https://errors.pydantic.dev/2.13/v/value_error | validation-retry loop: 3 attempt(s), rejected 3 time(s) [format, format, format]; the retry cap of 2 was reached
- `case_20:recall` produced no response: ValidationError: 1 validation error for DocsAgentResponse Value error, null Assessment at findings.answer requires a Gap whose blocks_field references it (CONTRACTS.md sec 2.1) [type=value_error, input_value={'request_id': 'req_case_...=datetime.timezone.utc)}, input_type=dict] For further information visit https://errors.pydantic.dev/2.13/v/value_error | validation-retry loop: 3 attempt(s), rejected 3 time(s) [format, format, format]; the retry cap of 2 was reached
