---
paths:
  - "src/aioc/contracts/**"
  - "docs/CONTRACTS.md"
---
# Frozen contract - contracts layer

You are in the Pydantic implementation of `docs/CONTRACTS.md` (schema `1.0.0`), which is **frozen**.
`src/aioc/contracts/` is the one package both layers import, so it is the one place where a
convenient local change is a breaking change somewhere else.

- Do not change a frozen type, field, enum member, or invariant as a side effect of other work.
  Frozen = shared primitives, the `AgentResponse` envelope, the four findings payloads,
  `CoordinatorResponse`, the tool envelope + four-class error taxonomy, and the six tool schemas.
- Changing a frozen thing requires the CONTRACTS.md sec 0 process, in order: a dated rationale in
  `docs/design-notes/contract-changes.md` written *before* the code changes, the superseded text
  struck through rather than deleted, a `schema_version` bump (patch = additive-optional,
  minor = additive-required or new enum member, major = removal or type change), and a changelog
  row in sec 9. There is no second engineer to catch a skipped step - the written record is the
  only check, so do not offer to skip it.
- Every model subclasses `StrictModel` (`extra="forbid"`). Keep it that way so a typo or a drifted
  payload fails loudly instead of being silently dropped.
- Enforce conventions with validators, not comments: the `other`+detail pairing, `null` vs `[]`,
  confidence bands (below 0.25 -> value `null` + a `Gap`), and "config values are never returned".
- The MCP boundary is JSON Schema, not Pydantic (contract sec 6): a tool server must not import these
  models. This package is normative for the Reasoning Layer only.
- The worked example in CONTRACTS.md sec 8 is the source of truth: when prose and the example
  disagree, the example wins. `tests/test_contract.py` validates the models against it - keep it green.
