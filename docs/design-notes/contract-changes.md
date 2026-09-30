# Contract change log - rationale before code

Every change to a frozen part of `docs/CONTRACTS.md` gets an entry here, written **before**
the code changes, per CONTRACTS.md §0.

This file exists because the project lost its second engineer on Day 6.
The original change process required both engineers to agree in writing before a frozen
shape moved.
That rule's real function was never consensus - it was to force a pause and a written
record before a shared boundary moved.
A sole maintainer has no counterparty to persuade, so the record *is* the counterparty.

## What an entry must contain

| Field | Why |
|---|---|
| Date and resulting `schema_version` | Ties the note to the §9 changelog row |
| What moves | The exact section, type, field, or enum member |
| Why | The forcing problem, not the convenience |
| What breaks | Every consumer that must change, named |
| What was considered instead | The rejected options, with the reason each was rejected |

An entry written after the code changed is not an entry.
It is a rationalisation, and it cannot catch the change it was supposed to catch.

## Entries

### 2026-09-17 - `1.1.0` - the `analyze_logs` / `analyze_events` split (Day 14)

Written before the code changed.
The v1.0.0 definitions stay in CONTRACTS.md struck through, the v1 server module stays runnable, and the same forty queries are re-run against the new descriptions; the numbers go to `docs/case-study-tool-routing.md`.

| Field | |
|---|---|
| Date and resulting `schema_version` | 2026-09-17, `1.0.0` -> `1.1.0` |
| What moves | §7.5 `analyze_logs` becomes `search_container_logs`; §7.6 `analyze_events` becomes `search_recorded_events`. Same inputs, same `data` shape, same error codes. Part 4 of each description changes from the v1 sentence ("Use this to analyze service output/activity over a time window", which names no alternative) to a part 4 that names the other tool and states the discriminator: what the process printed versus what was recorded as happening to it. The names carry the same discriminator, so the routing signal is in the name and in part 4, not only in parts 1 and 3. |
| Why | This is the pre-authorized exception in §0: the two tools shipped deliberately overlapping as the independent variable of the Domain 2 routing case study, and the plan says Day 14 splits and renames them and re-runs the same queries. The forcing problem is the case study itself, not a defect in the tools - the baseline came back 0/40 misrouted (Sonnet, `scripts/check_tool_routing.py`, 2026-09-13), so the split is made and measured because the method requires an "after", and the write-up reports whatever the re-run measures. |
| What breaks | Nothing in the Reasoning Layer: no agent consumes either tool (the servers were only ever the routing check's subject), so no `default_runners()` entry, prompt, or toolset changes. The one consumer is `scripts/check_tool_routing.py`, which gains a `v1_1` variant that maps the query sets' v1 ground-truth names onto the new names; the query sets and their hashes do not change. `tests/test_analyze_tools.py` keeps pinning the v1 module's weak part 4 (the baseline must stay reproducible) and a new test pins the v1.1 module's part 4 to naming the alternative. `SCHEMA_VERSION` in `aioc` becomes `1.1.0`, so every agent and coordinator payload now stamps `1.1.0`; a `1.0.0` payload is still valid (minor = compatible), and the §8 worked example is left at `1.0.0` to demonstrate exactly that. |
| What was considered instead | (a) Keep the names and only rewrite part 4 - rejected because the plan and §0 both say "split and rename", and because a name is the first routing signal a model reads; measuring only part 4 would understate the intervention. (b) Drop the v1 server module and keep only its descriptions in a test fixture - rejected because the "before" must be re-runnable over the same wire as the "after"; `check_tool_routing.py --variant v1` must keep working. (c) Call the rename a major bump, since by the letter of §0 step 3 a rename is a removal - rejected because §0 fixes the version at `1.1.0` in advance, the removed names have no consumer, and the v1 definitions remain in the document. The tension is recorded here rather than resolved silently. (d) Also split the inputs (a `level` filter only on logs, a `kind` filter only on events) - already the case in v1; nothing to change. |

### 2026-09-25 - no version change - enforcement of invariants the contract already states (Day 16)

Not a contract change, and recorded here so that the next reader can check that claim rather than take it on trust.
No type, field, enum member, or stated invariant moved, `schema_version` stays `1.1.0`, and §9 gets no row.
This entry was written after the code, which the process forbids for a change; it is acceptable only because nothing frozen changed, and the list below is how that can be verified.

Day 16 ran the `contract-audit` skill against the models and closed what it found that the contract already requires:

| Rule, as the contract states it | Before Day 16 | Now |
|---|---|---|
| §0: consumers read `schema_version` and fail loudly on a major mismatch | Nothing read it; a `9.9.9` agent payload inside a `2.0.0` coordinator payload validated | `AgentResponse` and `CoordinatorResponse` refuse a different major or a malformed version (`_common.check_schema_version`) |
| §2.1: a null `Assessment.value` needs a `Gap` whose `blocks_field` references it | Bare string prefix, so `findings.root_cause_elsewhere` covered `findings.root_cause` | Matched on field boundaries (`_common.references_field`) |
| §4.2: `supported == false` claims must not appear in `answer.value` | Not checked; the Docs agent's tool description told the model it was | `DocsFindings` rejects an unsupported claim's statement found in the answer (normalised case, whitespace, trailing punctuation). A paraphrase is beyond any validator, and the docstring says so |
| §4.2: *every* `unanswered` entry has a matching `Gap` | One gap satisfied any number of entries | Counted: one matching gap per entry (an indexed `blocks_field` names the same field) |
| §4.1: `requires_approval` is true when the action mutates production state | Enforced by prompt wording only; the model comment pointed at an empty `hitl/` package | `aioc.hitl.policy` classifies the action; the Incident runtime stamps the flag upward, and the HITL gate re-derives it. The contract model is unchanged - a prose heuristic does not belong in a validator that refuses whole reports |

Found by the audit and deliberately **not** done, because each is a real contract change and needs its own entry before any code:

1. **`other` without a detail field.** `CoordinatorResponse.status`, `AgentInvocation.mode`, and `TimelineEvent.severity` have an `other` member and no `*_detail` sibling, so the §1 pairing cannot be satisfied. Adding the fields is additive-optional (patch).
2. **The evidence rule contradicts itself.** The §2.1 field table says `evidence` may be `[]` only when `value` is null; the invariant list requires evidence only at confidence >= 0.5; the §8 example is consistent with the invariant list, which therefore wins. Striking the table's sentence is a wording fix, but it is frozen wording.
3. **Null outside an `Assessment` with no `Gap`.** §1 says every null carries a gap, but some nulls are legitimate without one (`incident_window.end` = ongoing, `from_version` = first release). This needs a per-field list in the contract, not a blanket validator.
4. **`changed_config_keys` cannot say "not looked".** The field is non-nullable, so a Deployment report that never ran `diff_release` must carry `[]` plus a gap - the only legal form, and one that reads as "looked and found none" under §1. A nullable type would be a type change (major).
5. **§6.4 "must include" details** (`details.field` / `details.expected` on `validation`, `details.required_scope` on `permission`) and the SCREAMING_SNAKE `code` format are unvalidated on `ToolError`. The tool servers are JSON Schema and do not import these models, so enforcement belongs in the servers' own tests first.
6. **RFC 3339 `Z` timestamps and id prefixes** are stated conventions, not "(validated)" rules; a `+05:00` timestamp validates. Tightening them could refuse payloads the contract does not call invalid.

### 2026-09-30 - no version change - `diff_release` commits carry their subject line (Day 21)

Not a contract change, recorded so the claim can be checked.
§7.3's commit shape is `{sha, message, authored_at}` and it still is: no field, type, or input moved, `include` still defaults to `all`, `schema_version` stays `1.1.0`, and §9 gets no row.
What changed is how much of a commit's message `message` carries: the subject line, where it was the first 1,000 characters of the whole message.
Written after the code, like the Day 16 entry, and acceptable for the same reason only.

| Field | |
|---|---|
| Why | Day 21's trimming. A commit body is prose an agent re-reads on every round of its loop, and it is not a release fact. It is also a way round §7.3's own rule: a body saying `DB_POOL_TIMEOUT_MS=2500 is new` put a configuration value in a tool that promises keys only. `tests/test_deployment_tool.py` now pins that a value in a body never leaves. |
| Why this is not a change | The contract never said what `message` holds; the §7.3 example is `"message": "..."`. Part 3 of the tool's description now says it is the subject line, so a consumer is told rather than left to notice. |
| What was considered instead | (a) An additive `message_truncated` flag, as the GitHub tools now carry - a patch bump through the full §0 process for a boolean the description already states for every commit. (b) Defaulting `include` to `config` so commits are opt-in, as HANDOFF item 13 proposed - a changed default on a frozen input, which is a behaviour change for any consumer relying on `all`. Neither was worth a version for what one sentence of part 3 says. |
| Not covered by this entry | The GitHub tools (`get_pull_request`, `list_commits`, `diff_refs`) are not among the six named tools, so their new fields (`message_truncated`, `pull_request_title`, `patch_paths`) and `touched_paths: null` for "not asked" are free to churn and need no entry. |

### Anticipated, not yet made

1. **`TIMELINE_STORE_TIMEOUT` (patch).**
   `get_incident_timeline` reads Postgres, not Prometheus, so the contract's
   `PROMETHEUS_TIMEOUT` in §7.1 would be a false value in a programmatically matched
   field. Additive, so patch level. Flagged in the module docstring since Day 6 and still
   pending an entry here plus a §9 row.
