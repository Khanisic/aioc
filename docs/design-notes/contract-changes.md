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

### Anticipated, not yet made

1. **`TIMELINE_STORE_TIMEOUT` (patch).**
   `get_incident_timeline` reads Postgres, not Prometheus, so the contract's
   `PROMETHEUS_TIMEOUT` in §7.1 would be a false value in a programmatically matched
   field. Additive, so patch level. Flagged in the module docstring since Day 6 and still
   pending an entry here plus a §9 row.
