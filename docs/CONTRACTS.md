# AIOC — Frozen Contracts

**Schema version: `1.0.0` · Frozen Day 1 · Owner: sole maintainer (both layers)**

This document is the integration surface between the Reasoning Layer and the Platform
Layer. It was written when those layers had separate owners; since Day 6 one maintainer
owns both, and the contract is deliberately kept as a hard boundary anyway - the whole
point is that the two halves can be reasoned about independently.
`EXECUTION_PLAN.md` states the project's one hard rule:

> The tool contract and output schemas are frozen on Day 1. Everything else can churn.
> If those churn, you lose a week to integration pain.

This is that contract.

---

## 0. Status and change control

### What is frozen

| Frozen | Section |
|---|---|
| Shared primitives (`Assessment`, `Evidence`, `Gap`, enum conventions) | §2 |
| The `AgentResponse` envelope | §3 |
| The four agent findings payloads | §4 |
| The `CoordinatorResponse` | §5 |
| The tool request/response envelope and the four-class error taxonomy | §6 |
| The six named tool schemas and the tool-description template | §7 |

### What is deliberately *not* frozen

Handoff digest formats (Day 23), eval record formats (Day 19), prompt text, retrieval
parameters, model selection, and every internal module boundary. These are expected to
churn and are cheaper to get wrong.

### Changing a frozen thing

The original rule was "both engineers agree, in writing, before any code changes." That
rule's actual job was never consensus - it was to force a pause and a written record
before a shared boundary moved. With a sole maintainer the counterparty is gone but the
job remains, so the ceremony is kept and the counterparty is replaced by the record:

1. **Write the rationale down before the code changes**, as a dated entry in
   `docs/design-notes/contract-changes.md`: what moves, why, what breaks, what was
   considered instead. Written *first*, not reconstructed afterwards - a change that
   cannot be justified in prose before it is made is the change this process exists to
   catch.
2. **Preserve the superseded text** in this file, struck through rather than deleted,
   exactly as the `analyze_logs` / `analyze_events` exception already requires. A frozen
   contract whose history is editable is not frozen.
3. Bump `schema_version` — patch for additive-optional, minor for additive-required or a
   new enum member, major for a removal or a type change.
4. Add a row to the changelog in §9 linking the design note.

Steps 1 and 2 are the ones that get skipped when nobody is watching, which is why they
are listed first.

Consumers **must** read `schema_version` off every payload and fail loudly on a major
mismatch rather than best-effort parsing.

### The one pre-authorized exception

`analyze_logs` and `analyze_events` (§7.5, §7.6) ship **deliberately overlapping**. They
are the independent variable in the Domain 2 routing case study: Day 13 records their
misrouting rate over 20 queries, Day 14 splits and renames them and re-runs the same 20.

That refactor is pre-approved as a `1.1.0` bump and needs no further agreement. Their
v1.0.0 definitions must remain in this file verbatim — struck through, not deleted — or
the before/after numbers in `docs/case-study-tool-routing.md` lose their baseline.

No other pre-authorized exceptions exist.

---

## 1. Conventions

**Language.** Python 3.12, Pydantic v2. JSON Schema is normative for anything crossing the
MCP boundary; Pydantic is normative for anything internal to the Reasoning Layer.

**Naming.** `snake_case` for all fields, on the wire and in Python. Enum members are
`snake_case` strings. Tool names are `snake_case` verbs.

**Timestamps.** RFC 3339 with an explicit `Z` offset, always UTC:
`"2026-07-23T14:02:11Z"`. Never a bare epoch, never a local offset.

**Durations.** Integer milliseconds, field name ends `_ms`. Or integer seconds, ending
`_seconds`. Never a bare number.

**Identifiers.** Opaque prefixed strings, stable within one response:
`inc_`, `evt_`, `ev_` (evidence), `doc_`, `claim_`, `act_` (recommended action),
`inv_` (agent invocation), `req_` (request), `tc_` (tool call).

**Money.** `usd` as a float. Token counts are plain integers.

### `null` versus `[]` — these are different and both are legal

| Value | Means | Example |
|---|---|---|
| `null` | Not determined. The agent could not establish this, or the data was absent. | `root_cause.value = null` — no root cause could be identified |
| `[]` | Determined to be empty. The agent looked and there was nothing. | `affected_services = []` — the search ran and matched no services |

**A `null` must never be a guess, a placeholder, or a fabricated plausible value.** When a
field is `null` because information was genuinely absent, the response **must** carry a
matching `Gap` in `gaps[]` explaining what was missing. This is the Day 16 nullable-field
requirement and it is enforced by validator, not convention.

### The `other` enum pattern

Every enum in this document carries an `other` member. Closed enums cause fabrication —
the model picks the nearest wrong member rather than admitting the taxonomy doesn't fit.

`other` is never used alone. It is always paired with a detail string:

- **Wrapped in an `Assessment`** — use the `Assessment.detail` field.
- **A bare enum field `x`** — use the sibling field `x_detail`.

`detail` / `x_detail` is `str | null`, and **must be non-null when the enum value is
`other`**, and **must be `null` otherwise**. Both directions are validated.

```json
{ "environment": "other", "environment_detail": "customer-managed on-prem cluster" }
{ "environment": "production", "environment_detail": null }
```

Recurring `other` values are the signal that the enum needs a new member. Log them; they
are the input to the next minor version bump.

---

## 2. Shared primitives

### 2.1 `Assessment[T]`

The confidence carrier. Applied to **analytic fields only** — anything the agent inferred,
judged, ranked, or concluded. Factual fields (a timestamp, a service name, a commit SHA, a
line count) stay plain scalars; wrapping them adds tokens and implies a judgement that
isn't there.

| Field | Type | Null? | Notes |
|---|---|---|---|
| `value` | `T` | yes | `null` = not determined. Never a guess. |
| `confidence` | `float` | no | `0.0`–`1.0` inclusive. See bands below. |
| `evidence` | `list[str]` | no | Ids into the response's `evidence[]`. May be `[]` only when `value` is `null`. |
| `reasoning` | `str` | yes | One or two sentences. How the evidence supports the value. |
| `detail` | `str` | yes | Required non-null iff `T` is an enum and `value == "other"`. |

**Confidence bands.** These are normative — the eval harness (Day 19) scores calibration
against them, so agents must be prompted with this table.

| Range | Meaning |
|---|---|
| `0.90`–`1.00` | Directly evidenced by two or more independent sources |
| `0.70`–`0.89` | Directly evidenced by a single reliable source |
| `0.50`–`0.69` | Inferred from correlated signals; no direct statement |
| `0.25`–`0.49` | Plausible hypothesis, weak or partial evidence |
| below `0.25` | Speculation — **set `value` to `null` and emit a `Gap` instead** |

**Invariants** (validated):
- `value != null` and `confidence >= 0.5` ⟹ `evidence` is non-empty.
- `value == null` ⟹ `confidence` describes confidence *that it cannot be determined*, and
  a `Gap` referencing this field exists in `gaps[]`.
- `confidence < 0.25` ⟹ `value == null`.

**Scope of the evidence invariant.** Inside an `AgentResponse`, `evidence` ids must resolve
against that response's own `evidence[]`. At coordinator level they resolve against the
union of `agent_responses[].evidence` — the coordinator cites its subagents' evidence
rather than duplicating it. `CoordinatorResponse.intent` is the one exemption: it is
derived from the query text alone, has no external evidence, and is permitted `evidence: []`
at any confidence.

```json
{
  "value": "connection pool exhaustion in checkout-api",
  "confidence": 0.72,
  "evidence": ["ev_2", "ev_5"],
  "reasoning": "Active connections hit the configured max of 20 six minutes before the p99 spike, and the error log shows pool-acquire timeouts.",
  "detail": null
}
```

### 2.2 `Evidence`

Every `Assessment.evidence` id resolves to one of these, in the same response. Dangling
references are a validation error.

| Field | Type | Null? | Notes |
|---|---|---|---|
| `id` | `str` | no | `ev_*`, unique within the response |
| `source_type` | enum | no | `metric` · `log` · `event` · `document` · `commit` · `pull_request` · `deployment` · `config` · `other` |
| `source_type_detail` | `str` | yes | Required iff `source_type == "other"` |
| `source_ref` | `str` | no | Tool-native identifier — a PromQL series, a log id, a chunk id, a SHA |
| `excerpt` | `str` | no | The verbatim slice that supports the claim. Trimmed, never paraphrased. |
| `observed_at` | timestamp | yes | When the underlying fact occurred |
| `uri` | `str` | yes | Where a human can go look |
| `tool_call_id` | `str` | yes | `tc_*` — which tool call produced this |

### 2.3 `Gap`

What the agent could not establish. This is the type the Day 14 coordinator refinement
loop consumes — it re-delegates directly off `suggested_agent` + `suggested_query`, so
those fields exist for a machine, not a reader.

| Field | Type | Null? | Notes |
|---|---|---|---|
| `id` | `str` | no | `gap_*` |
| `description` | `str` | no | What is missing, in one sentence |
| `kind` | enum | no | `missing_data` · `out_of_scope` · `tool_error` · `ambiguous_query` · `insufficient_permission` · `other` |
| `kind_detail` | `str` | yes | Required iff `kind == "other"` |
| `blocks_field` | `str` | yes | Dotted path to the field this gap affects — usually a `null` value, or a confidence held down by the missing data, e.g. `findings.root_cause.value` |
| `suggested_agent` | enum \| `null` | yes | `incident` · `docs` · `github` · `deployment` · `null` if no agent can close it |
| `suggested_query` | `str` | yes | A ready-to-delegate query. Non-null whenever `suggested_agent` is non-null. |
| `resolvable` | `bool` | no | `false` = re-delegation cannot help (data does not exist) |

`resolvable: false` is what stops the refinement loop from spinning. Agents must set it
honestly; the loop trusts it.

### 2.4 `ToolCallRef`

Emitted by every agent for Langfuse tracing (Day 9) and the token-reduction measurement
(Day 24).

| Field | Type | Null? |
|---|---|---|
| `id` | `str` (`tc_*`) | no |
| `tool_name` | `str` | no |
| `server` | `str` | no |
| `started_at` | timestamp | no |
| `duration_ms` | `int` | no |
| `ok` | `bool` | no |
| `error_class` | enum \| `null` | yes — non-null iff `ok == false` |
| `tokens_returned` | `int` | yes |
| `truncated` | `bool` | no |

---

## 3. The `AgentResponse` envelope

All four agents return this shape. Only `findings` differs.

| Field | Type | Null? | Notes |
|---|---|---|---|
| `schema_version` | `str` | no | `"1.0.0"` |
| `agent` | enum | no | `incident` · `docs` · `github` · `deployment` |
| `request_id` | `str` | no | `req_*`, echoed from the coordinator |
| `invocation_id` | `str` | no | `inv_*`, matches the coordinator's `AgentInvocation.invocation_id` |
| `status` | enum | no | `complete` · `partial` · `insufficient_evidence` · `error` · `other` |
| `status_detail` | `str` | yes | Required iff `status == "other"` |
| `summary` | `str` | no | 1–3 sentences. Human-readable, no markdown. |
| `findings` | object | no | Agent-specific; §4 |
| `evidence` | `list[Evidence]` | no | May be `[]` only when `status` is `insufficient_evidence` or `error` |
| `gaps` | `list[Gap]` | no | |
| `overall_confidence` | `float` | no | `0.0`–`1.0`. Not a mean of field confidences — the agent's confidence in the response as a whole. |
| `tool_calls` | `list[ToolCallRef]` | no | |
| `generated_at` | timestamp | no | |

**Status semantics** — the coordinator branches on these:

| `status` | Means | Refinement loop |
|---|---|---|
| `complete` | Every requested question answered | no re-delegation |
| `partial` | Some answered, some `null` with gaps | re-delegate on resolvable gaps |
| `insufficient_evidence` | Nothing answerable; tools worked, data absent | re-delegate only if another agent is suggested |
| `error` | Tools failed; the agent could not run | retry once, then surface |

`status` must be `partial` or weaker whenever any `Assessment.value` in `findings` is
`null`. `complete` with a `null` analytic field is a validation error.

---

## 4. Agent findings payloads

### 4.1 `IncidentFindings`

| Field | Type | Null? | Notes |
|---|---|---|---|
| `incident_window` | `{start: ts, end: ts \| null}` | no | `end: null` = ongoing |
| `affected_services` | `list[str]` | no | `[]` is legal — means none found |
| `severity` | `Assessment[Severity]` | no | enum: `sev1` · `sev2` · `sev3` · `sev4` · `other` |
| `failure_mode` | `Assessment[FailureMode]` | no | see below |
| `root_cause` | `Assessment[str]` | no | |
| `contributing_factors` | `list[Assessment[str]]` | no | |
| `timeline` | `list[TimelineEvent]` | no | Ascending by `at` |
| `impact` | `Impact` | no | every field inside is nullable |
| `recommended_actions` | `list[RecommendedAction]` | no | |
| `similar_incidents` | `list[str]` | no | `inc_*` ids from the seeded corpus |

**`FailureMode` enum** — members map 1:1 to the Day 4 chaos scripts so the eval harness can
score agent output against injected ground truth. Do not add members without adding a
chaos mode.

`resource_exhaustion` · `bad_config_deploy` · `downstream_latency` · `code_regression` · `other`

**`TimelineEvent`**

| Field | Type | Null? |
|---|---|---|
| `id` | `str` (`evt_*`) | no |
| `at` | timestamp | no |
| `service` | `str` | no |
| `kind` | enum: `deploy` · `alert` · `config_change` · `restart` · `scale` · `metric_threshold` · `log_pattern` · `other` | no |
| `kind_detail` | `str` | yes |
| `description` | `str` | no |
| `severity` | `Severity` \| `null` | yes |
| `evidence_id` | `str` | yes |

**`Impact`** — all nullable. An unmeasured metric is `null`, never `0`.

`error_rate_before` · `error_rate_after` (floats, 0–1) · `p50_latency_ms_before` ·
`p50_latency_ms_after` · `p99_latency_ms_before` · `p99_latency_ms_after` (ints) ·
`requests_affected` (int) · `duration_seconds` (int)

**`RecommendedAction`** — the input to the Day 16 HITL gate.

| Field | Type | Null? | Notes |
|---|---|---|---|
| `id` | `str` (`act_*`) | no | |
| `action` | `str` | no | Imperative, one line |
| `rationale` | `str` | no | |
| `risk` | enum | no | `low` · `medium` · `high` · `other` |
| `risk_detail` | `str` | yes | |
| `reversible` | `bool` | no | |
| `requires_approval` | `bool` | no | see rule |
| `target_service` | `str` | yes | |
| `command` | `str` | yes | Exact command, if one exists |

**Approval rule** (validated): `requires_approval` **must** be `true` when `risk` is
`medium`, `high`, or `other`, **or** when the action mutates production state — rollback,
restart, scale, merge, config write, traffic shift. Read-only actions may be `false`.

### 4.2 `DocsFindings`

Carries the Day 18 Domain 5 shore-up: claim → source mapping and coverage-gap reporting.

| Field | Type | Null? | Notes |
|---|---|---|---|
| `answer` | `Assessment[str]` | no | Synthesized from claims only |
| `claims` | `list[Claim]` | no | |
| `coverage` | `Coverage` | no | |

**`Claim`** — one atomic assertion, individually sourced.

| Field | Type | Null? | Notes |
|---|---|---|---|
| `id` | `str` (`claim_*`) | no | |
| `statement` | `str` | no | One assertion. No conjunctions. |
| `supported` | `bool` | no | see invariant |
| `sources` | `list[SourceRef]` | no | |
| `confidence` | `float` | no | |

**Invariant** (validated): `sources == []` ⟹ `supported == false`. And `supported == false`
claims **must not** appear in `answer.value` — the Docs agent's whole constraint is that it
answers from retrieved documents only. An unsupported claim is reported so the gap is
visible, not so it can be used.

**`SourceRef`**

| Field | Type | Null? |
|---|---|---|
| `document_id` | `str` (`doc_*`) | no |
| `title` | `str` | no |
| `chunk_id` | `str` | yes |
| `uri` | `str` | yes |
| `quote` | `str` | yes — verbatim, never paraphrased |
| `relevance` | `float` | yes — retrieval score, 0–1 |

**`Coverage`** — coverage-gap reporting.

| Field | Type | Null? |
|---|---|---|
| `sub_questions` | `list[str]` | no — the question decomposed |
| `answered` | `list[str]` | no — subset of `sub_questions` |
| `unanswered` | `list[str]` | no — subset of `sub_questions` |
| `documents_searched` | `int` | no |
| `documents_retrieved` | `int` | no |
| `documents_cited` | `int` | no |
| `corpus_snapshot` | `str` | yes — ingestion run id |

`answered ∪ unanswered == sub_questions` and the two are disjoint. Every entry in
`unanswered` must have a matching `Gap` in the envelope.

### 4.3 `GitHubFindings`

| Field | Type | Null? | Notes |
|---|---|---|---|
| `repository` | `str` | no | `owner/name` |
| `ref` | `str` | yes | Branch or tag examined |
| `pull_requests` | `list[PullRequestAnalysis]` | no | |
| `commits` | `list[CommitRef]` | no | |
| `suspect_changes` | `list[SuspectChange]` | no | |
| `diff_summary` | `Assessment[str]` | no | |

**`PullRequestAnalysis`** — factual fields plain, judgements wrapped.

| Field | Type | Null? |
|---|---|---|
| `number` | `int` | no |
| `title` | `str` | no |
| `state` | enum: `open` · `closed` · `merged` · `draft` · `other` | no |
| `state_detail` | `str` | yes |
| `merged_at` | timestamp | yes |
| `head_sha` | `str` | no |
| `files_changed` / `additions` / `deletions` | `int` | no |
| `touched_paths` | `list[str]` | no |
| `risk` | `Assessment[RiskLevel]` | no |
| `summary` | `Assessment[str]` | no |

**`CommitRef`**: `sha`, `short_sha`, `message`, `authored_at`, `touched_paths: list[str]`,
`pull_request_number: int | null`. All plain — these are facts.

**`SuspectChange`** — links a code change to an observed symptom. This is the join point
with `IncidentFindings` on the Day 13 sequential path.

| Field | Type | Null? |
|---|---|---|
| `change_ref` | `str` | no — SHA or `#PR` |
| `change_type` | enum: `code` · `dependency` · `config` · `schema` · `infrastructure` · `other` | no |
| `change_type_detail` | `str` | yes |
| `symptom_link` | `Assessment[str]` | no — how this change explains the symptom |

### 4.4 `DeploymentFindings`

| Field | Type | Null? | Notes |
|---|---|---|---|
| `service` | `str` | no | |
| `environment` | enum | no | `development` · `staging` · `production` · `other` |
| `environment_detail` | `str` | yes | |
| `releases_compared` | `{from_version: str \| null, to_version: str}` | no | `from_version: null` = first release |
| `rollout_status` | `Assessment[RolloutStatus]` | no | `healthy` · `degraded` · `failed` · `in_progress` · `rolled_back` · `unknown` · `other` |
| `changed_config_keys` | `list[str]` | no | **Keys only. Values are never returned.** |
| `image_changes` | `list[ImageChange]` | no | |
| `health_signals` | `HealthSignals` | no | all fields nullable |
| `regression_suspected` | `Assessment[bool]` | no | |
| `rollback_recommendation` | `Assessment[RollbackRecommendation]` | no | `rollback_now` · `hold_and_monitor` · `no_action` · `insufficient_data` · `other` |
| `approval` | `ApprovalRequirement` | no | |

**Config values are never returned**, by any tool or agent, at any layer. Config keys leak
nothing; config values leak connection strings and API keys. The Day 27 secrets audit
scans for violations of this rule, and `diff_release` (§7.3) enforces it at the source.

**`ImageChange`**: `container: str`, `from_image: str | null`, `to_image: str`,
`from_digest: str | null`, `to_digest: str | null`.

**`HealthSignals`** — all nullable: `replicas_desired`, `replicas_ready`,
`restart_count`, `probe_failures` (ints), `error_rate`, `p99_latency_ms` (floats),
`observed_over_seconds` (int).

**`ApprovalRequirement`**: `requires_approval: bool` (**const `true`** on this agent — every
deployment recommendation is human-gated), `risk: RiskLevel`, `risk_detail: str | null`,
`blast_radius: str | null`.

---

## 5. `CoordinatorResponse`

| Field | Type | Null? | Notes |
|---|---|---|---|
| `schema_version` | `str` | no | `"1.0.0"` |
| `request_id` | `str` | no | `req_*` |
| `query` | `str` | no | Verbatim user query |
| `received_at` | timestamp | no | |
| `intent` | `Assessment[Intent]` | no | `incident_diagnosis` · `documentation_lookup` · `code_change_review` · `deployment_check` · `mixed` · `other` |
| `selected_agents` | `list[AgentInvocation]` | no | |
| `skipped_agents` | `list[SkippedAgent]` | no | **dynamic-selection evidence** |
| `agent_responses` | `list[AgentResponse]` | no | |
| `synthesis` | `str` | no | Prose answer |
| `answer` | `Assessment[str]` | no | The one-line conclusion |
| `refinement_rounds` | `int` | no | `0` = no re-delegation needed |
| `unresolved_gaps` | `list[Gap]` | no | Gaps still open after the last round |
| `status` | enum: `complete` · `partial` · `insufficient_evidence` · `error` · `other` | no | |
| `cost` | `Cost` | no | |
| `trace_id` | `str` | yes | Langfuse trace |
| `completed_at` | timestamp | no | |

**`AgentInvocation`** — this type is the schema-level proof of the plan's "single
most-tested orchestration fact": *each subagent receives its context explicitly in its
prompt, with no automatic inheritance.*

| Field | Type | Null? | Notes |
|---|---|---|---|
| `invocation_id` | `str` (`inv_*`) | no | |
| `agent` | enum | no | |
| `reason` | `str` | no | Why this agent, for this query |
| `mode` | enum: `parallel` · `sequential` · `other` | no | |
| `depends_on` | `list[str]` | no | `inv_*` ids. **Must be `[]` when `mode == "parallel"`** and non-empty when `sequential`. |
| `context_passed` | `str` | no | **The literal context block embedded in the subagent's prompt.** Must be non-empty. |
| `round` | `int` | no | `0` = initial, `1+` = refinement |

**`context_passed` must be non-empty** — validated. An empty value means context was
assumed to be inherited, which is exactly the failure this project is built to demonstrate
the absence of.

**`SkippedAgent`**: `agent: enum`, `reason: str`. Populated on every request — an empty
list means all four ran, which for most queries indicates the dynamic selection isn't
working.

**`Cost`**: `input_tokens`, `output_tokens` (ints), `cache_read_tokens`,
`cache_write_tokens` (`int | null`), `usd` (`float | null`).

---

## 6. Tool interface (normative)

**The MCP wire format is normative.** The Python signatures in §6.4 are illustrative and
non-binding — the Platform Layer implements the server, the Reasoning Layer calls it over
the wire, and the wire is what they must agree on. One maintainer now writes both sides,
which makes it *easier*, not safer, to let a Python detail leak across; the wire stays the
only binding artifact for exactly that reason.

### 6.1 Request

Standard MCP `tools/call`. Input is validated against the tool's JSON Schema before the
handler runs; a schema failure returns a `validation` error, never an exception.

### 6.2 Success response

```json
{
  "content": [{ "type": "text", "text": "<json string of the payload below>" }],
  "isError": false
}
```

Payload:

```json
{
  "ok": true,
  "data": { },
  "meta": {
    "truncated": false,
    "total_available": 214,
    "returned": 50,
    "token_estimate": 1840,
    "query_ms": 312,
    "source": "prometheus",
    "as_of": "2026-07-23T14:07:00Z"
  }
}
```

**`meta` is required on every success response.** `truncated`, `returned` and
`token_estimate` are non-null; `total_available`, `query_ms`, `source` and `as_of` may be
`null`. This block is what the Day 21 trimming work and the Day 24 token-reduction
measurement are computed from — without it there is no baseline to reduce against.

**An empty result is a success, not an error.** `ok: true`, `data` with an empty
collection, `meta.returned: 0`. A `business` error means the request *cannot be computed*
(the version doesn't exist), not that it computed to nothing.

### 6.3 Error response

```json
{
  "content": [{ "type": "text", "text": "<json string of the payload below>" }],
  "isError": true
}
```

Payload:

```json
{
  "ok": false,
  "error": {
    "class": "transient",
    "code": "PROMETHEUS_TIMEOUT",
    "message": "Prometheus query exceeded the 5s deadline.",
    "retryable": true,
    "retry_after_ms": 2000,
    "details": { "query": "histogram_quantile(0.99, ...)", "deadline_ms": 5000 },
    "remediation": "Retry after 2s, or narrow the time window to reduce series cardinality."
  }
}
```

`class` is the wire name; in Python it aliases to `error_class` (`class` is a keyword).

`isError` and `ok` always agree. Never return `ok: false` with `isError: false`, and never
encode a failure as prose inside a success payload — that is the failure mode this taxonomy
exists to prevent.

### 6.4 The four error classes

| `class` | Means | `retryable` | `retry_after_ms` | Must include | Example `code` |
|---|---|---|---|---|---|
| `transient` | Upstream unavailable, timeout, rate limit | `true` | required, non-null | a retry hint in `remediation` | `PROMETHEUS_TIMEOUT` |
| `validation` | The caller's input is malformed | `false` | `null` | `details.field` and `details.expected` | `INVALID_TIME_RANGE` |
| `business` | Well-formed request that cannot be satisfied by the data | `false` | `null` | an alternative in `remediation` | `UNKNOWN_RELEASE_VERSION` |
| `permission` | The caller lacks the required scope | `false` | `null` | `details.required_scope` | `GITHUB_SCOPE_MISSING` |

**`transient` is the only class with `retryable: true`.** Retrying a validation, business,
or permission error is guaranteed to fail identically — the agent must change the request,
escalate, or record a `Gap` with `resolvable: false`.

`code` is `SCREAMING_SNAKE_CASE`, stable, and namespaced by upstream system. Codes are
matched programmatically; `message` is for humans and may change freely.

Illustrative Python (non-binding):

```python
class ToolError(BaseModel):
    error_class: Literal["transient", "validation", "business", "permission"] = Field(alias="class")
    code: str
    message: str
    retryable: bool
    retry_after_ms: int | None = None
    details: dict[str, Any] | None = None
    remediation: str
```

### 6.5 The tool description template

**Every tool description must have these four parts, in this order.** This is frozen; it is
what lets new tools land after Day 1 without a contract change, and it is the intervention
measured by the Day 13/14 routing case study.

1. **What it does, and input formats.** One sentence, then the shape and units of every
   non-obvious input.
2. **Example queries.** At least three natural-language queries this tool answers.
3. **Edge cases and limits.** Max rows, retention window, what an empty result means, known
   blind spots.
4. **When to use this vs. the alternative.** Names the competing tool explicitly and states
   the discriminator.

Part 4 is the one that matters. A tool with no alternative still needs the line — "no
alternative; this is the only source for X" is a valid part 4.

---

## 7. The six frozen tools

### 7.1 `get_incident_timeline`

Ordered operational events for one service over a window.

**Input**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `service` | `str` | yes | — | Service name as scraped by Prometheus |
| `start` | timestamp | yes | — | RFC 3339 UTC |
| `end` | timestamp | no | now | Must be `> start` |
| `event_kinds` | `list[enum]` | no | all | `TimelineEvent.kind` members |
| `min_severity` | enum | no | `sev4` | `sev1`–`sev4` |
| `max_events` | `int` | no | `50` | `1`–`200` |

**`data`**: `{ events: TimelineEvent[], window: {start, end}, services_covered: str[] }`

**Errors**: `INVALID_TIME_RANGE` (validation, `end <= start` or window > 7d) ·
`UNKNOWN_SERVICE` (business) · `PROMETHEUS_TIMEOUT` (transient)

**Part 4:** Use this when you need *what happened and in what order* for a single service.
Use `correlate_events` instead when you need to know *which signals moved together* across
services around a known point in time.

### 7.2 `correlate_events`

Signals statistically correlated with an anchor point.

**Input**

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `anchor_event_id` | `str` | one of | — | `evt_*` from `get_incident_timeline` |
| `anchor_at` | timestamp | one of | — | Use with `anchor_service` |
| `anchor_service` | `str` | no | — | Required if `anchor_at` given |
| `window_seconds` | `int` | no | `900` | `60`–`21600`, centred on the anchor |
| `services` | `list[str]` | no | all | |
| `signal_types` | `list[enum]` | no | all | `metric` · `log_rate` · `event` · `deploy` · `other` |
| `min_correlation` | `float` | no | `0.5` | `0.0`–`1.0` |

Exactly one of `anchor_event_id` or `anchor_at` must be present — supplying both or neither
is `AMBIGUOUS_ANCHOR` (validation).

**`data`**: `{ anchor: {...}, correlations: [{signal, service, correlation: float, lag_seconds: int, direction: "leads"|"lags"|"coincident"|"other", sample_size: int}], method: str }`

`correlation` is Pearson on the aligned series. **This is correlation, not causation** —
the description says so explicitly, because agents that read it as causal produce
confidently wrong root causes.

**Errors**: `AMBIGUOUS_ANCHOR` (validation) · `UNKNOWN_EVENT` (business) ·
`INSUFFICIENT_SAMPLES` (business, fewer than 10 aligned points) · `PROMETHEUS_TIMEOUT` (transient)

**Part 4:** Use this when you have a known incident moment and want to find what else
moved. Use `get_incident_timeline` instead when you need the ordered narrative for one
service, and `diff_release` when the anchor is a deployment and you want to know what
changed in it.

### 7.3 `diff_release`

Structural difference between two releases of a service.

**Input**: `service` (str, required) · `from_version` (str, required) ·
`to_version` (str, required) · `include` (enum `config` · `images` · `manifests` ·
`commits` · `all`, default `all`)

**`data`**

```json
{
  "service": "checkout-api",
  "from_version": "v1.4.2",
  "to_version": "v1.4.3",
  "config_keys_added": ["DB_POOL_TIMEOUT_MS"],
  "config_keys_removed": [],
  "config_keys_changed": ["DB_POOL_MAX"],
  "image_changes": [{"container": "app", "from_image": "...", "to_image": "..."}],
  "commits": [{"sha": "...", "message": "...", "authored_at": "..."}],
  "manifest_changes": ["spec.replicas", "spec.template.spec.resources.limits.memory"]
}
```

**Config values are never returned — keys only.** This is enforced here, at the source, and
is the reason `DeploymentFindings.changed_config_keys` can be safely rendered in a demo.

**Errors**: `UNKNOWN_RELEASE_VERSION` (business) · `SAME_VERSION` (validation) ·
`REGISTRY_UNAVAILABLE` (transient) · `REGISTRY_SCOPE_MISSING` (permission)

**Part 4:** Use this to answer *what changed between two releases*. Use
`check_rollout_health` instead to answer *how the current release is behaving*. Diff tells
you the cause candidates; health tells you whether there is a problem at all.

### 7.4 `check_rollout_health`

Current health of a deployed version.

**Input**: `service` (str, required) · `environment` (enum, required) ·
`version` (str, optional — defaults to currently deployed) ·
`lookback_minutes` (int, default `30`, range `5`–`1440`)

**`data`**: `{ service, environment, version, status: RolloutStatus, replicas: {desired, ready, updated, unavailable}, signals: {error_rate, p50_latency_ms, p99_latency_ms, restart_count, probe_failures}, compared_to_baseline: {baseline_version, error_rate_delta, p99_delta_ms} | null }`

Any signal that could not be measured is `null`, never `0`.

**Errors**: `UNKNOWN_SERVICE` (business) · `VERSION_NOT_DEPLOYED` (business) ·
`INVALID_LOOKBACK` (validation) · `PROMETHEUS_TIMEOUT` (transient)

**Part 4:** Use this for *is the current rollout healthy right now*. Use `diff_release`
when health is already known to be bad and you need the candidate cause.

### 7.5 `analyze_logs` — v1.0.0, deliberately overlapping

> **Case-study variable.** This tool and §7.6 overlap on purpose. Their descriptions
> deliberately omit a usable part 4. Day 13 records the misrouting rate across 20 queries;
> Day 14 splits and renames them and re-runs the same 20. Preserve these v1 definitions
> verbatim when that happens — they are the baseline.

**Input**: `service` (str, required) · `start` (ts, required) · `end` (ts, optional) ·
`pattern` (str, optional — regex) · `level` (enum `debug` · `info` · `warn` · `error` ·
`fatal` · `other`, optional) · `max_matches` (int, default `100`)

**`data`**: `{ matches: [{at, service, level, message, source_ref}], total_matched: int, patterns_detected: [{pattern, count, first_at, last_at}] }`

**Errors**: `INVALID_PATTERN` (validation) · `LOG_STORE_UNAVAILABLE` (transient) ·
`UNKNOWN_SERVICE` (business)

**Part 4 (v1, intentionally weak):** "Use this to analyze service output over a time
window."

### 7.6 `analyze_events` — v1.0.0, deliberately overlapping

> Same case-study note as §7.5.

**Input**: `service` (str, required) · `start` (ts, required) · `end` (ts, optional) ·
`pattern` (str, optional) · `kind` (enum — `TimelineEvent.kind` members, optional) ·
`max_matches` (int, default `100`)

**`data`**: `{ matches: [{at, service, kind, description, source_ref}], total_matched: int, patterns_detected: [{pattern, count, first_at, last_at}] }`

**Errors**: `INVALID_PATTERN` (validation) · `EVENT_STORE_UNAVAILABLE` (transient) ·
`UNKNOWN_SERVICE` (business)

**Part 4 (v1, intentionally weak):** "Use this to analyze service activity over a time
window."

---

## 8. Worked example

The Day 10 demo query. **When prose in this document and this example disagree, the example
wins.** Abridged where repetition adds nothing (`…`), but every structure that matters
appears at least once: a parallel fan-out, a skipped agent, a populated `Assessment`, a
`null` value with its matching `Gap`, a tool error, and a truncated tool response.

```json
{
  "schema_version": "1.0.0",
  "request_id": "req_8f21",
  "query": "Why did latency spike after the last deploy?",
  "received_at": "2026-07-23T14:07:02Z",
  "intent": {
    "value": "incident_diagnosis",
    "confidence": 0.91,
    "evidence": [],
    "reasoning": "Asks for causal explanation of a live symptom tied to a deployment.",
    "detail": null
  },
  "selected_agents": [
    {
      "invocation_id": "inv_a1",
      "agent": "incident",
      "reason": "The symptom is a latency spike; Prometheus is the authoritative source.",
      "mode": "parallel",
      "depends_on": [],
      "context_passed": "User query: 'Why did latency spike after the last deploy?' Time of interest: 2026-07-23T13:40Z to 14:07Z. Known deploy: checkout-api v1.4.3 at 13:52Z. Investigate p99 latency for checkout-api and its downstream dependencies.",
      "round": 0
    },
    {
      "invocation_id": "inv_a2",
      "agent": "docs",
      "reason": "A runbook may already document this failure signature.",
      "mode": "parallel",
      "depends_on": [],
      "context_passed": "User query: 'Why did latency spike after the last deploy?' Search the runbook corpus for checkout-api latency, connection pool sizing, and post-deploy latency regressions. Cite every claim.",
      "round": 0
    }
  ],
  "skipped_agents": [
    { "agent": "github", "reason": "No code-level question asked; deferred until a suspect commit exists." },
    { "agent": "deployment", "reason": "Deploy identity already known from the timeline; a release diff is not yet needed." }
  ],
  "agent_responses": [
    {
      "schema_version": "1.0.0",
      "agent": "incident",
      "request_id": "req_8f21",
      "invocation_id": "inv_a1",
      "status": "partial",
      "status_detail": null,
      "summary": "p99 latency on checkout-api rose from 180ms to 2100ms six minutes after v1.4.3 deployed. Connection-pool acquire timeouts appear in the same window.",
      "findings": {
        "incident_window": { "start": "2026-07-23T13:52:00Z", "end": null },
        "affected_services": ["checkout-api"],
        "severity": {
          "value": "sev2",
          "confidence": 0.84,
          "evidence": ["ev_1"],
          "reasoning": "Customer-facing latency degraded 11x, but no requests are failing outright.",
          "detail": null
        },
        "failure_mode": {
          "value": "resource_exhaustion",
          "confidence": 0.72,
          "evidence": ["ev_1", "ev_2"],
          "reasoning": "Active DB connections pinned at the configured maximum of 20 from 13:58Z onward, with pool-acquire timeouts logged.",
          "detail": null
        },
        "root_cause": {
          "value": "checkout-api v1.4.3 lowered DB_POOL_MAX from 50 to 20, exhausting the pool under normal traffic.",
          "confidence": 0.68,
          "evidence": ["ev_1", "ev_2"],
          "reasoning": "Pool saturation begins six minutes after the v1.4.3 rollout and the symptom is consistent with a reduced pool ceiling; the config change itself is not yet confirmed.",
          "detail": null
        },
        "contributing_factors": [],
        "timeline": [
          {
            "id": "evt_1",
            "at": "2026-07-23T13:52:00Z",
            "service": "checkout-api",
            "kind": "deploy",
            "kind_detail": null,
            "description": "checkout-api v1.4.2 -> v1.4.3 rolled out",
            "severity": null,
            "evidence_id": "ev_3"
          },
          {
            "id": "evt_2",
            "at": "2026-07-23T13:58:00Z",
            "service": "checkout-api",
            "kind": "metric_threshold",
            "kind_detail": null,
            "description": "db_connections_active reached max_open_connections (20)",
            "severity": "sev2",
            "evidence_id": "ev_1"
          }
        ],
        "impact": {
          "error_rate_before": 0.001,
          "error_rate_after": 0.004,
          "p50_latency_ms_before": 42,
          "p50_latency_ms_after": 310,
          "p99_latency_ms_before": 180,
          "p99_latency_ms_after": 2100,
          "requests_affected": null,
          "duration_seconds": 900
        },
        "recommended_actions": [
          {
            "id": "act_1",
            "action": "Roll back checkout-api to v1.4.2.",
            "rationale": "Restores the previous pool ceiling and is fully reversible.",
            "risk": "medium",
            "risk_detail": null,
            "reversible": true,
            "requires_approval": true,
            "target_service": "checkout-api",
            "command": "kubectl rollout undo deployment/checkout-api"
          }
        ],
        "similar_incidents": ["inc_0007"]
      },
      "evidence": [
        {
          "id": "ev_1",
          "source_type": "metric",
          "source_type_detail": null,
          "source_ref": "db_connections_active{service=\"checkout-api\"}",
          "excerpt": "13:58:00Z value=20; 13:59:00Z value=20; 14:00:00Z value=20 (max_open_connections=20)",
          "observed_at": "2026-07-23T13:58:00Z",
          "uri": "http://localhost:9090/graph?g0.expr=db_connections_active",
          "tool_call_id": "tc_1"
        },
        {
          "id": "ev_2",
          "source_type": "log",
          "source_type_detail": null,
          "source_ref": "checkout-api/2026-07-23T13:59:12Z#4471",
          "excerpt": "ERROR pool: timed out acquiring connection after 5000ms",
          "observed_at": "2026-07-23T13:59:12Z",
          "uri": null,
          "tool_call_id": "tc_4"
        },
        {
          "id": "ev_3",
          "source_type": "deployment",
          "source_type_detail": null,
          "source_ref": "checkout-api@v1.4.3",
          "excerpt": "rollout completed 13:52:00Z, 3/3 replicas ready",
          "observed_at": "2026-07-23T13:52:00Z",
          "uri": null,
          "tool_call_id": "tc_1"
        }
      ],
      "gaps": [
        {
          "id": "gap_1",
          "description": "The DB_POOL_MAX change is inferred from the symptom, not read from the v1.4.3 release.",
          "kind": "missing_data",
          "kind_detail": null,
          "blocks_field": "findings.root_cause.confidence",
          "suggested_agent": "deployment",
          "suggested_query": "Diff checkout-api v1.4.2 against v1.4.3 and report which configuration keys changed.",
          "resolvable": true
        },
        {
          "id": "gap_2",
          "description": "Request volume during the incident window is unavailable; the traffic counter was not scraped between 13:55Z and 14:05Z.",
          "kind": "tool_error",
          "kind_detail": null,
          "blocks_field": "findings.impact.requests_affected",
          "suggested_agent": null,
          "suggested_query": null,
          "resolvable": false
        }
      ],
      "overall_confidence": 0.7,
      "tool_calls": [
        {
          "id": "tc_1",
          "tool_name": "get_incident_timeline",
          "server": "aioc-incident",
          "started_at": "2026-07-23T14:07:03Z",
          "duration_ms": 412,
          "ok": true,
          "error_class": null,
          "tokens_returned": 1840,
          "truncated": true
        },
        {
          "id": "tc_2",
          "tool_name": "analyze_logs",
          "server": "aioc-incident",
          "started_at": "2026-07-23T14:07:04Z",
          "duration_ms": 5021,
          "ok": false,
          "error_class": "transient",
          "tokens_returned": null,
          "truncated": false
        },
        {
          "id": "tc_4",
          "tool_name": "analyze_logs",
          "server": "aioc-incident",
          "started_at": "2026-07-23T14:07:11Z",
          "duration_ms": 830,
          "ok": true,
          "error_class": null,
          "tokens_returned": 640,
          "truncated": false
        }
      ],
      "generated_at": "2026-07-23T14:07:16Z"
    },
    {
      "schema_version": "1.0.0",
      "agent": "docs",
      "request_id": "req_8f21",
      "invocation_id": "inv_a2",
      "status": "partial",
      "status_detail": null,
      "summary": "The runbook documents connection-pool sizing for checkout-api but says nothing about post-deploy latency regressions.",
      "findings": {
        "answer": {
          "value": "The checkout-api runbook specifies a minimum DB pool size of 50 for production traffic.",
          "confidence": 0.88,
          "evidence": ["ev_4"],
          "reasoning": "Stated directly in the connection-pool sizing section of the service runbook.",
          "detail": null
        },
        "claims": [
          {
            "id": "claim_1",
            "statement": "checkout-api requires a minimum DB pool size of 50 in production.",
            "supported": true,
            "sources": [
              {
                "document_id": "doc_012",
                "title": "checkout-api runbook",
                "chunk_id": "doc_012#7",
                "uri": "docs/runbooks/checkout-api.md",
                "quote": "Production requires DB_POOL_MAX >= 50; below this, pool acquisition times out under normal peak load.",
                "relevance": 0.91
              }
            ],
            "confidence": 0.88
          },
          {
            "id": "claim_2",
            "statement": "A documented rollback procedure exists for checkout-api pool misconfiguration.",
            "supported": false,
            "sources": [],
            "confidence": 0.1
          }
        ],
        "coverage": {
          "sub_questions": [
            "What is the documented DB pool sizing for checkout-api?",
            "Is there a documented post-deploy latency regression procedure?"
          ],
          "answered": ["What is the documented DB pool sizing for checkout-api?"],
          "unanswered": ["Is there a documented post-deploy latency regression procedure?"],
          "documents_searched": 18,
          "documents_retrieved": 4,
          "documents_cited": 1,
          "corpus_snapshot": "ingest_2026-07-20"
        }
      },
      "evidence": [
        {
          "id": "ev_4",
          "source_type": "document",
          "source_type_detail": null,
          "source_ref": "doc_012#7",
          "excerpt": "Production requires DB_POOL_MAX >= 50; below this, pool acquisition times out under normal peak load.",
          "observed_at": null,
          "uri": "docs/runbooks/checkout-api.md",
          "tool_call_id": "tc_3"
        }
      ],
      "gaps": [
        {
          "id": "gap_3",
          "description": "No document in the corpus covers post-deploy latency regression handling.",
          "kind": "missing_data",
          "kind_detail": null,
          "blocks_field": "findings.coverage.unanswered",
          "suggested_agent": null,
          "suggested_query": null,
          "resolvable": false
        }
      ],
      "overall_confidence": 0.74,
      "tool_calls": [
        {
          "id": "tc_3",
          "tool_name": "search_corpus",
          "server": "aioc-docs",
          "started_at": "2026-07-23T14:07:03Z",
          "duration_ms": 189,
          "ok": true,
          "error_class": null,
          "tokens_returned": 920,
          "truncated": false
        }
      ],
      "generated_at": "2026-07-23T14:07:11Z"
    }
  ],
  "synthesis": "checkout-api v1.4.3 rolled out at 13:52Z. Six minutes later the database connection pool saturated at its configured maximum of 20 and p99 latency rose from 180ms to 2100ms, with pool-acquire timeouts in the logs. The runbook requires a minimum pool size of 50 in production, so the deployed ceiling of 20 is below the documented floor. The configuration change itself has not yet been read from the release, which is the remaining gap.",
  "answer": {
    "value": "v1.4.3 appears to have reduced the checkout-api DB connection pool below its documented production minimum, saturating the pool and driving the latency spike.",
    "confidence": 0.68,
    "evidence": ["ev_1", "ev_2", "ev_4"],
    "reasoning": "Two independent agents agree on pool exhaustion; the config change is inferred rather than observed.",
    "detail": null
  },
  "refinement_rounds": 0,
  "unresolved_gaps": [
    {
      "id": "gap_1",
      "description": "The DB_POOL_MAX change is inferred from the symptom, not read from the v1.4.3 release.",
      "kind": "missing_data",
      "kind_detail": null,
      "blocks_field": "findings.root_cause.confidence",
      "suggested_agent": "deployment",
      "suggested_query": "Diff checkout-api v1.4.2 against v1.4.3 and report which configuration keys changed.",
      "resolvable": true
    }
  ],
  "status": "partial",
  "cost": {
    "input_tokens": 18420,
    "output_tokens": 2110,
    "cache_read_tokens": 12000,
    "cache_write_tokens": null,
    "usd": 0.14
  },
  "trace_id": "lf_9c02",
  "completed_at": "2026-07-23T14:07:19Z"
}
```

**What to notice.** `gap_1` is `resolvable: true` and names the Deployment agent with a
ready-to-send query — that is the Day 14 refinement loop's entire input, and running one
more round is exactly what would lift `root_cause.confidence` above 0.7. `gap_2` is
`resolvable: false`, so the loop must not retry it. `impact.requests_affected` is `null`
with `gap_2` attached rather than a plausible-looking number. `claim_2` has no sources, so
`supported: false`, and it does not appear in `answer.value`. `tc_2` failed with a
`transient` error, so the agent retried it — `tc_4` is the successful retry, and `ev_2`
cites `tc_4`, not the failed call. A `validation`, `business`, or `permission` failure
would instead have become a `Gap` with `resolvable: false`. `tc_1` returned
`truncated: true` — the Day 21 trimming work starts there.

---

## 9. Changelog

| Version | Day | Change | Agreed by |
|---|---|---|---|
| `1.0.0` | 1 | Initial freeze — shared primitives, agent envelope, four findings payloads, coordinator response, tool envelope and error taxonomy, six tool schemas, description template. | A + B |
