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

*None yet.*
`schema_version` is still `1.0.0` and nothing frozen has moved.

Two changes are already anticipated:

1. **`analyze_logs` / `analyze_events` split (`1.1.0`).**
   Pre-authorized in CONTRACTS.md §0 and needs no entry here to proceed, but the Day 14
   before/after numbers belong in `docs/case-study-tool-routing.md` when they exist.
2. **`TIMELINE_STORE_TIMEOUT` (patch).**
   `get_incident_timeline` reads Postgres, not Prometheus, so the contract's
   `PROMETHEUS_TIMEOUT` in §7.1 would be a false value in a programmatically matched
   field. Additive, so patch level. Flagged in the module docstring since Day 6 and still
   pending an entry here plus a §9 row.
