---
description: Surface the governing rules from the frozen contract before you change an area of it.
argument-hint: <topic or section, e.g. "gaps" or "4.1">
allowed-tools: Read, Grep
---
The user is about to work on: **$ARGUMENTS**

1. Search `docs/CONTRACTS.md` for the section(s) relevant to "$ARGUMENTS" and quote the governing
   rules - types, invariants, enum members.
2. State whether the area is FROZEN (sec 0 lists what is frozen). If so, restate the change process:
   a dated rationale in `docs/design-notes/contract-changes.md` written before the code changes,
   the superseded text struck through rather than deleted, a `schema_version` bump, and a changelog
   row in sec 9.
3. Mention the one pre-authorized exception (`analyze_logs` / `analyze_events`, `1.1.0`) if relevant.
4. Point to the implementing module under `src/aioc/contracts/` and any covering test in
   `tests/test_contract.py`.

This is read-only orientation - do not modify anything.
